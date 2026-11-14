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

# 等权合成分的候选因子：IC 检验中稳定为负（低值更优），合成时取反向排名
COMPOSITE_FACTORS = ("vol60", "pos60", "pb", "ret20")

# 否决项：剔除「趋势下行且贴近 60 日低位」「利润大幅下滑」的价值陷阱
VETO_COLUMNS = ("pos60", "ma20_slope_pct", "profit_cagr")
VETO_POS60_MAX = 0.30      # 位于近 60 日价格区间下 30%
VETO_SLOPE_MAX = 0.0       # MA20 斜率不为正
VETO_PROFIT_MIN = -30.0    # 利润增速低于 -30%（%）视为质量恶化
BAND = (0.60, 0.80)        # 多头取分数 60%~80% 分位，避开最极端分档


def _load(db: Session, window: int, days: int,
          columns: list[str] | None = None) -> pd.DataFrame:
    latest = db.execute(text(
        "SELECT max(as_of) FROM label_fwd WHERE horizon = :w"), {"w": window}
    ).scalar()
    if not latest:
        return pd.DataFrame()
    start = latest - timedelta(days=int(days * 1.6))
    use_cols = columns or FEATURE_COLUMNS
    cols = ", ".join(f"f.{c}" for c in use_cols)
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
    for c in use_cols:
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


