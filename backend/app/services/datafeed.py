"""数据采集：AKShare 主数据源（免费）。

- 股票列表 / 日线行情（并发 + 断点续传）
- 财务指标（报告期）/ 估值历史 PE/PB（个股）
"""
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import akshare as ak
import numpy as np
import pandas as pd
import requests
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import (DailyQuote, FinIndicator, IntradaySpot, Stock,
                      ValuationDaily, is_bj_code)

log = logging.getLogger(__name__)


def _f(v):
    """安全转 float（数据源中常见 '--'/空串）。"""
    try:
        v = float(v)
        return v if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def sync_stock_list(db: Session) -> int:
    """拉取 A 股股票列表入库（增量 upsert）。"""
    df = ak.stock_info_a_code_name()
    count = 0
    for _, row in df.iterrows():
        stmt = insert(Stock).values(code=str(row["code"]), name=str(row["name"]))
        stmt = stmt.on_conflict_do_nothing(index_elements=["code"])
        db.execute(stmt)
        count += 1
    db.commit()
    log.info("stock list synced: %d", count)
    return count


def _sina_symbol(code: str) -> str | None:
    """沪深代码 → 新浪符号（sh/sz 前缀）；非沪深返回 None。"""
    if code.startswith("6"):
        return "sh" + code
    if code[0] in "03":
        return "sz" + code
    return None


_SINA_RENAME = {"date": "日期", "open": "开盘", "high": "最高",
                "low": "最低", "close": "收盘", "volume": "成交量",
                "amount": "成交额"}

