"""每日三只（沪深）：硬门槛 + 利润/营收增速分位排序。

规则来自回测结论（2025-02 ~ 2026-10）：
- 硬门槛：非ST / 沪深 / 上市满3年 / 近4期 ROE>=10 / 负债<=60 / 扣非>0
          / 60日涨幅<=20% / 20日均成交额>=1亿
- 打分：利润增速 70% + 营收增速 30%，均为池内分位，避免封顶并列
- 取前 3，同一行业最多 2 只
- 安全区间 / MA20 斜率 / 行业涨停热度只做标签，不参与筛选（回测中均为负贡献）
"""
import logging
import time
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from .finutil import avail_date

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CTX: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600

ROE_MIN = 10.0
DEBT_MAX = 60.0
RET60_MAX = 0.20
MIN_AMOUNT = 1e8
MIN_HISTORY = 750


def _ld_sql(as_of: date | None) -> tuple[str, dict]:
    """用于 CTE 的「基准交易日」：指定 as_of 时用它，否则取全库最新。"""
    if as_of:
        return "SELECT CAST(:as_of AS date) AS d", {"as_of": as_of}
    return "SELECT max(trade_date) AS d FROM daily_quotes", {}


def _load_features(db: Session, as_of: date | None = None) -> pd.DataFrame:
    ld, params = _ld_sql(as_of)
    sql = text(f"""
        WITH ld AS ({ld}),
        b AS (
          SELECT q.code, q.trade_date, q.close::float8 AS close,
                 q.amount::float8 AS amount,
                 lag(q.close) OVER (PARTITION BY q.code ORDER BY q.trade_date) AS prev_close,
                 lag(q.close, 20) OVER (PARTITION BY q.code ORDER BY q.trade_date) AS close20,
                 lag(q.close, 60) OVER (PARTITION BY q.code ORDER BY q.trade_date) AS close60,
                 avg(q.close) OVER (PARTITION BY q.code ORDER BY q.trade_date
                     ROWS BETWEEN 249 PRECEDING AND CURRENT ROW) AS ma250,
                 avg(q.close) OVER (PARTITION BY q.code ORDER BY q.trade_date
                     ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
                 avg(q.amount) OVER (PARTITION BY q.code ORDER BY q.trade_date
                     ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS amt20
          FROM daily_quotes q
          WHERE q.trade_date >= (SELECT d FROM ld) - INTERVAL '520 days'
        ),
        b2 AS (SELECT *, lag(ma20, 5) OVER (PARTITION BY code ORDER BY trade_date) AS ma20_5
               FROM b)
        SELECT code, close, prev_close, close20, close60, ma250, ma20, ma20_5,
               amt20, trade_date
        FROM b2 WHERE trade_date = (SELECT d FROM ld)
    """)
    rows = db.execute(sql, params).mappings().all()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    num = ["close", "prev_close", "close20", "close60", "ma250", "ma20",
           "ma20_5", "amt20"]
    df[num] = df[num].astype(float)
    df["ret20"] = df["close"] / df["close20"] - 1
    df["ret60"] = df["close"] / df["close60"] - 1
    df["slope"] = (df["ma20"] / df["ma20_5"] - 1) * 100 / 5
    return df


def _load_history(db: Session, as_of: date | None = None) -> pd.DataFrame:
    where, params = ("WHERE trade_date <= :as_of", {"as_of": as_of}) if as_of \
        else ("", {})
    sql = text(f"""
        SELECT code, count(*) AS hist, max(trade_date) AS last_date
        FROM daily_quotes {where} GROUP BY code
    """)
    df = pd.DataFrame(db.execute(sql, params).mappings().all())
    if not df.empty:
        df["last_date"] = pd.to_datetime(df["last_date"])
    return df


