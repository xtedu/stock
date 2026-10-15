"""个股资金流同步（东方财富汇总口径）。

- 口径：主力净额 = 超大单 + 大单净额，由东财按单笔成交金额分档推算，非席位真值
- 取数：push2 ulist.np/get 按 secid 批量拉取（一次 60 只，股池 200 只约 3-4 请求）
- secid 规则：沪市（6 开头）前缀 1，深市/北交所前缀 0
- 盘中写入 source=intraday（开盘至今累计值），收盘链写入 official 覆盖当日
- 任何异常都降级为「本次无资金流」，不阻断调用方链路
"""
import logging
import time
from datetime import date

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import MoneyFlow, StockPool

log = logging.getLogger(__name__)

# 注意：实测本机容器侧到 push2.eastmoney.com（实时行情节点 61.129.129.196）
# 会被服务端直接断连（RemoteProtocolError），而 push2his 同接口返回完全相同的
# 资金流字段与数值（项目现有日线同步也走 push2his），故固定使用 push2his。
_URL = "https://push2his.eastmoney.com/api/qt/ulist.np/get"
_UT = "b2884a393a59ad64002292a3e90d46a5"
_FIELDS = "f12,f62,f184,f66,f69,f72,f75,f164"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/zjlx/detail.html",
}
_BATCH = 60      # 单请求代码数（过大易被服务端断连）
_RETRIES = 3     # 单批重试次数
_BACKOFF = 4     # 重试间隔基数（秒）
_GAP = 1.5       # 批次间隔（秒），避免触发限流

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


def _fetch_batch(client: httpx.Client, codes: list[str]) -> dict[str, dict]:
    params = {"fltt": 2, "invt": 2, "ut": _UT,
              "secids": ",".join(_secid(c) for c in codes),
              "fields": _FIELDS}
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = client.get(_URL, params=params, headers=_HEADERS, timeout=20)
            diff = (resp.json().get("data") or {}).get("diff") or []
            return {str(r["f12"]): r for r in diff if r.get("f12")}
        except Exception as e:
            log.warning("money flow batch failed (%d/%d): %s", attempt, _RETRIES, e)
            time.sleep(_BACKOFF * attempt)
    return {}


def fetch_money_flow(codes: list[str]) -> dict[str, dict]:
    """批量取资金流，返回 {code: {净额字段}}；失败批次跳过，不抛异常。"""
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
                for code, r in rows.items():
                    out[code] = {
                        "code": code,
                        "main_net": _f(r.get("f62")),
                        "main_net_pct": _f(r.get("f184")),
                        "super_net": _f(r.get("f66")),
                        "super_net_pct": _f(r.get("f69")),
                        "big_net": _f(r.get("f72")),
                        "big_net_pct": _f(r.get("f75")),
                        "main_net_5d": _f(r.get("f164")),
                    }
                if idx + 1 < len(batches):
                    time.sleep(_GAP)
    except Exception as e:
        log.warning("money flow fetch aborted: %s", e)
    return out


def upsert_money_flow(db: Session, rows: list[dict], source: str,
                      trade_date: date) -> int:
    """按 (code, trade_date) upsert，official 覆盖同日的 intraday 记录。"""
    values = [{"trade_date": trade_date, "source": source, **r}
              for r in rows if r and r.get("code")]
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
    """同步资金流：默认股池 active 标的。intraday=False 表示收盘官方数据。"""
    trade_date = trade_date or date.today()
    if codes is None:
        codes = [c for (c,) in db.query(StockPool.code)
                 .filter(StockPool.active.is_(True)).all()]
    if not codes:
        return {"codes": 0, "rows": 0}
    # 盘中数据不得覆盖当日已落库的官方数据
    if intraday:
        existed = (db.query(MoneyFlow.id)
                   .filter(MoneyFlow.trade_date == trade_date,
                           MoneyFlow.source == "official").first())
        if existed:
            log.info("money flow: official rows already exist for %s, skip intraday",
                     trade_date)
            return {"codes": len(codes), "rows": 0, "skipped": "official_exists"}
    rows = fetch_money_flow(codes)
    n = upsert_money_flow(db, list(rows.values()),
                          "intraday" if intraday else "official", trade_date)
    log.info("money flow synced: %d/%d (source=%s)", n, len(codes),
             "intraday" if intraday else "official")
    return {"codes": len(codes), "rows": n,
            "source": "intraday" if intraday else "official"}


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
