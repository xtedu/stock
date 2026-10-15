from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DailyQuote, IntradaySpot, Stock, StockPool
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
    codes = [p.code for p, _ in rows]
    if not codes:
        return []

    # 每只股票最近两根日线：最新一根取开盘/收盘，上一根取前收盘
    rn = func.row_number().over(
        partition_by=DailyQuote.code,
        order_by=DailyQuote.trade_date.desc()).label("rn")
    sub = (select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.open,
                  DailyQuote.close, rn)
           .where(DailyQuote.code.in_(codes)).subquery())
    quotes: dict[str, dict] = {}
    for code, td, op, cl, r in db.execute(select(sub).where(sub.c.rn <= 2)):
        q = quotes.setdefault(code, {})
        if r == 1:
            q["quote_date"] = td
            q["open"] = float(op) if op is not None else None
            q["close"] = float(cl) if cl is not None else None
        else:
            q["prev_close"] = float(cl) if cl is not None else None

    spots = {s.code: s for s in db.query(IntradaySpot)
             .filter(IntradaySpot.code.in_(codes)).all()}

    items = []
    for p, name in rows:
        q = quotes.get(p.code, {})
        spot = spots.get(p.code)
        close = q.get("close")
        prev_close = q.get("prev_close")
        # 现价优先取比日线更新的盘中快照，否则回退到最新收盘价
        if (spot and spot.price is not None and spot.trade_date is not None
                and (q.get("quote_date") is None
                     or spot.trade_date > q["quote_date"])):
            price, base = float(spot.price), close
        else:
            price, base = close, prev_close
        change_pct = (round((price - base) / base * 100, 2)
                      if price is not None and base else None)
        items.append(PoolItemOut(
            code=p.code, name=name, score=p.score, reason=p.reason,
            batch_date=p.batch_date, source=(p.reason or {}).get("source"),
            price=price, open=q.get("open"), close=close,
            change_pct=change_pct, quote_date=q.get("quote_date")))
    return items
