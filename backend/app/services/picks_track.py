"""选股落库、前向收益回填与绩效评估。

评估口径：
- 组合收益 = 当日入选标的等权
- 基准收益 = 同日同窗口的指数收益
- 胜率 / 盈亏比 / t 值 / 净超额只用**非重叠样本**（每 window 个交易日取 1 期），
  避免重叠窗口虚高 t 值；成本按每期双边 0.13% 扣一次
"""
import logging
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import PickSnapshot
from .pick import compute_picks

log = logging.getLogger(__name__)

# 双边成本：佣金 0.025% ×2 + 卖出印花税 0.05% + 滑点 0.03% ×2
COST_COMMISSION = 0.00025
COST_STAMP = 0.0005
COST_SLIPPAGE = 0.0003
COST_ROUND_TRIP = COST_COMMISSION * 2 + COST_STAMP + COST_SLIPPAGE * 2

BENCHMARK_NAMES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000"}
FWD_WINDOWS = (5, 10, 20, 60)

_SNAPSHOT_COLS = ("rank", "score", "close", "industry", "profit_growth",
                  "revenue_growth", "roe_avg", "debt_ratio", "zone_low",
                  "zone_high", "zone_status", "ma20_slope_up", "ret20",
                  "ret60", "tags", "source")


def _snapshot_row(p: dict, batch_date: date, source: str) -> dict:
    """compute_picks 的输出字段名与 picks 表列名对齐（price → close）。"""
    return {
        "batch_date": batch_date,
        "code": p["code"],
        "rank": p.get("rank"),
        "score": p.get("score"),
        "close": p.get("price"),
        "industry": p.get("industry"),
        "profit_growth": p.get("profit_growth"),
        "revenue_growth": p.get("revenue_growth"),
        "roe_avg": p.get("roe_avg"),
        "debt_ratio": p.get("debt_ratio"),
        "zone_low": p.get("zone_low"),
        "zone_high": p.get("zone_high"),
        "zone_status": p.get("zone_status"),
        "ma20_slope_up": p.get("ma20_slope_up"),
        "ret20": p.get("ret20"),
        "ret60": p.get("ret60"),
        "tags": p.get("tags") or [],
        "source": source,
    }


