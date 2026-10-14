"""数据采集：AKShare 主数据源（免费）。

- 股票列表 / 日线行情（并发 + 断点续传）
- 财务指标（报告期）/ 估值历史 PE/PB（个股）
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import DailyQuote, FinIndicator, Stock, ValuationDaily, IntradaySpot

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


def _fetch_quotes(code: str, start: str, end: str):
    try:
        return code, ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date=start, end_date=end,
                                        adjust="qfq")
    except Exception as e:
        log.warning("fetch quotes failed %s: %s", code, e)
        return code, None


def _upsert_quotes(db: Session, code: str, df: pd.DataFrame) -> int:
    rows = [{
        "code": code,
        "trade_date": pd.to_datetime(r["日期"]).date(),
        "open": _f(r.get("开盘")), "high": _f(r.get("最高")),
        "low": _f(r.get("最低")), "close": _f(r.get("收盘")),
        "volume": r.get("成交量") if pd.notna(r.get("成交量")) else None,
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


def sync_all_quotes(db: Session, years: int = 5, workers: int = 6) -> dict:
    """全市场日线同步：并发抓取，当日已有数据的股票跳过（断点续传）。"""
    total = sync_stock_list(db)
    codes = [c for (c,) in db.query(Stock.code).all()]

    latest = dict(db.query(DailyQuote.code,
                           func.max(DailyQuote.trade_date))
                  .group_by(DailyQuote.code).all())
    today = date.today()
    todo = [c for c in codes if latest.get(c) is None or latest[c] < today]
    log.info("quotes sync: %d total, %d todo", len(codes), len(todo))

    start = (today - timedelta(days=years * 365)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    ok = rows = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_quotes, c, start, end) for c in todo]
        for fut in as_completed(futs):
            code, df = fut.result()
            if df is None or df.empty:
                continue
            rows += _upsert_quotes(db, code, df)
            ok += 1
    log.info("quotes synced: %d/%d stocks, %d rows", ok, len(todo), rows)
    return {"stocks": total, "fetched": ok, "rows": rows}


# ---------- 盘中全市场快照 ----------

def sync_spot_snapshot(db: Session) -> int:
    """单次调用拉取全市场最新价/PE/PB，覆盖写入 intraday_spot（约10秒）。"""
    df = ak.stock_zh_a_spot_em()
    today = date.today()
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if len(code) != 6:
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
    """全市场基本面同步：财务指标 + 估值历史（并发抓取，主线程串行入库）。"""
    codes = [c for (c,) in db.query(Stock.code).all()]
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
