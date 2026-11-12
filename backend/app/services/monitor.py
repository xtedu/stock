"""安全区间监控：分级提醒。

- 重点提醒（盘中即推）：跌破下沿 + MA20 拐头向上 + 相对基准 20 日超额 > 0
- 普通观察（收盘后汇总一条）：仅跌破下沿，未满足右侧确认
"""
import logging
from datetime import date

import redis
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import AlertLog, AlertRule, DailyQuote, IntradaySpot, SafetyZone, Stock
from . import llm, notifier

log = logging.getLogger(__name__)

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def _claim(key: str) -> bool:
    """原子占位：返回 True 表示本次可以推送。"""
    return get_redis().set(key, "1", nx=True, ex=86400) is True


def _today() -> str:
    return date.today().isoformat()


def _price_of(db: Session, code: str) -> float | None:
    spot = db.get(IntradaySpot, code)
    if spot and spot.price and spot.trade_date == date.today():
        return float(spot.price)
    close = (db.query(DailyQuote.close)
             .filter(DailyQuote.code == code)
             .order_by(DailyQuote.trade_date.desc())
             .limit(1).scalar())
    return float(close) if close else None


def _name_of(db: Session, code: str) -> str:
    stock = db.get(Stock, code)
    return stock.name if stock else code


def check_and_notify(db: Session) -> dict:
    """只推「重点」档。返回触发数。"""
    rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
    if not rules:
        return {"triggered": 0}

    triggered = 0
    for rule in rules:
        codes = [rule.code] if rule.code else [
            c for (c,) in db.query(SafetyZone.code)
            .filter(SafetyZone.calc_date == date.today()).all()]
        for code in codes:
            zone = (db.query(SafetyZone)
                    .filter(SafetyZone.code == code)
                    .order_by(SafetyZone.calc_date.desc()).first())
            price = _price_of(db, code)
            if not (zone and price):
                continue
            zone_low = float(zone.zone_low)
            if price >= zone_low:
                continue

            d = zone.detail if isinstance(zone.detail, dict) else {}
            slope_up = d.get("ma20_slope_up")
            slope_pct = d.get("ma20_slope_pct") or 0.0
            ret20 = d.get("ret20")
            ret20_rel = d.get("ret20_rel")
            # 指数缺失时降级用绝对 20 日收益
            degraded = ret20_rel is None
            rel_ok = (ret20_rel is not None and ret20_rel > 0) or \
                     (degraded and ret20 is not None and ret20 > 0)
            if slope_up is not True or not rel_ok:
                continue
            if not _claim(f"alert_strong:{code}:{_today()}"):
                continue

            try:
                warn = float(d.get("warn_line"))
            except (TypeError, ValueError):
                warn = zone_low

            strength = ("相对基准 20 日超额 %+.2f%%" % ret20_rel) if not degraded \
                else ("基准缺失，降级按绝对 20 日 %+.2f%%" % (ret20 or 0))
            state = f"跌破下沿 + MA20 拐头向上 + {strength}"
            name = _name_of(db, code)
            summary = llm.summarize(code, name, price, zone_low,
                                    float(zone.zone_high))
            title = f"[重点] {name}({code}) {state}"
            content = (f"当前价 {price}，安全区间 [{zone.zone_low}, {zone.zone_high}]"
                       f"（下带宽 {d.get('band_lo_pct', '-')}%），预警线 {round(warn, 2)}\n"
                       f"MA20={d.get('ma20', '-')}，斜率 {slope_pct:+.4f}%/日\n"
                       + (summary or ""))
            result = notifier.notify(title, content,
                                     channels=rule.channels or None)
            db.add(AlertLog(code=code, price=price, zone_low=zone.zone_low,
                            zone_high=zone.zone_high, ai_summary=summary))
            db.commit()
            triggered += 1
            log.info("strong alert %s: %s", code, result)
    return {"triggered": triggered}


def send_zone_digest(db: Session) -> dict:
    """收盘后汇总普通观察档：只跌破下沿、未满足右侧确认。"""
    today = date.today()
    key = f"alert_digest:{today.isoformat()}"
    if get_redis().exists(key):
        return {"digest": 0, "reason": "already sent"}

    zones = (db.query(SafetyZone)
             .filter(SafetyZone.calc_date == db.query(func.max(SafetyZone.calc_date))
                     .scalar_subquery())
             .order_by(SafetyZone.code)
             .all())
    rows = []
    for zone in zones:
        price = _price_of(db, zone.code)
        if not price:
            continue
        low = float(zone.zone_low)
        if price >= low:
            continue
        if get_redis().exists(f"alert_strong:{zone.code}:{today.isoformat()}"):
            continue
        rows.append((price / low - 1, zone, price))
    if not rows:
        return {"digest": 0}

    rows.sort(key=lambda x: x[0])
    lines = [f"{_name_of(db, z.code)}({z.code}) 现价 {p:.2f}，下沿 {float(z.zone_low):.2f}"
             f"，距下沿 {dev * 100:+.1f}%" for dev, z, p in rows[:40]]
    more = f"\n…另有 {len(rows) - 40} 只" if len(rows) > 40 else ""
    content = ("\n".join(lines) + more +
               "\n\n仅跌破下沿、未满足右侧确认，列为观察，不构成买入建议。")
    result = notifier.notify(f"低估观察 {len(rows)} 只（{today}）", content)
    get_redis().set(key, "1", ex=86400)
    log.info("zone digest: %d rows, %s", len(rows), result)
    return {"digest": len(rows)}


def run_monitor_job() -> None:
    """供调度器调用。"""
    db = SessionLocal()
    try:
        check_and_notify(db)
    finally:
        db.close()


def run_digest_job() -> None:
    """收盘后普通观察汇总。"""
    db = SessionLocal()
    try:
        send_zone_digest(db)
    finally:
        db.close()
