"""安全区间计算：以中期价值中枢（MA250）为锚的价格区间。

原「三法融合取交集」存在结构性缺陷：技术支撑法下沿 max(MA250, 布林下轨)
天然高于价格分位带上沿，导致交集恒为空、大面积退化为价格分位带。现改为：

- 中枢锚：以 MA250 为价值中枢，区间 = [中枢×0.90, 中枢×1.10]
- 估值压缩：当前 PE 近5年分位 >= 70%（偏贵）时，上沿压缩到中枢，即 [0.90×中枢, 中枢]
- 预警线：布林下轨（MA20 − 2σ20），跌破视为深度低估的加仓参考

估值分位带、价格分位带仅作 detail 展示，不参与区间取交集。
"""
import logging
from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from ..models import DailyQuote, IntradaySpot, SafetyZone, StockPool, ValuationDaily
from .screener import _pe_percentile

log = logging.getLogger(__name__)

ZONE_LOW_RATIO = 0.90    # 下沿 = 中枢 × 0.90
ZONE_HIGH_RATIO = 1.10   # 上沿 = 中枢 × 1.10
PE_HIGH_PCT = 70.0       # PE 5年分位高于该值视为偏贵，压缩上沿至中枢


def _closes(db: Session, code: str, days: int) -> list[float]:
    rows = (db.query(DailyQuote.close)
            .filter(DailyQuote.code == code, DailyQuote.close.isnot(None))
            .order_by(DailyQuote.trade_date.desc()).limit(days).all())
    closes = [float(r[0]) for r in rows]
    closes.reverse()
    return closes


def _implied_band(closes: list[float], vals: list[float],
                  lo_pct: float, hi_pct: float) -> tuple | None:
    """估值分位 → 隐含价格区间：price * (val_pct / val_current)。"""
    vals = [v for v in vals if v and v > 0]
    if not closes or len(vals) < 60:
        return None
    cur = vals[-1]
    if cur <= 0:
        return None
    price_now = closes[-1]
    lo = float(np.percentile(vals, lo_pct))
    hi = float(np.percentile(vals, hi_pct))
    if lo <= 0 or hi <= 0 or hi <= lo:
        return None
    return (round(float(price_now * lo / cur), 2),
            round(float(price_now * hi / cur), 2))


def _valuation_band(db: Session, code: str,
                    closes: list[float]) -> tuple | None:
    pe = [float(r[0]) for r in db.query(ValuationDaily.pe)
          .filter(ValuationDaily.code == code,
                  ValuationDaily.pe.isnot(None))
          .order_by(ValuationDaily.trade_date).all()]
    pb = [float(r[0]) for r in db.query(ValuationDaily.pb)
          .filter(ValuationDaily.code == code,
                  ValuationDaily.pb.isnot(None))
          .order_by(ValuationDaily.trade_date).all()]
    bands = [b for b in (_implied_band(closes, pe, 10, 30),
                         _implied_band(closes, pb, 10, 30)) if b]
    if not bands:
        return None
    # 两个估值带取交集，无交集则取 PE 带
    lo = max(b[0] for b in bands)
    hi = min(b[1] for b in bands)
    if lo >= hi:
        return bands[0]
    return (lo, hi)


def _price_band(closes: list[float]) -> tuple | None:
    if len(closes) < 250:
        return None
    lo = float(np.percentile(closes, 15))
    hi = float(np.percentile(closes, 35))
    return (round(lo, 2), round(hi, 2)) if lo < hi else None


def _cur_pe_override(db: Session, code: str, px: float,
                     last_close: float | None) -> float | None:
    """盘中快照价折算 TTM PE，保持与估值历史同一口径。"""
    if not last_close:
        return None
    last_pe = (db.query(ValuationDaily.pe)
               .filter(ValuationDaily.code == code,
                       ValuationDaily.pe.isnot(None), ValuationDaily.pe > 0)
               .order_by(ValuationDaily.trade_date.desc()).limit(1).scalar())
    if not last_pe or float(last_pe) <= 0:
        return None
    return px * float(last_pe) / float(last_close)


def _calc_one(db: Session, code: str, calc_date: date,
              intraday: bool = False) -> dict | None:
    closes = _closes(db, code, 1249)  # ≈5年
    if len(closes) < 250:
        return None

    cur_override = None
    # 盘中模式：叠加当日快照价（近似今日收盘参与计算）
    if intraday:
        spot = db.get(IntradaySpot, code)
        if spot and spot.price and spot.trade_date == date.today():
            px = float(spot.price)
            cur_override = _cur_pe_override(db, code, px, closes[-1])
            closes.append(px)

    ma250 = float(np.mean(closes[-250:]))
    ma20 = float(np.mean(closes[-20:]))
    std20 = float(np.std(closes[-20:]))
    boll_lower = ma20 - 2 * std20

    center = round(ma250, 2)
    zone_low = round(center * ZONE_LOW_RATIO, 2)
    zone_high = round(center * ZONE_HIGH_RATIO, 2)

    val_band = _valuation_band(db, code, closes)
    price_band = _price_band(closes)
    pe_pct, pe_detail = _pe_percentile(db, code, cur_override)

    used = ["center_ma250"]
    if val_band:
        used.append("valuation")
    if price_band:
        used.append("price_pct")
    if pe_pct is not None:
        used.append("pe_percentile")

    pe_squeeze = bool(pe_pct is not None and pe_pct >= PE_HIGH_PCT)
    if pe_squeeze:
        zone_high = center

    if zone_low >= zone_high:
        return None

    detail = {"center_ma250": center, "valuation": val_band,
              "price_pct": price_band, "pe_percentile": pe_pct,
              "pe_current": pe_detail.get("pe_current"),
              "warn_line": round(boll_lower, 2), "pe_squeeze": pe_squeeze,
              "used": used,
              "source": "intraday" if intraday else "official"}

    return {"code": code, "zone_low": zone_low, "zone_high": zone_high,
            "method": "center_anchor", "detail": detail, "calc_date": calc_date}


def calc_zones(db: Session, calc_date: date | None = None,
               intraday: bool = False) -> dict:
    """为当前 active 股池计算安全区间（upsert 当日记录）。"""
    calc_date = calc_date or date.today()
    codes = [c for (c,) in db.query(StockPool.code)
             .filter(StockPool.active.is_(True)).all()]
    done = skipped = 0
    # 同日重算：先清当日旧记录再插入
    db.query(SafetyZone).filter(SafetyZone.calc_date == calc_date).delete()
    for code in codes:
        try:
            z = _calc_one(db, code, calc_date, intraday=intraday)
        except Exception as e:
            log.warning("zone calc failed %s: %s", code, e)
            z = None
        if not z:
            skipped += 1
            continue
        db.add(SafetyZone(**z))
        done += 1
    db.commit()
    log.info("zones done: %d ok, %d skipped @ %s", done, skipped, calc_date)
    return {"calc_date": str(calc_date), "zones": done, "skipped": skipped}
