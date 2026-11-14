"""个股资金流同步（东方财富汇总口径）。

- 口径：主力净额 = 超大单 + 大单净额，由东财按单笔成交金额分档推算，非席位真值
- 取数：push2 ulist.np/get 按 secid 批量拉取（一次 60 只，全市场主板约 50 请求）
- secid 规则：沪市（6 开头）前缀 1，深市/北交所前缀 0
- 收盘链写入官方数据并覆盖全市场主板；盘中只跑股池，控制请求量
- 盘中写入 source=intraday（开盘至今累计值），收盘链写入 official 覆盖当日
- 任何异常都降级为「本次无资金流」，不阻断调用方链路
"""
import logging
import time
from datetime import date

import httpx
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import MoneyFlow, StockPool

log = logging.getLogger(__name__)

# 注意：实测本机容器侧到 push2.eastmoney.com（实时行情节点 61.129.129.196）
# 会被服务端直接断连（RemoteProtocolError），而 push2his 同接口返回完全相同的
# 资金流字段与数值（项目现有日线同步也走 push2his），故固定使用 push2his。
_URL = "https://push2his.eastmoney.com/api/qt/ulist.np/get"
_LIST_URL = "https://push2his.eastmoney.com/api/qt/clist/get"
_HIST_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_HIST_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj")
_SINA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}
_SINA_BATCH = 40  # 新浪单请求代码数（100 只实测超时）
_UT = "b2884a393a59ad64002292a3e90d46a5"
_FIELDS = "f12,f62,f184,f66,f69,f72,f75,f164"
_FS = "m:0+t:6,m:1+t:2"   # 深主板 + 沪主板
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/zjlx/detail.html",
}
_BATCH = 60      # ulist 单请求代码数（过大易被服务端断连）
_PZ = 100        # clist 单页上限（实测大于 100 仍只返回 100）
_MAX_PAGES = 60
_RETRIES = 3     # 单批重试次数
_BACKOFF = 4     # 重试间隔基数（秒）
_GAP = 1.5       # 批次间隔（秒），避免触发限流
_HIST_GAP = 0.35  # 逐只历史回补间隔（秒）

FLOW_KEYS = ("main_net", "main_net_pct", "super_net", "super_net_pct",
             "big_net", "big_net_pct", "main_net_5d")


def _secid(code: str) -> str:
    return f"{'1' if code.startswith('6') else '0'}.{code}"


