from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CompareAnalysis
from ..schemas import CompareBriefOut, CompareIn, CompareOut
from ..services import compare as compare_svc

router = APIRouter(prefix="/api/compare", tags=["compare"])


@router.get("/history", response_model=list[CompareBriefOut])
def history(limit: int = 20, db: Session = Depends(get_db)):
    return (db.query(CompareAnalysis)
            .order_by(CompareAnalysis.created_at.desc())
            .limit(limit).all())


@router.post("/analyze", response_model=CompareOut)
def analyze(payload: CompareIn, db: Session = Depends(get_db)):
    codes = [c.strip() for c in payload.codes if c and c.strip()]
    if not codes:
        raise HTTPException(400, "至少选择一只股票")
    if len(set(codes)) > compare_svc.MAX_CODES:
        raise HTTPException(400, f"最多对比 {compare_svc.MAX_CODES} 只股票")
    try:
        return compare_svc.analyze(db, codes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{rec_id}", response_model=CompareOut)
def detail(rec_id: int, db: Session = Depends(get_db)):
    rec = db.get(CompareAnalysis, rec_id)
    if not rec:
        raise HTTPException(404, "compare record not found")
    return rec