def run_composite(db: Session, window: int = 20, days: int = 200,
                  factors: tuple[str, ...] = COMPOSITE_FACTORS,
                  veto: bool = False) -> dict:
    """等权合成分：各因子取反向截面排名后等权平均，检验分层单调性。

    结论：多头端无稳定 alpha（500 日多空差 0.82%，季度间反号），
    该合成分仅用于识别最低分位做风险规避，见 run_avoid_list。
    """
    key = ("composite", window, days, factors, veto)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    use = list(factors)
    sel = list(dict.fromkeys(use + list(VETO_COLUMNS)))
    latest = db.execute(text(
        "SELECT max(as_of) FROM label_fwd WHERE horizon = :w"), {"w": window}
    ).scalar()
    if not latest:
        return {"window": window, "factors": use, "sample_days": 0,
                "reason": "特征或标签为空，请先执行 build-features / build-labels"}
    start = latest - timedelta(days=int(days * 1.6))
    dates = [r[0] for r in db.execute(text(f"""
        SELECT DISTINCT f.as_of FROM feat_stock_daily f
        JOIN label_fwd l ON l.code = f.code AND l.as_of = f.as_of
        WHERE l.horizon = :w AND f.as_of BETWEEN :start AND :latest
        ORDER BY f.as_of
    """), {"w": window, "start": start, "latest": latest}).all()]
    if not dates:
        return {"window": window, "factors": use, "sample_days": 0,
                "reason": "区间内没有可用的特征与标签交集"}

    # 分块流式读取，避免一次性装载 50 万行以上导致容器 OOM
    cols = ", ".join(f"f.{c}" for c in sel)
    daily = []
    for i in range(0, len(dates), 40):
        d0 = dates[i]
        d1 = dates[min(i + 39, len(dates) - 1)]
        rows = db.execute(text(f"""
            SELECT f.as_of, {cols}, l.excess
            FROM feat_stock_daily f
            JOIN label_fwd l ON l.code = f.code AND l.as_of = f.as_of
            WHERE l.horizon = :w AND f.as_of BETWEEN :d0 AND :d1
        """), {"w": window, "d0": d0, "d1": d1}).all()
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["as_of"] + sel + ["excess"])
        for c in sel + ["excess"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["excess"])
        for as_of, g in df.groupby("as_of", sort=True):
            # 否决项：先剔除价值陷阱，再在剩余可交易池内排序（与线上选股口径一致）
            vetoed = 0.0
            if veto:
                keep = pd.Series(True, index=g.index)
                if "pos60" in g.columns and "ma20_slope_pct" in g.columns:
                    keep &= ~((g["pos60"] <= VETO_POS60_MAX)
                              & (g["ma20_slope_pct"] <= VETO_SLOPE_MAX))
                keep &= ~(g["profit_cagr"] <= VETO_PROFIT_MIN)
                vetoed = 1.0 - float(keep.mean()) if len(g) else 0.0
                g = g[keep]
            if len(g) < 30:
                continue
            # 这批因子已知方向为负：排名越低（低波动/低估值/低涨幅）得分越高
            score = 1 - pd.concat(
                [g[f].rank(pct=True) for f in use], axis=1).mean(axis=1)
            g = g.assign(score=score).dropna(subset=["score"])
            n = len(g)
            if n < 30:
                continue
            rx = g["score"].rank(method="first").to_numpy(dtype=float)
            ry = g["excess"].rank().to_numpy(dtype=float)
            ex = g["excess"].to_numpy(dtype=float)
            ic = (float(np.corrcoef(rx, ry)[0, 1])
                  if rx.std() > 0 and ry.std() > 0 else np.nan)
            order = np.argsort(rx, kind="stable")
            bounds = np.linspace(0, n, 11).astype(int)
            dec = np.array([ex[order[bounds[k]:bounds[k + 1]]].mean()
                            for k in range(10)])
            # 多头带取 60%~80% 分位（D7~D8），空头取最低 20%
            band_ret = float(ex[order[bounds[6]:bounds[8]]].mean())
            short_ret = float(ex[order[bounds[0]:bounds[2]]].mean())
            top_ret = float(ex[order[bounds[9]:bounds[10]]].mean())
            fic = {}
            for f in use:
                fr = g[f].to_numpy(dtype=float)
                ok = ~np.isnan(fr)
                if ok.sum() >= 30 and fr[ok].std() > 0:
                    fic[f] = float(np.corrcoef(fr[ok], ry[ok])[0, 1])
            daily.append((as_of, n, ic, dec, fic, band_ret, short_ret,
                          top_ret, vetoed))
        del df

    daily = [d for d in daily if not np.isnan(d[2])]
    if not daily:
        return {"window": window, "factors": use, "sample_days": 0,
                "reason": "截面样本不足 30 只，无法计算 IC"}

    ics = np.array([d[2] for d in daily])
    mean_ic = float(ics.mean())
    std_ic = float(ics.std(ddof=1)) if len(ics) > 1 else 0.0
    t = mean_ic / std_ic * np.sqrt(len(ics)) if std_ic > 0 else 0.0
    dec_avg = np.array([d[3] for d in daily]).mean(axis=0)
    deciles = [round(float(v) * 100, 2) for v in dec_avg]
    spread = round(float(dec_avg[-1] - dec_avg[0]) * 100, 2)
    band_mean = float(np.mean([d[5] for d in daily]))
    short_mean = float(np.mean([d[6] for d in daily]))
    band_spread = round((band_mean - short_mean) * 100, 2)
    dk = pd.Series(dec_avg).rank()
    mono = float(dk.corr(pd.Series(range(10)).rank()))

    components = []
    for f in use:
        vals = [d[4][f] for d in daily if f in d[4]]
        components.append({"factor": f,
                           "mean_ic": round(float(np.mean(vals)), 4) if vals else None})

    # 分季度稳定性
    qmap: dict[str, list] = {}
    for d in daily:
        qmap.setdefault(f"{d[0].year}Q{(d[0].month - 1) // 3 + 1}", []).append(d)
    by_quarter = []
    for q in sorted(qmap):
        grp = qmap[q]
        qic = np.array([d[2] for d in grp])
        qdec = np.array([d[3] for d in grp]).mean(axis=0)
        qspread = round(float(qdec[-1] - qdec[0]) * 100, 2)
        qband = float(np.mean([d[5] for d in grp]))
        qshort = float(np.mean([d[6] for d in grp]))
        qmono = float(pd.Series(qdec).rank().corr(pd.Series(range(10)).rank()))
        by_quarter.append({
            "quarter": q,
            "days": len(grp),
            "avg_stocks": int(round(np.mean([d[1] for d in grp]))),
            "mean_ic": round(float(qic.mean()), 4),
            "t": round(float(qic.mean() / qic.std(ddof=1) * np.sqrt(len(qic))), 2)
                 if len(qic) > 1 and qic.std(ddof=1) > 0 else 0.0,
            "positive_rate": round(float((qic > 0).mean()), 3),
            "monotonicity": round(qmono, 3),
            "long_short_pct": qspread,
            "band_spread_pct": round((qband - qshort) * 100, 2),
        })

    # 非重叠采样：每 window 个交易日取一个
    nic = np.array([d[2] for d in daily[::window]])
    nmean = float(nic.mean()) if len(nic) else 0.0
    nstd = float(nic.std(ddof=1)) if len(nic) > 1 else 0.0
    non_overlap = {
        "samples": int(len(nic)),
        "mean_ic": round(nmean, 4),
        "t": round(nmean / nstd * np.sqrt(len(nic)), 2) if nstd > 0 else 0.0,
    }

    ok = (mono >= 0.8 and band_spread > 0
          and abs(mean_ic) >= 0.02 and abs(t) >= 2.0)
    result = {
        "window": window,
        "days": days,
        "factors": use,
        "weight": "equal_reverse_rank",
        "veto": veto,
        "vetoed_pct": round(float(np.mean([d[8] for d in daily])) * 100, 1),
        "band_pct": [int(BAND[0] * 100), int(BAND[1] * 100)],
        "sample_days": len(daily),
        "avg_stocks": int(round(np.mean([d[1] for d in daily]))),
        "mean_ic": round(mean_ic, 4),
        "ic_std": round(std_ic, 4),
        "icir": round(mean_ic / std_ic, 3) if std_ic > 0 else None,
        "t": round(float(t), 2),
        "positive_rate": round(float((ics > 0).mean()), 3),
        "monotonicity": round(mono, 3),
        "band_spread_pct": band_spread,
        "band_mean_pct": round(band_mean * 100, 2),
        "short_mean_pct": round(short_mean * 100, 2),
        "long_short_pct": spread,
        "deciles_pct": deciles,
        "component_ic": components,
        "by_quarter": by_quarter,
        "quarters_positive": sum(1 for q in by_quarter if q["mean_ic"] > 0),
        "quarters_band_positive": sum(
            1 for q in by_quarter if q["band_spread_pct"] > 0),
        "non_overlap": non_overlap,
        "verdict": "合成分分层单调，可进入纸面跟踪" if ok else
                   "分层不单调或信号不足，需调整因子组合",
        "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _CACHE[key] = (time.time(), result)
    return result


def run_avoid_list(db: Session, as_of: date | None = None,
                   pct: float = 0.2, limit: int = 100,
                   factors: tuple[str, ...] = COMPOSITE_FACTORS) -> dict:
    """风险剔除清单：合成分最低分位（高波动 + 高位 + 高 PB + 强动量）个股。

    合成分已被验证不适合做多头 alpha，仅保留「规避端」用途：
    最低 20% 分位在 500 日样本中平均 20 日超额为负（-0.22% ~ -0.31%）。
    """
    use = list(factors)
    if as_of is None:
        as_of = db.execute(text(
            "SELECT max(as_of) FROM feat_stock_daily")).scalar()
    if not as_of:
        return {"as_of": None, "universe": 0, "count": 0, "items": []}

    cols = ", ".join(f"f.{c}" for c in use)
    rows = db.execute(text(f"""
        SELECT f.code, f.industry, f.close, {cols}, l.excess
        FROM feat_stock_daily f
        LEFT JOIN label_fwd l ON l.code = f.code AND l.as_of = f.as_of
             AND l.horizon = 20
        WHERE f.as_of = :d
    """), {"d": as_of}).mappings().all()
    if not rows:
        return {"as_of": str(as_of), "universe": 0, "count": 0, "items": []}

    df = pd.DataFrame([dict(r) for r in rows])
    for c in use + ["close", "excess"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["score"] = 1 - pd.concat(
        [df[f].rank(pct=True) for f in use], axis=1).mean(axis=1)
    df = df.dropna(subset=["score"]).sort_values("score")
    n = len(df)
    k = max(1, int(round(n * pct)))
    tail = df.head(k)

    items = []
    for r in tail.head(limit).itertuples(index=False):
        items.append({
            "code": r.code,
            "industry": r.industry,
            "close": round(float(r.close), 2) if pd.notna(r.close) else None,
            "score": round(float(r.score), 4),
            **{f: (round(float(getattr(r, f)), 4)
                  if pd.notna(getattr(r, f)) else None) for f in use},
            "fwd_excess_20d_pct": (round(float(r.excess) * 100, 2)
                                   if pd.notna(r.excess) else None),
        })
    fwd = tail["excess"].dropna()
    return {
        "as_of": str(as_of),
        "universe": n,
        "pct": pct,
        "count": k,
        "avg_score": round(float(tail["score"].mean()), 4),
        "avg_fwd_excess_20d_pct": (round(float(fwd.mean()) * 100, 2)
                                   if len(fwd) else None),
        "items": items,
    }


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
