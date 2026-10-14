"""股票行情列表：全市场已同步股票 + 最新收盘价（供 H5 行情页）。"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("")
def list_stocks(keyword: str = "", limit: int = 100, offset: int = 0,
                db: Session = Depends(get_db)):
    like = f"%{keyword.strip()}%"
    args = {"like": like, "limit": limit, "offset": offset}
    kw_empty = not keyword.strip()

    sql = text("""
        SELECT DISTINCT ON (q.code)
               q.code, s.name, s.industry, q.close, q.trade_date
        FROM daily_quotes q JOIN stocks s ON s.code = q.code
        WHERE (:kw_empty OR s.name ILIKE :like OR q.code ILIKE :like)
        ORDER BY q.code, q.trade_date DESC
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(sql, {"kw_empty": kw_empty, **args}).mappings().all()

    count_sql = text("""
        SELECT count(*) FROM stocks s
        WHERE (:kw_empty OR s.name ILIKE :like OR s.code ILIKE :like)
          AND EXISTS (SELECT 1 FROM daily_quotes q WHERE q.code = s.code)
    """)
    total = db.execute(count_sql, {"kw_empty": kw_empty, "like": like}).scalar()

    return {"total": total, "items": [
        {"code": r["code"], "name": r["name"], "industry": r["industry"],
         "close": r["close"], "trade_date": str(r["trade_date"])} for r in rows]}
