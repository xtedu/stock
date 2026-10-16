"""数据采集：AKShare 主数据源（免费）。

- 股票列表 / 日线行情（并发 + 断点续传）
- 财务指标（报告期）/ 估值历史 PE/PB（个股）
"""
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import akshare as ak
import pandas as pd
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


def _fetch_em(code: str, start: str, end: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(symbol=code, period="daily",
                              start_date=start, end_date=end, adjust="qfq")


def _fetch_sina(code: str, start: str, end: str) -> pd.DataFrame | None:
    """新浪日线（仅沪深）。列名对齐库内口径，成交量由「股」换算为「手」。"""
    sym = _sina_symbol(code)
    if sym is None:
        return None
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end,
                             adjust="qfq")
    if df is None or df.empty:
        return df
    df = df.rename(columns=_SINA_RENAME)
    df["成交量"] = df["成交量"] / 100.0
    return df


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
        log.warning("quotes skipped (北交所无新浪源): %s", code)
        return code, None, "skipped"
    df = _retry(lambda: _fetch_sina(code, start, end))
    time.sleep(random.uniform(0.3, 0.8))
    return code, df, ("sina" if df is not None else "fail")


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
    # 探活：先用一只沪深票试东财，失败则本次全程走新浪（不白撞被封的源）
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

    def _start_of(code: str) -> str:
        last = latest.get(code)
        return ((last + timedelta(days=1)) if last
                else (today - timedelta(days=years * 365))).strftime("%Y%m%d")

    ok = rows = consec = 0
    src: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_quotes, c, _start_of(c), end, use_em): c
                for c in todo}
        for fut in as_completed(futs):
            code, df, source = fut.result()
            src[source] = src.get(source, 0) + 1
            consec = consec + 1 if source == "fail" else 0
            if df is not None and not df.empty:
                rows += _upsert_quotes(db, code, df)
                ok += 1
            if consec >= 25:
                log.warning("quotes sync aborted after %d consecutive failures",
                            consec)
                for f in futs:
                    f.cancel()
                break
    log.info("quotes synced: %d/%d stocks, %d rows, src=%s",
             ok, len(todo), rows, src)
    return {"stocks": total, "fetched": ok, "rows": rows, "source": src,
            "eastmoney": use_em}


# ---------- 盘中全市场快照 ----------

def sync_spot_snapshot(db: Session) -> int:
    """单次调用拉取全市场最新价/PE/PB，覆盖写入 intraday_spot（约10秒）。"""
    df = ak.stock_zh_a_spot_em()
    today = date.today()
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if len(code) != 6 or is_bj_code(code):
            continue
        rows.append({"code": code, "price": _f(r.get("最新价")),
                     "pe": _f(r.get("市盈率-动态")), "pb": _f(r.get("市净率")),
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
    log.info("spot snapshot synced: %d rows", len(rows))
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
