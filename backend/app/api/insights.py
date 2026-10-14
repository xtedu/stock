from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SafetyZone, Stock
from ..schemas import InsightIn
from ..services import llm

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.post("")
def insight(payload: InsightIn, db: Session = Depends(get_db)):
    stock = db.get(Stock, payload.code)
    if not stock:
        raise HTTPException(404, "stock not found")
    zone = (db.query(SafetyZone).filter(SafetyZone.code == payload.code)
            .order_by(SafetyZone.calc_date.desc()).first())
    if not zone:
        raise HTTPException(404, "no safety zone for this stock")
    # 实时解读使用最新区间；价格取区间下沿示意
    text = llm.summarize(stock.code, stock.name, float(zone.zone_low),
                         float(zone.zone_low), float(zone.zone_high))
    if text is None:
        raise HTTPException(503, "llm disabled or unavailable")
    return {"code": stock.code, "insight": text}
