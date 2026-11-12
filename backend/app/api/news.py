"""消息面接口。

GET  /api/news?day=YYYY-MM-DD&source=&category=&keyword=  —— 当日政策/快讯/公告
GET  /api/news/brief?day=&refresh=                       —— 当日 AI 决策简报
GET  /api/news/dates                                     —— 有数据的日期列表
POST /api/news/refresh                                   —— 抓取全部源并重建简报
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import NewsItem
from ..services import calendar as calendar_svc
from ..services import news as news_svc

router = APIRouter(prefix="/api/news", tags=["news"])


def _day(day: str | None) -> date:
    return date.fromisoformat(day) if day else date.today()


@router.get("")
def list_news(day: str | None = None, source: str | None = None,
              category: str | None = None, keyword: str | None = None,
              limit: int = Query(200, ge=1, le=500),
              db: Session = Depends(get_db)):
    d = _day(day)
    q = db.query(NewsItem).filter(NewsItem.trade_date == d)
    if source:
        q = q.filter(NewsItem.source == source)
    if category:
        q = q.filter(NewsItem.category == category)
    if keyword:
        q = q.filter(NewsItem.title.ilike(f"%{keyword}%"))
    rows = (q.order_by(NewsItem.published_at.desc().nulls_last(),
                       NewsItem.id.desc())
            .limit(limit).all())

    counts = dict(db.query(NewsItem.source, func.count(NewsItem.id))
                  .filter(NewsItem.trade_date == d)
                  .group_by(NewsItem.source).all())
    return {
        "date": str(d),
        "counts": {s: counts.get(s, 0) for s in news_svc._SOURCE_NAME},
        "source_names": news_svc._SOURCE_NAME,
        "items": [{
            "id": n.id,
            "source": n.source,
            "category": n.category,
            "title": n.title,
            "summary": n.summary,
            "url": n.url,
            "published_at": (n.published_at.isoformat()
                             if n.published_at else None),
            "related_codes": n.related_codes or [],
            "digest": n.digest or {},
        } for n in rows],
    }


@router.get("/calendar")
def get_calendar(days: int = Query(10, ge=1, le=30),
                 db: Session = Depends(get_db)):
    """未来事件日历：宏观数据（含预期值）、限售解禁、业绩预告。"""
    return {"days": days, "events": calendar_svc.list_calendar(db, days)}


@router.get("/brief")
def get_brief(day: str | None = None, refresh: bool = False,
              db: Session = Depends(get_db)):
    return news_svc.build_brief(db, _day(day), force=refresh)


@router.get("/dates")
def list_dates(limit: int = Query(30, ge=1, le=90),
               db: Session = Depends(get_db)):
    rows = (db.query(NewsItem.trade_date, func.count(NewsItem.id))
            .group_by(NewsItem.trade_date)
            .order_by(NewsItem.trade_date.desc()).limit(limit).all())
    return [{"date": str(d), "count": c} for d, c in rows]


@router.post("/refresh")
def refresh_all(day: str | None = None, db: Session = Depends(get_db)):
    d = _day(day)
    sync = news_svc.sync_news(db, d)
    float_mv = news_svc.sync_float_mv(db)
    digest = news_svc.attach_digest(db, d)
    cal = calendar_svc.sync_calendar(db)
    brief = news_svc.build_brief(db, d, force=True)
    return {"sync": sync, "float_mv": float_mv, "digest": digest, "calendar": cal,
            "brief": brief.get("payload"),
            "brief_reason": brief.get("reason")}