# 腾讯前复权日线：akshare 的 sina 接口在多线程下会段错误，这里直连 HTTP
_TX_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 "?param={sym},day,{start},{end},640,qfq")
_TX_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch_em(code: str, start: str, end: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(symbol=code, period="daily",
                              start_date=start, end_date=end, adjust="qfq")


def _fetch_tencent(code: str, start: str, end: str) -> pd.DataFrame | None:
    """腾讯前复权日线（仅沪深）。纯 requests 实现，线程安全。

    腾讯单次最多返回 640 根，超出时按最早日期向前翻页。
    接口只提供 成交量（手），成交额按 成交量 × 100 × 收盘价 近似。
    """
    sym = _sina_symbol(code)
    if sym is None:
        return None
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    rows: list[list] = []
    for _ in range(6):  # 最多向前翻 6 页（约 15 年）
        url = _TX_KLINE_URL.format(sym=sym, start=s, end=e)
        payload = requests.get(url, timeout=15, headers=_TX_HEADERS).json()
        node = (payload.get("data") or {}).get(sym) or {}
        day = node.get("qfqday") or node.get("day") or []
        if not day:
            break
        rows = day + rows
        if len(day) < 640 or day[0][0] <= s:
            break
        e = (pd.Timestamp(day[0][0]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if not rows:
        return None
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
    for c in ("开盘", "收盘", "最高", "最低", "成交量"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.drop_duplicates(subset=["日期"]).sort_values("日期")
    df["成交额"] = df["成交量"] * 100.0 * df["收盘"]
    return df.reset_index(drop=True)


def _retry(fn, attempts: int = 3):
    """带指数退避 + 随机抖动的重试；返回非空 DataFrame，否则 None。"""
    for i in range(attempts):
        try:
            df = fn()
            if df is not None and not df.empty:
                return df
        except Exception as e:
            log.debug("fetch attempt %d failed: %s", i + 1, str(e)[:80])
        time.sleep(random.uniform(0.5, 1.2) * (i + 1))
    return None


def _fetch_quotes(code: str, start: str, end: str, use_em: bool = True):
    """单只日线：东财优先，失败降级新浪（沪深）。返回 (code, df, source)。

    每次请求后随机 sleep，并对空响应/异常做退避重试，降低封禁风险。
    """
    if use_em:
        df = _retry(lambda: _fetch_em(code, start, end))
        time.sleep(random.uniform(0.3, 0.8))
        if df is not None:
            return code, df, "eastmoney"
    if is_bj_code(code):
        log.warning("quotes skipped (北交所无可用数据源): %s", code)
        return code, None, "skipped"
    df = _retry(lambda: _fetch_tencent(code, start, end))
    time.sleep(random.uniform(0.3, 0.8))
    return code, df, ("tencent" if df is not None else "fail")


def _upsert_quotes(db: Session, code: str, df: pd.DataFrame) -> int:
    rows = [{
        "code": code,
        "trade_date": pd.to_datetime(r["日期"]).date(),
        "open": _f(r.get("开盘")), "high": _f(r.get("最高")),
        "low": _f(r.get("最低")), "close": _f(r.get("收盘")),
        "volume": int(r.get("成交量")) if pd.notna(r.get("成交量")) else None,
        "amount": _f(r.get("成交额")),
    } for _, r in df.iterrows()]
    if not rows:
        return 0
    stmt = insert(DailyQuote).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={"open": stmt.excluded.open, "high": stmt.excluded.high,
              "low": stmt.excluded.low, "close": stmt.excluded.close,
              "volume": stmt.excluded.volume, "amount": stmt.excluded.amount},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_all_quotes(db: Session, years: int = 5, workers: int = 2,
                    limit: int | None = None) -> dict:
    """全市场日线增量同步（限速 + 熔断，东财优先、失败降级新浪）。

    - 只补 latest_date 之后的新 bar（新股才拉 years 年历史窗口），请求量极小
    - 并发默认 2，每笔请求后随机 sleep 0.3-0.8s
    - 连续失败 25 次判定数据源不可用，直接中止本次任务（不再反复撞击）
    - 北交所（4/8/92 开头）全链路排除，本项目只处理沪深
    """
    total = sync_stock_list(db)
    codes = [c for (c,) in db.query(Stock.code).all()]

    latest = dict(db.query(DailyQuote.code,
                           func.max(DailyQuote.trade_date))
                  .group_by(DailyQuote.code).all())
    today = date.today()
    stale = [c for c in codes if latest.get(c) is None or latest[c] < today]
    todo = [c for c in stale if not is_bj_code(c)]
    if limit:
        todo = todo[:limit]
    log.info("quotes sync: %d total, %d todo (北交所 %d 只跳过)",
             len(codes), len(todo), len(stale) - len(todo))
    if not todo:
        return {"stocks": total, "fetched": 0, "rows": 0, "source": "-"}

    end = today.strftime("%Y%m%d")
    # 探活：先用一只沪深票试东财，成功则高并发跑东财；失败则全程低并发走新浪
    use_em = False
    probe = next((c for c in todo if not is_bj_code(c)), None)
    if probe is not None:
        try:
            _fetch_em(probe, (today - timedelta(days=10)).strftime("%Y%m%d"), end)
            use_em = True
            log.info("eastmoney quotes alive, use it first")
        except Exception as e:
            log.warning("eastmoney quotes unavailable, fallback to sina: %s",
                        str(e)[:80])

    # 并发统一压到 2：akshare 底层原生扩展在多线程并发下曾出现 SIGSEGV
    # （2 核小内存机上尤为明显），与本地长时间验证过的配置保持一致。
    max_workers = int(os.getenv("QUOTES_SYNC_WORKERS", "2"))
    em_workers = max_workers
    sg_workers = max_workers

    def _start_of(code: str) -> str:
        last = latest.get(code)
        if last is None:
            return (today - timedelta(days=years * 365)).strftime("%Y%m%d")
        # 新浪 stock_zh_a_daily 不认 start==end 的单天窗口。
        # 对 last 接近 today 的股票（差 1-2 天），把 start 提前到 today-2
        # 确保至少 3 天窗口；对 last 较早的股票（差 >2 天），直接用 last+1。
        ideal = last + timedelta(days=1)
        return min(ideal, today - timedelta(days=2)).strftime("%Y%m%d")

    ok = rows = 0
    fail = 0
    src: dict[str, int] = {}
    # 熔断改成**成功率判定**：处理到 50 只时如果 fail >= 45（90% 失败），
    # 说明两个源都挂了才 abort；否则跑完。避免"连续 25 只 fail 就 abort"
    # 的激进逻辑——线程池并发时前几批很可能因为网络抖动全挂。
    _abort_ratio = 0.90
    _abort_window = 50
    processed = 0

    def _pick_workers(use_em_flag: bool) -> int:
        return em_workers if use_em_flag else sg_workers

    with ThreadPoolExecutor(max_workers=_pick_workers(use_em)) as ex:
        futs = {ex.submit(_fetch_quotes, c, _start_of(c), end, use_em): c
                for c in todo}
        for fut in as_completed(futs):
            code, df, source = fut.result()
            processed += 1
            src[source] = src.get(source, 0) + 1
            if source == "fail":
                fail += 1
            if df is not None and not df.empty:
                rows += _upsert_quotes(db, code, df)
                ok += 1
            # 熔断：处理足够样本后，失败率超过阈值才判定两源全挂
            if (processed >= _abort_window
                    and fail / processed >= _abort_ratio
                    and fail >= _abort_window * _abort_ratio):
                log.error("quotes sync aborted: %d/%d failed after %d processed (%.0f%%)",
                          fail, len(todo), processed, fail / processed * 100)
                for f in futs:
                    f.cancel()
                break
    # 跑完后检查覆盖率：如果今日行情覆盖率低于 80%，记 warning
    db.commit()
    coverage = db.query(func.count(DailyQuote.code.distinct())
                        .filter(DailyQuote.trade_date == today)).scalar() or 0
    hs_total = len([c for c, in db.query(Stock.code).all()
                    if not is_bj_code(c)])
    cov_pct = coverage / hs_total * 100 if hs_total else 0
    if cov_pct < 80:
        log.warning("daily quotes coverage low: %.1f%% (%d/%d)",
                    cov_pct, coverage, hs_total)
    log.info("quotes synced: %d/%d stocks, %d rows, src=%s, coverage=%.1f%%",
             ok, len(todo), rows, src, cov_pct)
    return {"stocks": total, "fetched": ok, "rows": rows, "source": src,
            "eastmoney": use_em, "coverage": round(cov_pct, 1)}


# ---------- 盘中全市场快照 ----------

def _fetch_spot_em() -> pd.DataFrame:
    return ak.stock_zh_a_spot_em()


def _fetch_spot_sina() -> pd.DataFrame:
    """新浪全市场快照：代码带 sh/sz/bj 前缀需去掉；不含 PE/PB。"""
    df = ak.stock_zh_a_spot()
    df["代码"] = df["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "",
                                                    regex=True)
    return df


def fetch_spot_bars() -> pd.DataFrame | None:
    """盘中全市场快照 → 标准化 OHLCV（只读，不落库）。

    东财优先、新浪降级，失败返回 None。供妖股盘中模式拼当日临时 K 线。
    """
    col_map = {"代码": "code", "今开": "open", "最高": "high", "最低": "low",
               "最新价": "close", "成交量": "volume", "成交额": "amount",
               "涨跌幅": "pct_chg"}
    try:
        df = _fetch_spot_em()
    except Exception as e:  # noqa: BLE001
        log.warning("eastmoney spot unavailable, fallback to sina: %s",
                    str(e)[:80])
        try:
            df = _retry(_fetch_spot_sina)
        except Exception as e2:  # noqa: BLE001
            log.error("spot fetch failed: %s", str(e2)[:80])
            return None
    if df is None or df.empty or "代码" not in df.columns:
        return None
    keep = {k: v for k, v in col_map.items() if k in df.columns}
    out = df[list(keep)].rename(columns=keep)
    for c in out.columns:
        if c != "code":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out["code"] = (out["code"].astype(str)
                   .str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6))
    out = out[out["code"].str.len() == 6]
    out = out[~out["code"].apply(is_bj_code)]
    out = out[out["close"].fillna(0) > 0]
    for c in ("open", "high", "low", "volume", "amount", "pct_chg"):
        if c not in out.columns:
            out[c] = np.nan
    return out.reset_index(drop=True)


def sync_spot_snapshot(db: Session) -> int:
    """全市场最新价快照：东财优先，失败降级新浪，覆盖写入 intraday_spot。

    本表 pe/pb 无任何消费方（各处只读 price），新浪源缺失时写 None。
    """
    today = date.today()
    try:
        df = _fetch_spot_em()
        pe_col, pb_col, src = "市盈率-动态", "市净率", "eastmoney"
    except Exception as e:
        log.warning("eastmoney spot unavailable, fallback to sina: %s",
                    str(e)[:80])
        df = _retry(_fetch_spot_sina)
        pe_col = pb_col = None
        src = "sina"
    if df is None:
        log.error("spot snapshot failed: eastmoney and sina both unavailable")
        return 0
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if len(code) != 6 or is_bj_code(code):
            continue
        rows.append({"code": code, "price": _f(r.get("最新价")),
                     "pe": _f(r.get(pe_col)) if pe_col else None,
                     "pb": _f(r.get(pb_col)) if pb_col else None,
                     "trade_date": today})
    if not rows:
        return 0
    stmt = insert(IntradaySpot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={"price": stmt.excluded.price, "pe": stmt.excluded.pe,
              "pb": stmt.excluded.pb, "trade_date": stmt.excluded.trade_date,
              "updated_at": func.now()},
    )
    db.execute(stmt)
    db.commit()
    log.info("spot snapshot synced: %d rows (src=%s)", len(rows), src)
    return len(rows)


# ---------- 财务指标（报告期） ----------

FIN_COLS = {
    "净资产收益率(%)": "roe",
    "主营业务收入增长率(%)": "revenue_yoy",
    "净利润增长率(%)": "profit_yoy",
    "资产负债率(%)": "debt_ratio",
    "每股经营性现金流(元)": "ocf",
    "扣除非经常性损益后的净利润(元)": "np_deducted",
    "经营现金净流量与净利润的比率(%)": "ocf_np_ratio",
}


def _fetch_fin(code: str, start_year: str):
    try:
        return code, ak.stock_financial_analysis_indicator(
            symbol=code, start_year=start_year)
    except Exception as e:
        log.warning("fetch fin failed %s: %s", code, e)
        return code, None


def _upsert_fin(db: Session, code: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        rd = pd.to_datetime(r["日期"], errors="coerce")
        if pd.isna(rd):
            continue
        rows.append({"code": code, "report_date": rd.date(),
                     **{v: _f(r.get(k)) for k, v in FIN_COLS.items()}})
    if not rows:
        return 0
    stmt = insert(FinIndicator).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "report_date"],
        set_={v: getattr(stmt.excluded, v) for v in FIN_COLS.values()},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_fin_indicators(db: Session, code: str, years: int = 5) -> int:
    """拉取单只股票近 N 年报告期财务指标并 upsert。"""
    df = _fetch_fin(code, str(date.today().year - years))[1]
    return _upsert_fin(db, code, df)


# ---------- 估值历史（PE/PB 日度） ----------

def _fetch_val(code: str):
    try:
        return code, ak.stock_value_em(symbol=code)
    except Exception as e:
        log.warning("fetch valuation failed %s: %s", code, e)
        return code, None


def _upsert_val(db: Session, code: str, df: pd.DataFrame, years: int = 5) -> int:
    if df is None or df.empty:
        return 0
    cutoff = date.today() - timedelta(days=years * 365)
    rows = []
    for _, r in df.iterrows():
        td = pd.to_datetime(r["数据日期"], errors="coerce")
        if pd.isna(td) or td.date() < cutoff:
            continue
        rows.append({"code": code, "trade_date": td.date(),
                     "pe": _f(r.get("PE(TTM)")), "pb": _f(r.get("市净率"))})
    if not rows:
        return 0
    stmt = insert(ValuationDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={"pe": stmt.excluded.pe, "pb": stmt.excluded.pb},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_valuation(db: Session, code: str, years: int = 5) -> int:
    """拉取单只股票 PE(TTM)/PB 历史并 upsert（限近 N 年）。"""
    df = _fetch_val(code)[1]
    return _upsert_val(db, code, df, years)


def sync_stock_fundamentals(db: Session, code: str) -> dict:
    return {"fin": sync_fin_indicators(db, code),
            "val": sync_valuation(db, code)}


def sync_all_fundamentals(db: Session, years: int = 5, workers: int = 4) -> dict:
    """全市场基本面同步：财务指标 + 估值历史（仅沪深，并发抓取、串行入库）。"""
    codes = [c for (c,) in db.query(Stock.code).all() if not is_bj_code(c)]
    ok_fin = ok_val = 0
    start_year = str(date.today().year - years)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs_fin = {ex.submit(_fetch_fin, c, start_year): c for c in codes}
        for fut in as_completed(futs_fin):
            code, df = fut.result()
            try:
                if _upsert_fin(db, code, df):
                    ok_fin += 1
            except Exception as e:
                db.rollback()
                log.warning("upsert fin failed %s: %s", code, e)
        futs_val = {ex.submit(_fetch_val, c): c for c in codes}
        for fut in as_completed(futs_val):
            code, df = fut.result()
            try:
                if _upsert_val(db, code, df, years):
                    ok_val += 1
            except Exception as e:
                db.rollback()
                log.warning("upsert val failed %s: %s", code, e)
    log.info("fundamentals synced: fin=%d val=%d / %d", ok_fin, ok_val, len(codes))
    return {"stocks": len(codes), "fin_synced": ok_fin, "val_synced": ok_val}


# ---------- 行业分类（申万二级，准静态数据） ----------

_SW_LEVEL_SUFFIX = ("Ⅰ", "Ⅱ", "Ⅲ")


def _clean_sw_name(name) -> str:
    """去掉申万行业名末尾的分级罗马数字（如 白酒Ⅱ → 白酒）。"""
    n = str(name).strip()
    while n and n[-1] in _SW_LEVEL_SUFFIX:
        n = n[:-1]
    return n.strip()


def _fetch_sw_cons(sw_code: str, sw_name: str):
    try:
        df = ak.index_component_sw(symbol=sw_code)
        return sw_name, [str(c) for c in df["证券代码"].tolist()]
    except Exception as e:
        log.warning("fetch sw cons failed %s(%s): %s", sw_name, sw_code, e)
        return sw_name, []


def sync_industry(db: Session, workers: int = 6) -> dict:
    """申万二级行业分类 → stocks.industry（准静态，手动/周度触发）。

    东财行业接口（stock_board_industry_*）在当前网络下不可用，改用申万
    行业：先取二级行业清单（约 131 个），再逐个拉成分股，全市场约 131 次
    请求，代码→行业名批量写回 stocks.industry。独立路径，不挂日更链路。
    """
    info = ak.sw_index_second_info()
    pairs = [(str(r["行业代码"]).removesuffix(".SI"), _clean_sw_name(r["行业名称"]))
             for _, r in info.iterrows()]
    by_name: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_sw_cons, c, n) for c, n in pairs]
        for fut in as_completed(futs):
            name, codes = fut.result()
            if codes:
                by_name.setdefault(name, []).extend(codes)

    updated = 0
    for name, codes in by_name.items():
        res = db.execute(update(Stock).where(Stock.code.in_(codes))
                         .values(industry=name))
        updated += res.rowcount or 0
    db.commit()
    log.info("industry synced: %d industries, %d stocks updated",
             len(pairs), updated)
    return {"industries": len(pairs), "updated": updated}