def _load_fin(db: Session, today: date) -> pd.DataFrame:
    sql = text("""
        SELECT code, report_date, roe, revenue_yoy, profit_yoy, debt_ratio, np_deducted
        FROM fin_indicators WHERE report_date <= :d
    """)
    df = pd.DataFrame(db.execute(sql, {"d": today}).mappings().all())
    if df.empty:
        return df
    for c in ("roe", "revenue_yoy", "profit_yoy", "debt_ratio", "np_deducted"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["avail"] = df["report_date"].dt.date.map(avail_date)
    df = df[df["avail"] <= today]
    if df.empty:
        return df
    df = df.sort_values(["code", "report_date"])

    last4 = df.groupby("code").tail(4)
    agg = last4.groupby("code").agg(roe_avg=("roe", "mean"),
                                    roe_n=("roe", "count"))
    l3 = df.groupby("code").tail(3)[["code", "revenue_yoy", "profit_yoy"]].copy()
    l3["rf"] = 1 + l3["revenue_yoy"] / 100
    l3["pf"] = 1 + l3["profit_yoy"] / 100
    g3 = l3.groupby("code")
    prod, cnt = g3[["rf", "pf"]].prod(min_count=1), g3[["rf", "pf"]].count()
    agg["rev_cagr"] = (np.power(prod["rf"].where(prod["rf"] > 0),
                                1 / cnt["rf"].where(cnt["rf"] > 0)) - 1) * 100
    agg["profit_cagr"] = (np.power(prod["pf"].where(prod["pf"] > 0),
                                   1 / cnt["pf"].where(cnt["pf"] > 0)) - 1) * 100
    last = df.groupby("code").tail(1).set_index("code")
    agg["debt_ratio"] = last["debt_ratio"]
    agg["np_deducted"] = last["np_deducted"]
    return agg


def _load_heat(db: Session, as_of: date | None = None) -> dict[str, int]:
    ld, params = _ld_sql(as_of)
    sql = text(f"""
        WITH ld AS ({ld}),
        q AS (
          SELECT code, trade_date, close,
                 lag(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev
          FROM daily_quotes
          WHERE trade_date >= (SELECT d FROM ld) - INTERVAL '20 days'
            AND trade_date <= (SELECT d FROM ld)
        ),
        l AS (
          SELECT code, trade_date, close / NULLIF(prev, 0) - 1 AS chg,
                 CASE WHEN code LIKE '300%' OR code LIKE '301%' OR code LIKE '688%'
                      THEN 0.195 ELSE 0.095 END AS rate
          FROM q
        ),
        d3 AS (SELECT DISTINCT trade_date FROM q
               ORDER BY trade_date DESC LIMIT 3)
        SELECT s.industry, count(*)::int AS n
        FROM l JOIN stocks s ON s.code = l.code
        WHERE l.trade_date IN (SELECT trade_date FROM d3)
          AND l.chg >= l.rate - 0.005
        GROUP BY s.industry
    """)
    return {r[0]: int(r[1]) for r in db.execute(sql, params).all() if r[0]}


def _load_zones(db: Session,
                as_of: date | None = None) -> dict[str, tuple[float | None, float | None]]:
    where, params = ("WHERE calc_date <= :as_of", {"as_of": as_of}) if as_of \
        else ("", {})
    sql = text(f"""
        SELECT DISTINCT ON (code) code, zone_low, zone_high
        FROM safety_zones {where} ORDER BY code, calc_date DESC
    """)
    return {r[0]: (float(r[1]) if r[1] is not None else None,
                   float(r[2]) if r[2] is not None else None)
            for r in db.execute(sql, params).all()}


def compute_picks(db: Session, limit: int = 3, refresh: bool = False,
                  as_of: date | None = None) -> list[dict]:
    """as_of 指定后按该交易日的历史快照重算（用于回填，避免前视偏差）。"""
    latest = as_of or db.execute(
        text("SELECT max(trade_date) FROM daily_quotes")).scalar()
    if latest is None:
        return []
    key = f"{latest}:{limit}"
    hit = _CACHE.get(key)
    if not refresh and hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    stocks = pd.DataFrame(db.execute(text(
        "SELECT code, name, industry FROM stocks "
        "WHERE code NOT LIKE '4%' AND code NOT LIKE '8%' AND code NOT LIKE '92%'"
    )).mappings().all())
    feats = _load_features(db, as_of)
    hist = _load_history(db, as_of)
    fin = _load_fin(db, latest)
    if stocks.empty or feats.empty or fin.empty:
        return []

    df = (stocks.merge(feats, on="code", how="inner")
                .merge(hist, on="code", how="left")
                .merge(fin, on="code", how="left"))
    df = df[df["last_date"] == pd.Timestamp(latest)]

    ok = (
        ~df["name"].fillna("").str.contains("ST")
        & (df["hist"].fillna(0) >= MIN_HISTORY)
        & (df["roe_avg"] >= ROE_MIN)
        & (df["roe_n"] >= 4)
        & (df["debt_ratio"].isna() | (df["debt_ratio"] <= DEBT_MAX))
        & (df["np_deducted"].isna() | (df["np_deducted"] > 0))
        & (df["ret60"].isna() | (df["ret60"] <= RET60_MAX))
        & (df["amt20"].fillna(0) >= MIN_AMOUNT)
        & df["ma250"].notna() & (df["close"] > 0)
    )
    pool = df[ok].copy()
    if pool.empty:
        return []

    # 池内分位打分，避免利润增速封顶造成的并列（并列会让排序退化为按代码升序）
    pool["score"] = (0.7 * pool["profit_cagr"].fillna(0).rank(pct=True)
                     + 0.3 * pool["rev_cagr"].fillna(0).rank(pct=True)) * 100
    pool = pool.sort_values(["score", "profit_cagr"], ascending=False)

    heat = _load_heat(db, as_of)
    zones = _load_zones(db, as_of)

    picks: list[dict] = []
    ind_cnt: dict[str, int] = {}
    for _, r in pool.iterrows():
        ind = r["industry"] if isinstance(r["industry"], str) else None
        if ind and ind_cnt.get(ind, 0) >= 2:
            continue
        if ind:
            ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
        picks.append(_row_to_pick(r, len(picks) + 1, heat, zones))
        if len(picks) >= limit:
            break

    _CACHE[key] = (time.time(), picks)
    log.info("computed %d picks for %s, pool=%d", len(picks), latest, len(pool))
    return picks


def _row_to_pick(r, rank: int, heat: dict[str, int], zones) -> dict:
    price = float(r["close"])
    prev = float(r["prev_close"]) if pd.notna(r["prev_close"]) else None
    change_pct = round((price / prev - 1) * 100, 2) if prev else None

    zl, zh = zones.get(r["code"], (None, None))
    if zl is None or zh is None:
        zl = float(r["ma250"]) * 0.90 if pd.notna(r["ma250"]) else None
        zh = float(r["ma250"]) * 1.10 if pd.notna(r["ma250"]) else None
    if zl is None or zh is None:
        zone_status = None
    elif price > zh:
        zone_status = "above"
    elif price < zl:
        zone_status = "below"
    else:
        zone_status = "in"

    slope = float(r["slope"]) if pd.notna(r["slope"]) else None
    slope_up = slope is not None and slope > 0
    ind = r["industry"] if isinstance(r["industry"], str) else None
    heat_n = heat.get(ind, 0) if ind else 0

    tags: list[str] = []
    if pd.notna(r.get("profit_cagr")):
        tags.append(f"利润增速 {r['profit_cagr']:.0f}%")
    if pd.notna(r.get("rev_cagr")):
        tags.append(f"营收增速 {r['rev_cagr']:.0f}%")
    tags.append(f"ROE {r['roe_avg']:.1f}%")
    if zone_status == "above":
        tags.append("高于区间上沿（追高）")
    elif zone_status == "below":
        tags.append("低于区间下沿")
    if slope is not None:
        tags.append("MA20 向上" if slope_up else "MA20 向下")
    if heat_n:
        tags.append(f"行业3日涨停 {heat_n} 家")

    return {
        "code": r["code"],
        "name": r["name"],
        "industry": ind,
        "rank": rank,
        "score": round(float(r["score"]), 2),
        "price": round(price, 2),
        "change_pct": change_pct,
        "profit_growth": round(float(r["profit_cagr"]), 1)
        if pd.notna(r.get("profit_cagr")) else None,
        "revenue_growth": round(float(r["rev_cagr"]), 1)
        if pd.notna(r.get("rev_cagr")) else None,
        "roe_avg": round(float(r["roe_avg"]), 1) if pd.notna(r["roe_avg"]) else None,
        "debt_ratio": round(float(r["debt_ratio"]), 1)
        if pd.notna(r.get("debt_ratio")) else None,
        "zone_low": round(zl, 2) if zl is not None else None,
        "zone_high": round(zh, 2) if zh is not None else None,
        "zone_status": zone_status,
        "ma20_slope_pct": round(slope, 3) if slope is not None else None,
        "ma20_slope_up": slope_up,
        "ind_heat3": heat_n,
        "ret20": round(float(r["ret20"]) * 100, 2) if pd.notna(r["ret20"]) else None,
        "ret60": round(float(r["ret60"]) * 100, 2) if pd.notna(r["ret60"]) else None,
        "tags": tags,
    }


REGIME_LABEL = {"offensive": "进攻期", "neutral": "震荡期", "defensive": "防守期"}
REGIME_NOTE = {
    "offensive": "市场普涨放量，低估值策略通常跑输，别指望短期弹性",
    "neutral": "环境中性，个股业绩增速是主要变量",
    "defensive": "市场弱势缩量，低估值修复逻辑相对占优，但需要耐心",
}


def compute_context(db: Session) -> dict:
    """市场周期 + 行业周期，只做展示，不参与筛选。

    回测（2025-02~2026-10，周频88期）：行业趋势、行业资金流入、行业涨停热度
    作为门槛均为负贡献；市场 regime 仅用于提示策略适配度。
    """
    latest = db.execute(text("SELECT max(trade_date) FROM daily_quotes")).scalar()
    if latest is None:
        return {}
    key = str(latest)
    hit = _CTX.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    sql = text("""
        WITH ld AS (SELECT max(trade_date) AS d FROM daily_quotes)
        SELECT q.code, q.trade_date, q.close::float8 AS close,
               q.amount::float8 AS amount,
               lag(q.close) OVER w AS prev,
               lag(q.close, 20) OVER w AS close20,
               s.industry
        FROM daily_quotes q
        JOIN stocks s ON s.code = q.code
        WHERE q.trade_date >= (SELECT d FROM ld) - INTERVAL '60 days'
        WINDOW w AS (PARTITION BY q.code ORDER BY q.trade_date)
    """)
    df = pd.DataFrame(db.execute(sql).mappings().all())
    if df.empty:
        return {}
    df = df[~df["code"].str.startswith(("4", "8", "92"))]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("close", "amount", "prev", "close20"):
        df[c] = df[c].astype(float)
    df["chg"] = df["close"] / df["prev"] - 1
    df["ret20"] = df["close"] / df["close20"] - 1
    df["rate"] = np.where(df["code"].str.match(r"^(300|301|688|689)"),
                          0.195, 0.095)
    df["up"] = (df["chg"] > 0).astype(float)
    df["lu"] = df["chg"] >= df["rate"] - 0.005
    df["ld"] = df["chg"] <= -(df["rate"] - 0.005)

    mkt = df.groupby("trade_date").agg(
        adv=("up", "mean"), lu=("lu", "sum"), ld=("ld", "sum"),
        med20=("ret20", "median"), amt=("amount", "sum"), n=("code", "size"))
    mkt["amt_ratio"] = mkt["amt"] / mkt["amt"].rolling(20).mean().shift(1)
    mkt["n_ratio"] = mkt["n"] / mkt["n"].rolling(20).mean().shift(1)
    last = mkt.iloc[-1]
    adv = float(last["adv"])
    amt_ratio = float(last["amt_ratio"]) if pd.notna(last["amt_ratio"]) else 1.0
    med20 = float(last["med20"])
    if adv >= 0.55 and amt_ratio >= 1.05:
        regime = "offensive"
    elif adv <= 0.40 or (med20 < -0.03 and amt_ratio < 0.9):
        regime = "defensive"
    else:
        regime = "neutral"

    ind = df[df["industry"].notna()]
    i_ret = ind.groupby(["trade_date", "industry"])["ret20"].median().unstack()
    i_amt = ind.groupby(["trade_date", "industry"])["amount"].sum().unstack()
    i_lu = ind.groupby(["trade_date", "industry"])["lu"].sum().unstack()
    i_cnt = ind.groupby(["trade_date", "industry"])["code"].size().unstack()
    share = i_amt.div(mkt["amt"], axis=0)
    flow = share / share.rolling(20).mean().shift(1)
    base_cnt = i_cnt.iloc[:-1].tail(20).mean()
    ps_amt = i_amt / i_cnt
    base_ps = ps_amt.iloc[:-1].tail(20).mean()

    industries: dict[str, dict] = {}
    for name in i_ret.columns:
        r = i_ret.iloc[-1].get(name)
        fl = flow.iloc[-1].get(name) if not flow.empty else np.nan
        ht = i_lu.iloc[-1].get(name)
        # 样本数减少且单只成交额同步骤降时，说明缺的是大票，资金占比失真
        cnt_now = i_cnt.iloc[-1].get(name)
        cr = cnt_now / base_cnt.get(name, np.nan)
        ps_ratio = ((i_amt.iloc[-1].get(name) / cnt_now) / base_ps.get(name, np.nan)
                    if cnt_now else np.nan)
        reliable = not (pd.notna(cr) and cr < 0.95
                        and pd.notna(ps_ratio) and ps_ratio < 0.75)
        industries[name] = {
            "ret20": round(float(r) * 100, 2) if pd.notna(r) else None,
            "flow": round(float(fl), 2) if (pd.notna(fl) and reliable) else None,
            "heat": int(ht) if pd.notna(ht) else 0,
        }

    n_avg = float(mkt["n"].iloc[-21:-1].mean()) if len(mkt) > 21 else float(mkt["n"].iloc[:-1].mean())
    coverage = round(float(last["n"]) / n_avg * 100, 1) if n_avg else None
    warning = None
    if coverage is not None and coverage < 90:
        warning = (f"当日仅同步 {int(last['n'])} 只（近20日均 {int(n_avg)} 只），"
                   "涨跌家数与行业资金统计可能失真")

    out = {
        "date": str(latest),
        "market": {
            "regime": regime,
            "label": REGIME_LABEL[regime],
            "adv_ratio": round(adv * 100, 1),
            "limit_up": int(last["lu"]),
            "limit_down": int(last["ld"]),
            "median_ret20": round(med20 * 100, 2),
            "amount_ratio": round(amt_ratio, 2),
            "amount_yi": round(float(last["amt"]) / 1e8),
            "coverage": coverage,
            "data_warning": warning,
            "note": REGIME_NOTE[regime],
        },
        "industries": industries,
    }
    _CTX[key] = (time.time(), out)
    return out
