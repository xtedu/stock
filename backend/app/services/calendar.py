"""前瞻事件日历：宏观数据日程 / 限售解禁 / 业绩预告。

与 news 模块的区别：这里的都是「尚未发生」或「结果待落地」的事件，
用来提前布局或规避，而不是解释已经涨跌的原因。

数据源：
  百度财经日历 ak.news_economic_baidu   —— 含预期值/前值，可算预期差
  限售解禁     ak.stock_restricted_release_detail_em —— 全市场按日期区间一次拉取
  业绩预告     ak.stock_yjyg_em         —— 早于正式财报，预期先行
"""
import logging
from datetime import date, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import CalendarEvent, StockPool, is_main_board

log = logging.getLogger(__name__)

WINDOW_DAYS = 10
_FOCUS_REGIONS = ("中国", "美国", "欧元区", "日本")
_FORECAST_PERIOD = "20260930"   # 最近一个已完成报告期


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except Exception:
        return None


def _trading_days(start: date, n: int) -> list[date]:
    """未来 n 个工作日（含当天），节假日不精确剔除。"""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _macro_events(days: list[date]) -> list[dict]:
    out = []
    for d in days:
        try:
            df = ak.news_economic_baidu(date=d.strftime("%Y%m%d"))
        except Exception as e:
            log.warning("calendar macro %s failed: %s", d, e)
            continue
        for _, r in df.iterrows():
            region = str(r.get("地区") or "").strip()
            name = str(r.get("事件") or "").strip()
            if not name or region not in _FOCUS_REGIONS:
                continue
            imp = int(_num(r.get("重要性")) or 1)
            # 中国数据全保留，海外只看最高重要性
            if region != "中国" and imp < 3:
                continue
            out.append({
                "kind": "macro",
                "event_date": d,
                "title": f"{region} {name}"[:300],
                "detail": {"region": region,
                           "time": str(r.get("时间") or ""),
                           "forecast": _num(r.get("预期")),
                           "previous": _num(r.get("前值"))},
                "importance": max(imp, 2) if region == "中国" else imp,
                "related_codes": None,
            })
    return out


def _release_events(days: list[date], pool: set[str]) -> list[dict]:
    s, e = days[0], days[-1]
    try:
        df = ak.stock_restricted_release_detail_em(
            start_date=s.strftime("%Y%m%d"), end_date=e.strftime("%Y%m%d"))
    except Exception as ex:
        log.warning("calendar release failed: %s", ex)
        return []

    out = []
    for _, r in df.iterrows():
        code = str(r.get("股票代码") or "").strip().zfill(6)
        ratio = _num(r.get("占解禁前流通市值比例"))
        if ratio is None or not is_main_board(code):
            continue
        pct = round(ratio * 100, 2)
        imp = 3 if pct >= 5 else 2 if pct >= 1 else 1
        in_pool = code in pool
        if not in_pool and imp < 3:
            continue  # 池外的只看大比例解禁，避免噪声
        d = pd.to_datetime(r.get("解禁时间")).date()
        out.append({
            "kind": "release",
            "event_date": d,
            "title": f"{r.get('股票简称')} 解禁 {pct:.2f}% 流通市值"[:300],
            "detail": {"code": code, "name": str(r.get("股票简称")),
                       "ratio": pct, "type": str(r.get("限售股类型") or ""),
                       "in_pool": in_pool},
            "importance": imp,
            "related_codes": [code] if in_pool else None,
        })
    return out


def _forecast_events(pool: set[str]) -> list[dict]:
    """池内业绩预告：预增/预减的方向与幅度，早于正式财报的预期差来源。"""
    if not pool:
        return []
    try:
        df = ak.stock_yjyg_em(date=_FORECAST_PERIOD)
    except Exception as ex:
        log.warning("calendar forecast failed: %s", ex)
        return []

    out = []
    for _, r in df.iterrows():
        code = str(r.get("股票代码") or "").strip().zfill(6)
        if code not in pool:
            continue
        if str(r.get("预测指标") or "") not in ("归属于上市公司股东的净利润",):
            continue
        d = pd.to_datetime(r.get("公告日期"), errors="coerce")
        if pd.isna(d):
            continue
        kind = str(r.get("预告类型") or "")
        amp = _num(r.get("业绩变动幅度"))
        out.append({
            "kind": "forecast",
            "event_date": d.date(),
            "title": f"{r.get('股票简称')} {kind}"
                     + (f" {amp:+.1f}%" if amp is not None else "")[:300],
            "detail": {"code": code, "name": str(r.get("股票简称")),
                       "type": kind, "amplitude": amp,
                       "reason": str(r.get("业绩变动原因") or "")[:200]},
            "importance": 3 if kind in ("预增", "预减", "扭亏", "首亏") else 2,
            "related_codes": [code],
        })
    return out


def sync_calendar(db: Session, days: int = WINDOW_DAYS) -> dict:
    """重建未来窗口的宏观与解禁事件，并刷新池内业绩预告。"""
    today = date.today()
    window = _trading_days(today, days)
    pool = {c for (c,) in db.query(StockPool.code)
            .filter(StockPool.active.is_(True)).all()}

    rows: list[dict] = []
    rows += _macro_events(window)
    rows += _release_events(window, pool)
    rows += _forecast_events(pool)

    # 宏观预测值会变、解禁名单会增，先删窗口内旧数据再重插
    db.query(CalendarEvent).filter(
        CalendarEvent.event_date >= today,
        CalendarEvent.kind.in_(("macro", "release"))).delete(
        synchronize_session=False)

    inserted = 0
    if rows:
        uniq = {(r["kind"], r["event_date"], r["title"]): r for r in rows}
        stmt = (insert(CalendarEvent).values(list(uniq.values()))
                .on_conflict_do_nothing(
                    index_elements=["kind", "event_date", "title"]))
        inserted = db.execute(stmt).rowcount or 0
    db.commit()

    log.info("calendar sync done: window=%s~%s events=%d inserted=%d",
             window[0], window[-1], len(rows), inserted)
    return {"from": str(window[0]), "to": str(window[-1]),
            "events": len(rows), "inserted": inserted}


def list_calendar(db: Session, days: int = WINDOW_DAYS) -> list[dict]:
    today = date.today()
    end = today + timedelta(days=days * 2)
    rows = (db.query(CalendarEvent)
            .filter(CalendarEvent.event_date >= today,
                    CalendarEvent.event_date <= end)
            .order_by(CalendarEvent.event_date, CalendarEvent.importance.desc())
            .all())
    return [{
        "date": str(r.event_date),
        "kind": r.kind,
        "title": r.title,
        "importance": r.importance,
        "detail": r.detail or {},
        "related_codes": r.related_codes or [],
    } for r in rows]
