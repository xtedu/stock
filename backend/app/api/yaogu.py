"""妖股筛选 REST 接口。

GET  /api/yaogu?limit=50        —— 返回候选池（八层漏斗+六维打分）+ 连板梯队 + 统计
GET  /api/yaogu/ladder          —— 只返回连板梯队（按行业分组）
GET  /api/yaogu/history         —— 每日快照日期列表
GET  /api/yaogu/history/{date}  —— 某日快照（按行业分组）
POST /api/yaogu/refresh         —— 重新跑一遍筛选（同步）
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import yaogu as yaogu_svc

router = APIRouter(prefix="/api/yaogu", tags=["yaogu"])


@router.get("")
def list_yaogu(limit: int = Query(50, ge=1, le=200),
               refresh: bool = False,
               db: Session = Depends(get_db)):
    # live=True：交易时段内自动抓快照，收盘/非交易时段自动回退到日线
    res = yaogu_svc.run_yaogu(db, force=refresh, live=True)
    if limit and len(res.get("candidates", [])) > limit:
        res["candidates"] = res["candidates"][:limit]
    return res


@router.get("/ladder")
def get_ladder(db: Session = Depends(get_db)):
    res = yaogu_svc.run_yaogu(db, live=True)
    return {"ladder": res.get("ladder", {}),
            "stats": res.get("stats", {}),
            "batch_date": res.get("batch_date")}


@router.get("/history")
def yaogu_history(limit: int = Query(120, ge=1, le=400),
                  db: Session = Depends(get_db)):
    return {"dates": yaogu_svc.history_dates(db, limit=limit)}


@router.get("/history/{trade_date}")
def yaogu_history_detail(trade_date: str, db: Session = Depends(get_db)):
    return yaogu_svc.history_detail(db, date.fromisoformat(trade_date))


@router.post("/refresh")
def refresh_yaogu(db: Session = Depends(get_db)):
    return yaogu_svc.run_yaogu(db, force=True, live=True)
