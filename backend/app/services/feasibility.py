"""预测可行性检验：特征与未来超额收益的横截面 IC / 分层单调性。

不做点预测，只回答"现有数据是否具备可用的排序能力"：
- 每日横截面 Spearman IC 的均值、标准差、ICIR、t 值
- 按特征分十档后的平均超额收益差（top - bottom）
- 覆盖率与样本天数

结论口径：|mean_ic| >= 0.02 且 |t| >= 2 且分层方向一致，才视为可用信号。
"""
import logging
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

FEATURE_COLUMNS = [
    "pct_chg", "ret5", "ret20", "ret60",
    "bias20", "ma20_slope_pct", "vol20", "vol60",
    "amt_ratio", "pos60", "limit_up", "streak",
    "pe", "pb", "pe_pct5y",
    "roe_avg", "profit_cagr", "rev_cagr", "debt_ratio",
    "mf_main_net_pct", "mf_main_net_5d", "ind_heat3",
]

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 600.0


def _load(db: Session, window: int, days: int) -> pd.DataFrame:
    latest = db.execute(text(
        "SELECT max(as_of) FROM label_fwd WHERE horizon = :w"), {"w": window}
    ).scalar()
    if not latest:
        return pd.DataFrame()
    start = latest - timedelta(days=int(days * 1.6))
    cols = ", ".join(f"f.{c}" for c in FEATURE_COLUMNS)
    sql = text(f"""
        SELECT f.as_of, f.code, l.excess, {cols}
        FROM feat_stock_daily f
        JOIN label_fwd l ON l.code = f.code AND l.as_of = f.as_of
        WHERE l.horizon = :w AND f.as_of BETWEEN :start AND :latest
    """)
    df = pd.DataFrame(db.execute(sql, {"w": window, "start": start,
                                       "latest": latest}).mappings().all())
    if df.empty:
        return df
    df["excess"] = pd.to_numeric(df["excess"], errors="coerce")
    for c in FEATURE_COLUMNS:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _ic_series(sub: pd.DataFrame) -> pd.Series:
    g = sub[["as_of", "rx", "ry"]].groupby("as_of")
    return g.apply(lambda x: x["rx"].corr(x["ry"]) if len(x) >= 30 else np.nan)


def _decile_spread(sub: pd.DataFrame) -> tuple[float | None, list]:
    s = sub.copy()
    s["dec"] = s.groupby("as_of")["rx"].transform(
        lambda x: pd.qcut(x.rank(method="first"), 10, labels=False,
                          duplicates="drop"))
    per_day = s.groupby(["as_of", "dec"])["excess"].mean().unstack()
    avg = per_day.mean()
    if avg.empty:
        return None, []
    spread = float(avg.iloc[-1] - avg.iloc[0]) * 100
    return spread, [round(float(v) * 100, 2) for v in avg.tolist()]


def run_feasibility(db: Session, window: int = 20,
                    days: int = 200) -> dict:
    key = (window, days)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    df = _load(db, window, days)
    if df.empty:
        return {"window": window, "days": days, "sample_days": 0,
                "features": [], "usable_count": 0,
                "reason": "特征或标签为空，请先执行 build-features / build-labels"}

    df = df.dropna(subset=["excess"])
    df["ry"] = df.groupby("as_of")["excess"].rank()

    features, usable = [], 0
    for col in FEATURE_COLUMNS:
        sub = df[["as_of", col, "excess", "ry"]].dropna()
        if sub.empty or sub[col].nunique() < 3:
            continue
        sub["rx"] = sub.groupby("as_of")[col].rank()
        ic = _ic_series(sub).dropna()
        if len(ic) < 10:
            continue
        mean_ic = float(ic.mean())
        std_ic = float(ic.std())
        t = mean_ic / std_ic * np.sqrt(len(ic)) if std_ic > 0 else 0.0
        spread, deciles = _decile_spread(sub)
        ok = (abs(mean_ic) >= 0.02 and abs(t) >= 2.0
              and spread is not None and spread * mean_ic > 0)
        if ok:
            usable += 1
        features.append({
            "feature": col,
            "mean_ic": round(mean_ic, 4),
            "ic_std": round(std_ic, 4),
            "icir": round(mean_ic / std_ic, 3) if std_ic > 0 else None,
            "t": round(float(t), 2),
            "positive_rate": round(float((ic > 0).mean()), 3),
            "top_bottom_spread_pct": spread,
            "deciles_pct": deciles,
            "usable": bool(ok),
        })

    features.sort(key=lambda x: abs(x["mean_ic"]), reverse=True)
    result = {
        "window": window,
        "days": days,
        "sample_days": int(df["as_of"].nunique()),
        "avg_stocks": int(round(df.groupby("as_of").size().mean())),
        "usable_count": usable,
        "verdict": ("具备可用排序信号" if usable >= 3 else
                    "信号偏弱，建议先补数据维度"),
        "features": features,
        "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _CACHE[key] = (time.time(), result)
    return result


def coverage(db: Session) -> dict:
    feat = db.execute(text("""
        SELECT as_of, count(*) AS rows, count(DISTINCT code) AS codes
        FROM feat_stock_daily
        WHERE as_of = (SELECT max(as_of) FROM feat_stock_daily)
        GROUP BY as_of
    """)).mappings().all()
    label = db.execute(text("""
        SELECT horizon, as_of, count(*) AS rows, count(DISTINCT code) AS codes
        FROM label_fwd
        WHERE (horizon, as_of) IN (
            SELECT horizon, max(as_of) FROM label_fwd GROUP BY horizon)
        GROUP BY horizon, as_of ORDER BY horizon
    """)).mappings().all()
    return {
        "features": [dict(r) for r in feat],
        "labels": [dict(r) for r in label],
    }
