"""研究与可行性检验：特征/标签构建、覆盖率、IC 与分层单调性。"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..scheduler import _with_db
from ..services import feasibility, features, labels, trading_calendar
from .jobs import _run_async

router = APIRouter(prefix="/api/research", tags=["research"])


@router.get("/feasibility")
def get_feasibility(window: int = Query(20, ge=1, le=120),
                    days: int = Query(200, ge=20, le=500),
                    db: Session = Depends(get_db)):
    """现有特征对未来超额收益的排序能力（IC / 分层）。"""
    return feasibility.run_feasibility(db, window=window, days=days)


@router.get("/composite")
def get_composite(window: int = Query(20, ge=1, le=120),
                  days: int = Query(200, ge=20, le=500),
                  veto: bool = Query(False),
                  db: Session = Depends(get_db)):
    """等权合成分（低波动 + 低位置 + 低 PB + 反转）的十分位单调性。"""
    return feasibility.run_composite(db, window=window, days=days, veto=veto)


@router.get("/avoid-list")
def get_avoid_list(as_of: date | None = None,
                   pct: float = Query(0.2, gt=0, le=0.5),
                   limit: int = Query(100, ge=1, le=500),
                   db: Session = Depends(get_db)):
    """风险剔除清单：合成分最低分位的个股（仅供规避，不作为买入信号）。"""
    return feasibility.run_avoid_list(db, as_of=as_of, pct=pct, limit=limit)


@router.get("/coverage")
def get_coverage(db: Session = Depends(get_db)):
    """特征层与标签层的最新覆盖情况。"""
    return feasibility.coverage(db)


@router.post("/build-features")
def build_features(as_of: date | None = None,
                   days: int | None = Query(None, ge=2, le=500)):
    """构建全市场特征：指定 as_of 则单日，指定 days 则回补最近 N 个交易日。"""
    if days:
        return _run_async(
            "build_features",
            lambda: _with_db(features.build_features_range, days=days))
    return _run_async("build_features",
                      lambda: _with_db(features.build_features, as_of=as_of))


@router.post("/build-labels")
def build_labels(days: int = Query(250, ge=5, le=500)):
    """回填全市场前向收益标签。"""
    return _run_async("build_labels",
                      lambda: _with_db(labels.build_labels, days=days))


@router.post("/sync-calendar")
def sync_calendar():
    """同步真实交易日历。"""
    return _run_async("sync_trade_calendar",
                      lambda: _with_db(trading_calendar.sync_trade_calendar))
