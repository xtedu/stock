from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DailyQuote, SafetyZone, Stock, StockPool
from ..schemas import QuoteOut, SafetyZoneOut

router = APIRouter(prefix="/api/zones", tags=["zones"])


@router.get("", response_model=list[SafetyZoneOut])
def list_zones(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(SafetyZone).order_by(SafetyZone.calc_date.desc())
            .limit(limit).all())
    return rows


@router.get("/{code}")
def stock_detail(code: str, days: int = 250, db: Session = Depends(get_db)):
    stock = db.get(Stock, code)
    if not stock:
        raise HTTPException(404, "stock not found")
    zone = (db.query(SafetyZone).filter(SafetyZone.code == code)
            .order_by(SafetyZone.calc_date.desc()).first())
    pool = (db.query(StockPool).filter(StockPool.code == code)
            .order_by(StockPool.batch_date.desc()).first())
    quotes = (db.query(DailyQuote).filter(DailyQuote.code == code)
              .order_by(DailyQuote.trade_date.desc()).limit(days).all())
    quotes.reverse()

    price = float(quotes[-1].close) if quotes and quotes[-1].close else None
    in_zone = bool(
        zone and price and zone.zone_low is not None and zone.zone_high is not None
        and float(zone.zone_low) <= price <= float(zone.zone_high))
    return {
        "stock": {"code": stock.code, "name": stock.name,
                  "industry": stock.industry},
        "zone": SafetyZoneOut.model_validate(zone) if zone else None,
        "pool": ({"score": float(pool.score) if pool.score is not None else None,
                  "reason": pool.reason, "batch_date": str(pool.batch_date)}
                 if pool else None),
        "price": price,
        "in_zone": in_zone,
        "quotes": [QuoteOut(trade_date=q.trade_date, open=q.open, high=q.high,
                            low=q.low, close=q.close, volume=q.volume)
                   for q in quotes],
    }
