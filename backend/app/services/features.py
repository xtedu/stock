"""全市场每日截面特征（沪深主板）：所有策略的统一取数底座。

设计约束：
- 仅沪深主板（60x / 00x），与股票池口径一致
- (code, as_of) 唯一，同日重算覆盖
- 财务使用 avail_date 做时点过滤（防前视）
- feature_version 标记口径版本，便于回溯与重算
"""
import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import (DailyQuote, FeatureStockDaily, MoneyFlow, Stock,
                      ValuationDaily)
from . import quality
from .pick import _load_fin, _load_heat

log = logging.getLogger(__name__)

FEATURE_VERSION = "f1"
LOOKBACK_DAYS = 420          # 覆盖 ma250 + 60 日前向回看
CHUNK = 400                  # 单条 INSERT 参数数量控制


def resolve_as_of(db: Session, as_of: date | None = None) -> date | None:
    if as_of:
        return as_of
    return db.query(func.max(DailyQuote.trade_date)).scalar()


def _price_features(db: Session, as_of: date) -> pd.DataFrame:
    start = as_of - timedelta(days=LOOKBACK_DAYS)
    sql = text("""
        WITH r AS (
          SELECT code, trade_date, close, amount,
                 lag(close) OVER w AS prev,
                 avg(close) OVER (PARTITION BY code ORDER BY trade_date
                                  ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20r,
                 avg(close) OVER (PARTITION BY code ORDER BY trade_date
                                  ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60r,
                 avg(close) OVER (PARTITION BY code ORDER BY trade_date
                                  ROWS BETWEEN 249 PRECEDING AND CURRENT ROW) AS ma250r
          FROM daily_quotes
          WHERE trade_date BETWEEN :start AND :as_of
            AND (code LIKE '60%' OR code LIKE '00%')
          WINDOW w AS (PARTITION BY code ORDER BY trade_date)
        ),
        x AS (
          SELECT *, row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn
          FROM r
        )
        SELECT code,
               max(close) FILTER (WHERE rn = 1) AS close,
               max(amount) FILTER (WHERE rn = 1) AS amount,
               max(prev) FILTER (WHERE rn = 1) AS prev,
               max(close) FILTER (WHERE rn = 1)
                 / NULLIF(max(prev) FILTER (WHERE rn = 1), 0) - 1 AS pct_chg,
               max(close) FILTER (WHERE rn = 1)
                 / NULLIF(max(close) FILTER (WHERE rn = 6), 0) - 1 AS ret5,
               max(close) FILTER (WHERE rn = 1)
                 / NULLIF(max(close) FILTER (WHERE rn = 21), 0) - 1 AS ret20,
               max(close) FILTER (WHERE rn = 1)
                 / NULLIF(max(close) FILTER (WHERE rn = 61), 0) - 1 AS ret60,
               max(ma20r) FILTER (WHERE rn = 1) AS ma20,
               max(ma60r) FILTER (WHERE rn = 1) AS ma60,
               max(ma250r) FILTER (WHERE rn = 1) AS ma250,
               max(ma20r) FILTER (WHERE rn = 1)
                 / NULLIF(max(ma20r) FILTER (WHERE rn = 6), 0) - 1 AS ma20_slope_pct,
               max(close) FILTER (WHERE rn = 1)
                 / NULLIF(max(ma20r) FILTER (WHERE rn = 1), 0) - 1 AS bias20,
               stddev_samp(CASE WHEN rn <= 20
                                THEN close / NULLIF(prev, 0) - 1 END) AS vol20,
               stddev_samp(CASE WHEN rn <= 60
                                THEN close / NULLIF(prev, 0) - 1 END) AS vol60,
               avg(amount) FILTER (WHERE rn <= 20) AS amt20,
               (max(close) FILTER (WHERE rn = 1)
                - min(close) FILTER (WHERE rn <= 60))
                 / NULLIF(max(close) FILTER (WHERE rn <= 60)
                          - min(close) FILTER (WHERE rn <= 60), 0) AS pos60,
               count(*) AS n_hist
        FROM x
        GROUP BY code
    """)
    df = pd.DataFrame(db.execute(sql, {"start": start, "as_of": as_of})
                      .mappings().all())
    if df.empty:
        return df
    for c in ("close", "amount", "prev", "pct_chg", "ret5", "ret20", "ret60",
              "ma20", "ma60", "ma250", "ma20_slope_pct", "bias20",
              "vol20", "vol60", "amt20", "pos60"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["amt_ratio"] = df["amount"] / df["amt20"].where(df["amt20"] > 0)
    df["limit_up"] = df["pct_chg"] >= 0.095
    return df


def _streak_map(db: Session, as_of: date) -> dict[str, int]:
    """截至 as_of 的连续涨停天数（仅统计主板）。"""
    start = as_of - timedelta(days=45)
    rows = db.execute(text("""
        SELECT code, trade_date, close FROM daily_quotes
        WHERE trade_date BETWEEN :start AND :as_of
          AND (code LIKE '60%' OR code LIKE '00%')
        ORDER BY code, trade_date
    """), {"start": start, "as_of": as_of}).mappings().all()
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["prev"] = df.groupby("code")["close"].shift(1)
    df["lu"] = df["close"] / df["prev"] - 1 >= 0.095
    out: dict[str, int] = {}
    for code, g in df.groupby("code"):
        n = 0
        for flag in reversed(g["lu"].fillna(False).tolist()):
            if flag:
                n += 1
            else:
                break
        if n:
            out[code] = n
    return out


def _valuation_features(db: Session, as_of: date) -> pd.DataFrame:
    start = as_of - timedelta(days=365 * 5)
    sql = text("""
        WITH v AS (
          SELECT code, trade_date, pe, pb,
                 percent_rank() OVER (PARTITION BY code ORDER BY pe) AS pr,
                 row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn
          FROM valuation_daily
          WHERE trade_date BETWEEN :start AND :as_of
            AND pe IS NOT NULL AND pe > 0
            AND (code LIKE '60%' OR code LIKE '00%')
        )
        SELECT code,
               max(pe) FILTER (WHERE rn = 1) AS pe,
               max(pb) FILTER (WHERE rn = 1) AS pb,
               max(pr) FILTER (WHERE rn = 1) * 100 AS pe_pct5y
        FROM v GROUP BY code
    """)
    df = pd.DataFrame(db.execute(sql, {"start": start, "as_of": as_of})
                      .mappings().all())
    for c in ("pe", "pb", "pe_pct5y"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _money_flow_features(db: Session, as_of: date) -> pd.DataFrame:
    rows = (db.query(MoneyFlow.code, MoneyFlow.main_net_pct,
                     MoneyFlow.main_net_5d)
            .filter(MoneyFlow.trade_date <= as_of)
            .order_by(MoneyFlow.code, MoneyFlow.trade_date.desc())
            .distinct(MoneyFlow.code).all())
    df = pd.DataFrame(rows, columns=["code", "mf_main_net_pct", "mf_main_net_5d"])
    for c in ("mf_main_net_pct", "mf_main_net_5d"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def build_features(db: Session, as_of: date | None = None) -> dict:
    as_of = resolve_as_of(db, as_of)
    if not as_of:
        return {"as_of": None, "rows": 0, "reason": "no daily_quotes"}

    price = _price_features(db, as_of)
    if price.empty:
        return {"as_of": str(as_of), "rows": 0, "reason": "no price data"}

    # 只保留满足最小历史长度的股票，避免新股污染截面
    price = price[price["n_hist"] >= 60].copy()

    stocks = pd.DataFrame(db.query(Stock.code, Stock.name, Stock.industry)
                          .all(), columns=["code", "name", "industry"])
    fin = _load_fin(db, as_of)
    if not fin.empty:
        price = price.merge(
            fin.reset_index()[["code", "roe_avg", "profit_cagr",
                               "rev_cagr", "debt_ratio"]],
            on="code", how="left")
    val = _valuation_features(db, as_of)
    if not val.empty:
        price = price.merge(val, on="code", how="left")
    mf = _money_flow_features(db, as_of)
    if not mf.empty:
        price = price.merge(mf, on="code", how="left")

    price = price.merge(stocks[["code", "industry"]], on="code", how="left")
    heat = _load_heat(db, as_of)
    price["ind_heat3"] = price["industry"].map(heat)
    streak = _streak_map(db, as_of)
    price["streak"] = price["code"].map(streak).fillna(0).astype(int)

    cols = ["code", "as_of", "industry", "close", "amount", "pct_chg",
            "ret5", "ret20", "ret60", "ma20", "ma60", "ma250", "bias20",
            "ma20_slope_pct", "vol20", "vol60", "amt20", "amt_ratio",
            "pos60", "limit_up", "streak", "pe", "pb", "pe_pct5y",
            "roe_avg", "profit_cagr", "rev_cagr", "debt_ratio",
            "mf_main_net_pct", "mf_main_net_5d", "ind_heat3"]
    price["as_of"] = as_of
    price["feature_version"] = FEATURE_VERSION
    for c in cols:
        if c not in price:
            price[c] = None
    out = price[cols + ["feature_version"]]
    records = (out.astype(object).where(out.notna(), None)
               .to_dict("records"))

    for i in range(0, len(records), CHUNK):
        stmt = insert(FeatureStockDaily).values(records[i:i + CHUNK])
        update = {c: getattr(stmt.excluded, c) for c in cols
                  if c not in ("code", "as_of")}
        update["feature_version"] = stmt.excluded.feature_version
        update["created_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            index_elements=["code", "as_of"], set_=update)
        db.execute(stmt)
    db.commit()

    # 质量门禁：覆盖率与关键字段填充率
    eligible = db.execute(text(
        "SELECT count(*) FROM stocks WHERE code LIKE '60%' OR code LIKE '00%'"
    )).scalar() or 0
    coverage = round(float(len(price)) / eligible * 100, 2) if eligible else None
    quality.record(db, "feat_stock_daily", as_of, "coverage_pct", coverage,
                   quality.status_of(coverage, 95.0, 85.0),
                   {"rows": len(records), "eligible": int(eligible)})
    for col in ("pe", "profit_cagr", "mf_main_net_pct"):
        rate = (round(float(price[col].notna().mean()) * 100, 2)
                if len(price) else None)
        quality.record(db, "feat_stock_daily", as_of, f"{col}_fill_pct", rate,
                       quality.status_of(rate, 60.0, 30.0))
    db.commit()

    log.info("features built: as_of=%s rows=%d coverage=%s%%",
             as_of, len(records), coverage)
    return {"as_of": str(as_of), "rows": len(records),
            "coverage_pct": coverage, "feature_version": FEATURE_VERSION}


def build_features_range(db: Session, days: int = 250) -> dict:
    """回补最近 N 个交易日的全市场特征（可行性检验/训练需要历史截面）。"""
    dates = [r[0] for r in db.query(DailyQuote.trade_date)
             .filter(DailyQuote.code.like("60%") | DailyQuote.code.like("00%"))
             .distinct().order_by(DailyQuote.trade_date.desc())
             .limit(days).all()]
    if not dates:
        return {"days": 0, "rows": 0}
    total = 0
    for d in sorted(dates):
        res = build_features(db, d)
        total += res.get("rows", 0)
    log.info("features backfilled: %d days, %d rows", len(dates), total)
    return {"days": len(dates), "rows": total,
            "first": str(min(dates)), "last": str(max(dates))}
