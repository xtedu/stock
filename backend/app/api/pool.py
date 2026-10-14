from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Stock, StockPool
from ..schemas import PoolItemOut

router = APIRouter(prefix="/api/pool", tags=["pool"])


@router.get("", response_model=list[PoolItemOut])
def list_pool(limit: int = 100, min_score: float = 0,
              db: Session = Depends(get_db)):
    rows = (db.query(StockPool, Stock.name)
            .join(Stock, Stock.code == StockPool.code)
            .filter(StockPool.active.is_(True),
                    StockPool.score >= min_score)
            .order_by(StockPool.score.desc())
            .limit(limit).all())
    return [PoolItemOut(code=p.code, name=name, score=p.score,
                        reason=p.reason, batch_date=p.batch_date)
            for p, name in rows]
