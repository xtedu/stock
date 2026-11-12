from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PickSnapshot
from ..schemas import (PickHistoryDayOut, PickOut, PerfOut)
from ..services.pick import compute_context, compute_picks
from ..services.picks_track import compute_performance

router = APIRouter(prefix="/api/picks", tags=["picks"])


@router.get("/context")
def pick_context(db: Session = Depends(get_db)):
    return compute_context(db)


@router.get("", response_model=list[PickOut])
def list_picks(limit: int = 3, refresh: bool = False,
               db: Session = Depends(get_db)):
    return compute_picks(db, limit=limit, refresh=refresh)


@router.get("/history", response_model=list[PickHistoryDayOut])
def pick_history(days: int = 60, db: Session = Depends(get_db)):
    """最近 N 个有落库记录的交易日，按日分组。"""
    dates = [r[0] for r in db.execute(text(
        "SELECT DISTINCT batch_date FROM picks "
        "ORDER BY batch_date DESC LIMIT :n"), {"n": days}).all()]
    if not dates:
        return []
    rows = (db.query(PickSnapshot)
            .filter(PickSnapshot.batch_date.in_(dates))
            .order_by(PickSnapshot.batch_date.desc(), PickSnapshot.rank)
            .all())
    grouped: dict[date, list[PickSnapshot]] = {}
    for r in rows:
        grouped.setdefault(r.batch_date, []).append(r)
    return [{"batch_date": d, "items": grouped[d]} for d in dates]


@router.get("/performance", response_model=PerfOut)
def pick_performance(benchmark: str = "000852", window: int = 20,
                     db: Session = Depends(get_db)):
    """策略 vs 基准绩效：非重叠样本口径，已扣双边成本。"""
    return compute_performance(db, benchmark=benchmark, window=window)
