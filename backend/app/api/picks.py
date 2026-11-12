from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import PickOut
from ..services.pick import compute_context, compute_picks

router = APIRouter(prefix="/api/picks", tags=["picks"])


@router.get("/context")
def pick_context(db: Session = Depends(get_db)):
    return compute_context(db)


@router.get("", response_model=list[PickOut])
def list_picks(limit: int = 3, refresh: bool = False,
               db: Session = Depends(get_db)):
    return compute_picks(db, limit=limit, refresh=refresh)