def _f(v) -> float | None:
    """东财缺失值为 '-' / '' / None，统一转 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(r: dict) -> dict:
    """东财字段 → 库表字段（主力 = 超大单 + 大单）。"""
    return {
        "code": str(r.get("f12")),
        "main_net": _f(r.get("f62")),
        "main_net_pct": _f(r.get("f184")),
        "super_net": _f(r.get("f66")),
        "super_net_pct": _f(r.get("f69")),
        "big_net": _f(r.get("f72")),
        "big_net_pct": _f(r.get("f75")),
        "main_net_5d": _f(r.get("f164")),
    }


def _fetch_batch(client: httpx.Client, codes: list[str]) -> dict[str, dict]:
    params = {"fltt": 2, "invt": 2, "ut": _UT,
              "secids": ",".join(_secid(c) for c in codes),
              "fields": _FIELDS}
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = client.get(_URL, params=params, headers=_HEADERS, timeout=20)
            diff = (resp.json().get("data") or {}).get("diff") or []
            return {str(r["f12"]): _pick(r) for r in diff if r.get("f12")}
        except Exception as e:
            log.warning("money flow batch failed (%d/%d): %s", attempt, _RETRIES, e)
            time.sleep(_BACKOFF * attempt)
    return {}


def _fetch_page(client: httpx.Client, pn: int) -> list[dict]:
    params = {"pn": pn, "pz": _PZ, "po": 1, "np": 1, "fltt": 2, "invt": 2,
              "ut": _UT, "fid": "f62", "fs": _FS, "fields": _FIELDS}
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = client.get(_LIST_URL, params=params, headers=_HEADERS,
                              timeout=20)
            return (resp.json().get("data") or {}).get("diff") or []
        except Exception as e:
            log.warning("money flow page %d failed (%d/%d): %s",
                        pn, attempt, _RETRIES, e)
            time.sleep(_BACKOFF * attempt)
    return []


def universe_codes(db: Session, trade_date: date | None = None) -> list[str]:
    """全市场沪深主板代码（取 <= 指定日的最近一个行情日，避免休市日取空）。"""
    sql = text("""
        SELECT DISTINCT code FROM daily_quotes
        WHERE trade_date = (
            SELECT max(trade_date) FROM daily_quotes WHERE trade_date <= :d)
          AND (code LIKE '60%' OR code LIKE '00%')
        ORDER BY code
    """)
    return [c for (c,) in db.execute(
        sql, {"d": trade_date or date.today()}).all()]


def _sina_symbol(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def _sina_pick(r: dict) -> dict | None:
    """新浪字段 → 库表字段；主力 = 超大单(r0) + 大单(r1)，占比按 收盘价×成交量 近似成交额。

    注意：新浪分档阈值与东财不同（同一天 600000 主力净额 东财 0.89 亿 / 新浪 1.50 亿），
    因此仅在东财不可用时整体切换，避免同一交易日混用两个口径；跨日混用对截面排序影响有限。
    """
    sym = str(r.get("symbol") or "")
    code = sym[-6:]
    if not code.isdigit():
        return None
    r0_in, r0_out = _f(r.get("r0_in")), _f(r.get("r0_out"))
    r1_in, r1_out = _f(r.get("r1_in")), _f(r.get("r1_out"))
    super_net = r0_in - r0_out if None not in (r0_in, r0_out) else None
    big_net = r1_in - r1_out if None not in (r1_in, r1_out) else None
    main_net = (super_net + big_net
                if None not in (super_net, big_net) else None)
    vol, price = _f(r.get("volume")), _f(r.get("trade"))
    amt = vol * price if None not in (vol, price) else None

    def _pct(v: float | None) -> float | None:
        return round(v / amt * 100, 2) if v is not None and amt else None

    return {"code": code, "main_net": main_net, "main_net_pct": _pct(main_net),
            "super_net": super_net, "super_net_pct": _pct(super_net),
            "big_net": big_net, "big_net_pct": _pct(big_net),
            "main_net_5d": None}


def _fetch_sina_batch(client: httpx.Client, codes: list[str]) -> list[dict]:
    params = {"daima": ",".join(_sina_symbol(c) for c in codes)}
    for attempt in (1, 2):
        try:
            data = client.get(_SINA_URL, params=params,
                              headers=_SINA_HEADERS, timeout=25).json()
            return data if isinstance(data, list) else []
        except Exception as e:
            log.warning("sina flow batch failed (%d/2): %s", attempt, e)
            time.sleep(2)
    return []


def fetch_sina_flow(codes: list[str]) -> dict[str, dict]:
    """新浪资金流（备用源）：40 只/批，全市场约 80 请求。"""
    out: dict[str, dict] = {}
    if not codes:
        return out
    batches = [codes[i:i + _SINA_BATCH]
               for i in range(0, len(codes), _SINA_BATCH)]
    try:
        with httpx.Client() as client:
            for idx, batch in enumerate(batches):
                for r in _fetch_sina_batch(client, batch):
                    row = _sina_pick(r)
                    if row:
                        out[row["code"]] = row
                if idx + 1 < len(batches):
                    time.sleep(_GAP)
    except Exception as e:
        log.warning("sina flow fetch aborted: %s", e)
    return out


def _eastmoney_market() -> dict[str, dict]:
    """东财 clist 分页（单页 100 只，全市场约 35 请求）。"""
    out: dict[str, dict] = {}
    try:
        with httpx.Client() as client:
            for pn in range(1, _MAX_PAGES + 1):
                rows = _fetch_page(client, pn)
                if not rows:
                    break
                for r in rows:
                    if r.get("f12"):
                        out[str(r["f12"])] = _pick(r)
                if len(rows) < _PZ:
                    break
                time.sleep(_GAP)
    except Exception as e:
        log.warning("money flow market fetch aborted: %s", e)
    return out


def fetch_market_flow(codes: list[str]) -> dict[str, dict]:
    """全市场资金流：优先东财，覆盖率不足 80% 时用新浪补齐。"""
    out = _eastmoney_market()
    if len(out) >= len(codes) * 0.8:
        return out
    log.warning("eastmoney market flow rows=%d/%d, fallback to sina",
                len(out), len(codes))
    for code, row in fetch_sina_flow(codes).items():
        out.setdefault(code, row)
    return out


def _fetch_history(client: httpx.Client, code: str) -> list[str]:
    """个股资金流历史（东财单只最多返回约 120 个交易日）。"""
    params = {"lmt": 0, "klt": 101, "secid": _secid(code),
              "fields1": "f1,f2,f3,f7", "fields2": _HIST_FIELDS,
              "ut": _UT}
    for attempt in (1, 2):
        try:
            resp = client.get(_HIST_URL, params=params, headers=_HEADERS,
                              timeout=20)
            return (resp.json().get("data") or {}).get("klines") or []
        except Exception as e:
            log.debug("money flow history %s failed (%d/2): %s", code, attempt, e)
            time.sleep(2)
    return []


def _parse_history(code: str, klines: list[str], days: int) -> list[dict]:
    """klines → 库表行；main_net_5d 由主力净额滚动 5 日累加补算。"""
    rows = []
    for line in klines[-days:]:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append({
            "code": code,
            "trade_date": date.fromisoformat(p[0]),
            "main_net": _f(p[1]),
            "big_net": _f(p[4]),
            "super_net": _f(p[5]),
            "main_net_pct": _f(p[6]),
            "big_net_pct": _f(p[9]),
            "super_net_pct": _f(p[10]),
        })
    win: list[float] = []
    for r in rows:
        win.append(r["main_net"] or 0.0)
        if len(win) > 5:
            win.pop(0)
        r["main_net_5d"] = round(sum(win), 2)
    return rows


def backfill_money_flow(db: Session, codes: list[str] | None = None,
                        days: int = 120) -> dict:
    """逐个个股回补资金流历史（约 120 个交易日），用于特征层的历史覆盖。"""
    if codes is None:
        codes = [c for (c,) in db.execute(text("""
            SELECT DISTINCT code FROM daily_quotes
            WHERE trade_date = (SELECT max(trade_date) FROM daily_quotes)
              AND (code LIKE '60%' OR code LIKE '00%')
            ORDER BY code
        """)).all()]
    if not codes:
        return {"codes": 0, "rows": 0}

    buf: list[dict] = []
    total = ok = failed = 0
    try:
        with httpx.Client() as client:
            for i, code in enumerate(codes, 1):
                klines = _fetch_history(client, code)
                if not klines:
                    failed += 1
                else:
                    rows = _parse_history(code, klines, days)
                    buf.extend(rows)
                    total += len(rows)
                    ok += 1
                if len(buf) >= 5000:
                    upsert_money_flow(db, buf, "official")
                    buf = []
                if i % 100 == 0:
                    log.info("money flow backfill %d/%d ok=%d rows=%d failed=%d",
                             i, len(codes), ok, total, failed)
                time.sleep(_HIST_GAP)
    finally:
        if buf:
            upsert_money_flow(db, buf, "official")
    return {"codes": len(codes), "ok": ok, "failed": failed, "rows": total}


def fetch_money_flow(codes: list[str]) -> dict[str, dict]:
    """批量取资金流：东财 ulist 分批，缺口超过 20% 时用新浪补齐。"""
    out: dict[str, dict] = {}
    if not codes:
        return out
    batches = [codes[i:i + _BATCH] for i in range(0, len(codes), _BATCH)]
    try:
        with httpx.Client() as client:
            for idx, batch in enumerate(batches):
                rows = _fetch_batch(client, batch)
                if len(rows) < len(batch):
                    log.warning("money flow partial: %d/%d in batch %d",
                                len(rows), len(batch), idx + 1)
                out.update(rows)
                if idx + 1 < len(batches):
                    time.sleep(_GAP)
    except Exception as e:
        log.warning("money flow fetch aborted: %s", e)
    missing = [c for c in codes if c not in out]
    if missing and len(missing) > len(codes) * 0.2:
        log.warning("eastmoney ulist missing %d/%d, fallback to sina",
                    len(missing), len(codes))
        for code, row in fetch_sina_flow(missing).items():
            out.setdefault(code, row)
    return out


def upsert_money_flow(db: Session, rows: list[dict], source: str,
                      trade_date: date | None = None) -> int:
    """按 (code, trade_date) upsert，official 覆盖同日的 intraday 记录。

    行内自带 trade_date 时优先使用（历史回补场景），否则用统一 trade_date。
    """
    values = []
    for r in rows:
        if not r or not r.get("code"):
            continue
        d = r.get("trade_date") or trade_date
        if not d:
            continue
        values.append({**r, "trade_date": d, "source": source})
    if not values:
        return 0
    stmt = insert(MoneyFlow).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={**{k: getattr(stmt.excluded, k) for k in FLOW_KEYS},
              "source": stmt.excluded.source,
              "updated_at": func.now()})
    db.execute(stmt)
    db.commit()
    return len(values)


def sync_money_flow(db: Session, codes: list[str] | None = None,
                    intraday: bool = False, trade_date: date | None = None) -> dict:
    """同步资金流：收盘官方数据覆盖全市场主板，盘中只跑当期股池。"""
    trade_date = trade_date or date.today()
    source = "intraday" if intraday else "official"
    # 盘中数据不得覆盖当日已落库的官方数据
    if intraday:
        existed = (db.query(MoneyFlow.id)
                   .filter(MoneyFlow.trade_date == trade_date,
                           MoneyFlow.source == "official").first())
        if existed:
            log.info("money flow: official rows already exist for %s, skip intraday",
                     trade_date)
            return {"codes": 0, "rows": 0, "skipped": "official_exists"}

    if intraday or codes is not None:
        if codes is None:
            codes = [c for (c,) in db.query(StockPool.code)
                     .filter(StockPool.active.is_(True)).all()]
        if not codes:
            return {"codes": 0, "rows": 0}
        rows = fetch_money_flow(codes)
        n_codes = len(codes)
    else:
        rows = fetch_market_flow(universe_codes(db, trade_date))
        n_codes = len(rows)

    n = upsert_money_flow(db, list(rows.values()), source, trade_date)
    log.info("money flow synced: %d/%d (source=%s)", n, n_codes, source)
    return {"codes": n_codes, "rows": n, "source": source}


def load_latest(db: Session, codes: list[str]) -> dict[str, dict]:
    """读取各标的最近一条已落库的资金流（实时抓取失败时作降级依据）。"""
    if not codes:
        return {}
    out: dict[str, dict] = {}
    for m in (db.query(MoneyFlow).filter(MoneyFlow.code.in_(codes))
              .order_by(MoneyFlow.trade_date.desc()).all()):
        if m.code in out:
            continue
        out[m.code] = {k: (float(getattr(m, k))
                           if getattr(m, k) is not None else None)
                       for k in FLOW_KEYS}
        out[m.code]["flow_date"] = m.trade_date
    return out