def _pct(v) -> float | None:
    """小数收益 → 百分比，保留 2 位。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v) * 100, 2)


def _latest_trade_date(db: Session) -> date | None:
    return db.execute(text("SELECT max(trade_date) FROM daily_quotes")).scalar()


def save_snapshot(db: Session, batch_date: date | None = None, limit: int = 3,
                  source: str = "official", as_of: date | None = None) -> dict:
    """把当日选股结果落库。official 覆盖同键的 backfill 记录。"""
    if batch_date is None:
        batch_date = as_of or _latest_trade_date(db)
    if batch_date is None:
        return {"batch_date": None, "saved": 0, "reason": "no quotes"}
    picks = compute_picks(db, limit=limit, refresh=True, as_of=as_of)
    if not picks:
        return {"batch_date": str(batch_date), "saved": 0, "reason": "no picks"}

    rows = [_snapshot_row(p, batch_date, source) for p in picks]
    stmt = insert(PickSnapshot).values(rows)
    if source == "official":
        stmt = stmt.on_conflict_do_update(
            index_elements=["batch_date", "code"],
            set_={c: getattr(stmt.excluded, c) for c in _SNAPSHOT_COLS}
            | {"updated_at": func.now()},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["batch_date", "code"])
    db.execute(stmt)
    db.commit()
    log.info("picks snapshot saved: %s, %d rows (source=%s)",
             batch_date, len(rows), source)
    return {"batch_date": str(batch_date), "saved": len(rows), "source": source}


def backfill_snapshots(db: Session, days: int = 250, limit: int = 3) -> dict:
    """按历史交易日逐日重算并落库（避免前视偏差；仍含幸存者偏差）。"""
    dates = [r[0] for r in db.execute(text(
        "SELECT DISTINCT trade_date FROM daily_quotes "
        "ORDER BY trade_date DESC LIMIT :n"), {"n": days}).all()]
    dates.reverse()
    total = failed = 0
    for d in dates:
        try:
            total += save_snapshot(db, batch_date=d, limit=limit,
                                   source="backfill", as_of=d)["saved"]
        except Exception as e:
            db.rollback()
            failed += 1
            log.warning("backfill %s failed: %s", d, str(e)[:120])
    log.info("picks backfill: %d days, %d rows, %d failed",
             len(dates), total, failed)
    return {"days": len(dates), "saved": total, "failed": failed}


def update_forward_returns(db: Session) -> dict:
    """用 lead() 回填 T+5/10/20/60 收益，已存在的不覆盖。"""
    sql = text("""
        WITH q AS (
          SELECT code, trade_date, close::float8 AS close,
                 lead(close, 5)  OVER (PARTITION BY code ORDER BY trade_date) AS c5,
                 lead(close, 10) OVER (PARTITION BY code ORDER BY trade_date) AS c10,
                 lead(close, 20) OVER (PARTITION BY code ORDER BY trade_date) AS c20,
                 lead(close, 60) OVER (PARTITION BY code ORDER BY trade_date) AS c60
          FROM daily_quotes
        )
        UPDATE picks p SET
          fwd_ret_5  = COALESCE(ROUND((q.c5  / NULLIF(q.close, 0) - 1)::numeric, 4), p.fwd_ret_5),
          fwd_ret_10 = COALESCE(ROUND((q.c10 / NULLIF(q.close, 0) - 1)::numeric, 4), p.fwd_ret_10),
          fwd_ret_20 = COALESCE(ROUND((q.c20 / NULLIF(q.close, 0) - 1)::numeric, 4), p.fwd_ret_20),
          fwd_ret_60 = COALESCE(ROUND((q.c60 / NULLIF(q.close, 0) - 1)::numeric, 4), p.fwd_ret_60),
          updated_at = now()
        FROM q
        WHERE p.code = q.code AND p.batch_date = q.trade_date
          AND (p.fwd_ret_5 IS NULL OR p.fwd_ret_10 IS NULL
               OR p.fwd_ret_20 IS NULL OR p.fwd_ret_60 IS NULL)
    """)
    result = db.execute(sql)
    db.commit()
    n = result.rowcount or 0
    log.info("forward returns updated: %d rows", n)
    return {"updated": n}


def _benchmark_curve(db: Session, benchmark: str, window: int) -> pd.Series:
    sql = text("""
        WITH q AS (
          SELECT trade_date, close::float8 AS close,
                 lead(close, :w) OVER (ORDER BY trade_date) AS fwd
          FROM index_daily WHERE code = :bm
        )
        SELECT trade_date, fwd / NULLIF(close, 0) - 1 AS ret
        FROM q WHERE fwd IS NOT NULL
    """)
    rows = db.execute(sql, {"w": window, "bm": benchmark}).all()
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({r[0]: float(r[1]) for r in rows}, dtype=float)
    return s.sort_index()


def compute_performance(db: Session, benchmark: str = "000852",
                        window: int = 20) -> dict:
    """策略 vs 基准的绩效统计。返回的收益率均为百分数。"""
    if window not in FWD_WINDOWS:
        window = 20
    col = f"fwd_ret_{window}"

    rows = db.execute(text(
        f"SELECT batch_date, code, {col}::float8 AS ret FROM picks "
        f"WHERE {col} IS NOT NULL ORDER BY batch_date"
    )).mappings().all()
    if not rows:
        return {
            "benchmark": benchmark,
            "benchmark_name": BENCHMARK_NAMES.get(benchmark, benchmark),
            "window": window,
            "cost_round_trip_pct": _pct(COST_ROUND_TRIP),
            "summary": None,
            "curve": [],
            "reason": "no snapshots with forward returns",
        }

    df = pd.DataFrame(rows)
    port = df.groupby("batch_date")["ret"].mean().sort_index()
    bench = _benchmark_curve(db, benchmark, window)
    idx = port.index.intersection(bench.index)
    if len(idx) == 0:
        return {
            "benchmark": benchmark,
            "benchmark_name": BENCHMARK_NAMES.get(benchmark, benchmark),
            "window": window,
            "cost_round_trip_pct": _pct(COST_ROUND_TRIP),
            "summary": None,
            "curve": [],
            "reason": "benchmark data missing",
        }

    port, bench = port.loc[idx], bench.loc[idx]
    excess_gross = port - bench

    # 非重叠抽样：每 window 个交易日取 1 期
    sampled = list(port.index[::window])
    p_ns = port.loc[sampled]
    b_ns = bench.loc[sampled]
    excess_ns = p_ns - b_ns
    excess_net = excess_ns - COST_ROUND_TRIP

    net_ns = p_ns.values - COST_ROUND_TRIP
    cum_strategy = float(np.prod(1 + net_ns) - 1)
    cum_bench = float(np.prod(1 + b_ns.values) - 1)
    cum_net = ((1 + cum_strategy) / (1 + cum_bench) - 1) if cum_bench > -1 else None
    equity = np.cumprod(1 + net_ns)
    peak = np.maximum.accumulate(equity)
    max_dd = float(np.min(equity / peak - 1)) if len(equity) else 0.0

    wins = excess_net[excess_net > 0]
    losses = excess_net[excess_net <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    payoff = round(avg_win / abs(avg_loss), 2) if avg_loss else None
    n = len(excess_net)
    t_stat = None
    if n >= 2 and excess_net.std(ddof=1) > 0:
        t_stat = round(float(excess_net.mean() / excess_net.std(ddof=1)
                             * np.sqrt(n)), 2)

    curve = []
    eq_s, eq_b = 1.0, 1.0
    for d in sampled:
        eq_s *= 1 + float(port.loc[d]) - COST_ROUND_TRIP
        eq_b *= 1 + float(bench.loc[d])
        curve.append({
            "date": str(d),
            "strategy": _pct(eq_s - 1),
            "bench": _pct(eq_b - 1),
            "excess": _pct(eq_s - eq_b),
        })

    summary = {
        "raw_periods": int(len(port)),
        "effective_periods": int(n),
        "start": str(sampled[0]),
        "end": str(sampled[-1]),
        "win_rate": round(float((excess_net > 0).mean()) * 100, 2) if n else None,
        "avg_excess_gross": _pct(float(excess_gross.loc[sampled].mean())),
        "avg_excess_net": _pct(float(excess_net.mean())),
        "avg_port": _pct(float(p_ns.mean())),
        "avg_bench": _pct(float(b_ns.mean())),
        "avg_win": _pct(avg_win),
        "avg_loss": _pct(avg_loss),
        "payoff": payoff,
        "cum_strategy": _pct(cum_strategy),
        "cum_bench": _pct(cum_bench),
        "cum_excess_net": _pct(cum_net),
        "max_drawdown": _pct(max_dd),
        "t_stat": t_stat,
        "t_note": "t 值基于非重叠样本；样本期数少时不足以判定显著",
    }
    return {
        "benchmark": benchmark,
        "benchmark_name": BENCHMARK_NAMES.get(benchmark, benchmark),
        "window": window,
        "cost_round_trip_pct": _pct(COST_ROUND_TRIP),
        "summary": summary,
        "curve": curve,
    }
