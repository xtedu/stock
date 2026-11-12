"""真实交易日历：替代按工作日近似，供特征/标签/调度统一判断开市日。"""
import logging
from datetime import date, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import TradeCalendar

log = logging.getLogger(__name__)


def sync_trade_calendar(db: Session, years_back: int = 5,
                        years_fwd: int = 1) -> dict:
    """从新浪拉取历史交易日并入库（含未来一年预测日）。

    akshare 只给到当年，未来日期按工作日近似补齐，保证调度不空转。
    """
    df = ak.tool_trade_date_hist_sina()
    days = {pd.Timestamp(d).date() for d in df["trade_date"]}
    today = date.today()
    start, end = today - timedelta(days=365 * years_back), \
        today + timedelta(days=365 * years_fwd)

    rows = [{"trade_date": d, "is_open": True} for d in sorted(days)
            if start <= d <= end]
    # 未来日期（akshare 未覆盖）按工作日近似
    d = max(days) + timedelta(days=1) if days else today
    while d <= end:
        if d.weekday() < 5:
            rows.append({"trade_date": d, "is_open": True})
        d += timedelta(days=1)
    if not rows:
        return {"rows": 0}

    stmt = insert(TradeCalendar).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date"],
        set_={"is_open": stmt.excluded.is_open})
    db.execute(stmt)
    db.commit()
    log.info("trade calendar synced: %d days", len(rows))
    return {"rows": len(rows), "first": str(rows[0]["trade_date"]),
            "last": str(rows[-1]["trade_date"])}


def is_trade_day(db: Session, day: date) -> bool:
    """日历未初始化时降级为工作日判断。"""
    row = db.get(TradeCalendar, day)
    if row is None:
        return day.weekday() < 5
    return bool(row.is_open)


def latest_trade_day(db: Session, day: date | None = None) -> date | None:
    """<= 指定日期的最近开市日。"""
    day = day or date.today()
    row = (db.query(TradeCalendar.trade_date)
           .filter(TradeCalendar.trade_date <= day,
                   TradeCalendar.is_open.is_(True))
           .order_by(TradeCalendar.trade_date.desc())
           .first())
    return row[0] if row else None


def trade_days_between(db: Session, start: date, end: date) -> list[date]:
    rows = (db.query(TradeCalendar.trade_date)
            .filter(TradeCalendar.trade_date.between(start, end),
                    TradeCalendar.is_open.is_(True))
            .order_by(TradeCalendar.trade_date)
            .all())
    return [r[0] for r in rows]
