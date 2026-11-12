"""全市场前向收益标签（沪深主板）：一次回填，所有策略共用。

- 标签与特征解耦，策略只产出 signal，不再各自回填收益
- 基准为中证1000（000852），超额 = 个股收益 - 基准收益
- fwd_max_dd 为窗口内最大回撤（相对 as_of 收盘价）
"""
import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import LabelFwd
from . import quality
from .features import resolve_as_of

log = logging.getLogger(__name__)

LABEL_VERSION = "l1"
WINDOWS = (5, 10, 20, 60)
BENCH_CODE = "000852"       # 中证1000
BACKFILL_DAYS = 250
CHUNK = 1000


def _range_start(as_of: date, days: int) -> date:
    # 交易日 → 自然日需留冗余（含窗口前推）
    return as_of - timedelta(days=int(days * 1.6) + 120)


def _price_labels(db: Session, as_of: date, start: date,
                  window: int) -> pd.DataFrame:
    sql = text(f"""
        WITH q AS (
          SELECT code, trade_date, close,
                 lead(close, {window}) OVER (
                     PARTITION BY code ORDER BY trade_date) AS fwd_close,
                 min(close) OVER (
                     PARTITION BY code ORDER BY trade_date
                     ROWS BETWEEN 1 FOLLOWING AND {window} FOLLOWING) AS min_close
          FROM daily_quotes
          WHERE trade_date BETWEEN :start AND :as_of
            AND (code LIKE '60%' OR code LIKE '00%')
        )
        SELECT code, trade_date, close,
               fwd_close / NULLIF(close, 0) - 1 AS fwd_ret,
               min_close / NULLIF(close, 0) - 1 AS fwd_max_dd
        FROM q
        WHERE fwd_close IS NOT NULL
    """)
    df = pd.DataFrame(db.execute(sql, {"start": start, "as_of": as_of})
                      .mappings().all())
    if df.empty:
        return df
    for c in ("fwd_ret", "fwd_max_dd"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _bench_labels(db: Session, as_of: date, start: date,
                  window: int) -> dict[date, float]:
    sql = text(f"""
        SELECT trade_date, close,
               lead(close, {window}) OVER (ORDER BY trade_date) AS fwd_close
        FROM index_daily
        WHERE code = :bench AND trade_date BETWEEN :start AND :as_of
    """)
    rows = db.execute(sql, {"bench": BENCH_CODE, "start": start,
                            "as_of": as_of}).mappings().all()
    out: dict[date, float] = {}
    for r in rows:
        if r["fwd_close"] is None or not r["close"]:
            continue
        out[r["trade_date"]] = float(r["fwd_close"]) / float(r["close"]) - 1
    return out


def build_labels(db: Session, as_of: date | None = None,
                 windows: tuple[int, ...] = WINDOWS,
                 days: int = BACKFILL_DAYS) -> dict:
    as_of = resolve_as_of(db, as_of)
    if not as_of:
        return {"as_of": None, "rows": 0, "reason": "no daily_quotes"}
    start = _range_start(as_of, days)

    total = 0
    per_window: dict[str, int] = {}
    for w in windows:
        df = _price_labels(db, as_of, start, w)
        if df.empty:
            continue
        bench = _bench_labels(db, as_of, start, w)
        df["bench_ret"] = df["trade_date"].map(bench)
        df["excess"] = df["fwd_ret"] - df["bench_ret"]
        df["horizon"] = w
        df["label_version"] = LABEL_VERSION

        out = df[["code", "trade_date", "horizon", "fwd_ret", "bench_ret",
                  "excess", "fwd_max_dd", "label_version"]]
        records = (out.astype(object).where(out.notna(), None)
                   .to_dict("records"))
        for i in range(0, len(records), CHUNK):
            chunk = records[i:i + CHUNK]
            for r in chunk:
                r["as_of"] = r.pop("trade_date")
            stmt = insert(LabelFwd).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "as_of", "horizon"],
                set_={c: getattr(stmt.excluded, c) for c in
                      ("fwd_ret", "bench_ret", "excess", "fwd_max_dd",
                       "label_version")})
            db.execute(stmt)
        db.commit()
        per_window[str(w)] = len(records)
        total += len(records)
        log.info("labels window=%s rows=%d", w, len(records))

    latest = db.execute(text(
        "SELECT max(as_of) FROM label_fwd")).scalar()
    if latest:
        n_codes = db.execute(text(
            "SELECT count(DISTINCT code) FROM label_fwd WHERE as_of = :d"
        ), {"d": latest}).scalar() or 0
        quality.record(db, "label_fwd", latest, "codes_per_day", float(n_codes),
                       quality.status_of(float(n_codes), 2500.0, 1500.0))
        db.commit()
    return {"as_of": str(as_of), "rows": total, "per_window": per_window,
            "label_version": LABEL_VERSION}
