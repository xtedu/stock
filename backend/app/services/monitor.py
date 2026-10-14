"""安全区间监控：比对最新价与区间，触发提醒（去重 + 通知 + AI 解读）。"""
import logging
from datetime import date

import redis
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


def _already_notified(code: str) -> bool:
    """同一只股票当天只提醒一次。"""
    key = f"alert:{code}:{date.today().isoformat()}"
    return get_redis().set(key, "1", nx=True, ex=86400) is not True


def check_and_notify(db: Session) -> dict:
    """对启用规则的标的做一次价格比对。返回触发数。"""
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
            # 价格来源：当日快照优先（盘中），否则最近收盘价
            price = None
            spot = db.get(IntradaySpot, code)
            if spot and spot.price and spot.trade_date == date.today():
                price = float(spot.price)
            else:
                close = (db.query(DailyQuote.close)
                         .filter(DailyQuote.code == code)
                         .order_by(DailyQuote.trade_date.desc())
                         .limit(1).scalar())
                price = float(close) if close else None
            if not (zone and price):
                continue
            zone_low = float(zone.zone_low)
            zone_high = float(zone.zone_high)
            # 预警线（布林下轨）缺失时退回区间下沿
            warn = (zone.detail or {}).get("warn_line") if isinstance(zone.detail, dict) else None
            try:
                warn = float(warn)
            except (TypeError, ValueError):
                warn = zone_low
            # enter_zone：仅推送低于区间下沿（低估区）的标的
            hit = price < zone_low
            if not hit or _already_notified(code):
                continue

            name = db.get(Stock, code).name if db.get(Stock, code) else code
            summary = llm.summarize(code, name, price, zone_low, zone_high)
            state = "低于区间下沿，低估区可关注"
            title = f"{name}({code}) {state}"
            content = (f"当前价 {price}，安全区间 [{zone.zone_low}, {zone.zone_high}]，"
                       f"预警线 {round(warn, 2)}（{state}）\n"
                       + (summary or ""))
            result = notifier.notify(title, content,
                                     channels=rule.channels or None)
            db.add(AlertLog(code=code, price=price, zone_low=zone.zone_low,
                            zone_high=zone.zone_high, ai_summary=summary))
            db.commit()
            triggered += 1
            log.info("alert %s: %s", code, result)
    return {"triggered": triggered}


def run_monitor_job() -> None:
    """供调度器调用。"""
    db = SessionLocal()
    try:
        check_and_notify(db)
    finally:
        db.close()
