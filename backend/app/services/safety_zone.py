"""安全区间计算：以中期价值中枢（MA250）为锚的价格区间。

带宽改为**个股自适应**：取近5年「收盘价 / MA250 − 1」的 10 / 90 分位作为
上下带宽，再裁剪到 [4%, 20%]。固定 ±10% 会把 55% 以上的股票判为低估，
分位法按构造只让约 10% 的历史区间落在下沿之下。

- 中枢锚：MA250，区间 = [中枢×(1−lo), 中枢×(1+hi)]
- 估值压缩：当前 PE 近5年分位 >= 70%（偏贵）时，上沿压缩到中枢
- 预警线：布林下轨（MA20 − 2σ20），跌破视为深度低估的加仓参考
- 右侧信号：detail 里附 MA20 斜率与相对基准 20 日超额，供提醒分级使用

估值分位带、价格分位带仅作 detail 展示，不参与区间取交集。
"""
import logging
from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from ..models import (DailyQuote, IndexDaily, IntradaySpot, SafetyZone,
                      StockPool, ValuationDaily)
from .screener import _pe_percentile

log = logging.getLogger(__name__)

BAND_LO_PCT = 10.0       # 偏离分布 10 分位 → 下沿带宽
BAND_HI_PCT = 90.0       # 偏离分布 90 分位 → 上沿带宽
MIN_BAND = 0.04          # 带宽下限 4%
MAX_BAND = 0.20          # 带宽上限 20%
PE_HIGH_PCT = 70.0       # PE 5年分位高于该值视为偏贵，压缩上沿至中枢
BENCHMARK = "000852"     # 相对强度基准：中证1000


def _closes(db: Session, code: str, days: int) -> list[float]:
    rows = (db.query(DailyQuote.close)
            .filter(DailyQuote.code == code, DailyQuote.close.isnot(None))
            .order_by(DailyQuote.trade_date.desc()).limit(days).all())
    closes = [float(r[0]) for r in rows]
    closes.reverse()
    return closes


def _band_pct(closes: list[float]) -> tuple[float, float]:
    """近5年「收盘 / MA250 − 1」的 10/90 分位，作为上下带宽（小数）。"""
    if len(closes) < 250:
        return 0.10, 0.10
    arr = np.asarray(closes, dtype=float)
    csum = np.cumsum(np.insert(arr, 0, 0.0))
    ma = (csum[250:] - csum[:-250]) / 250.0
    dev = arr[249:] / ma - 1.0
    dev = dev[np.isfinite(dev)]
    if len(dev) < 250:
        return 0.10, 0.10
    lo = -float(np.percentile(dev, BAND_LO_PCT))
    hi = float(np.percentile(dev, BAND_HI_PCT))
    return (min(max(lo, MIN_BAND), MAX_BAND),
            min(max(hi, MIN_BAND), MAX_BAND))


def _vol250(closes: list[float]) -> float | None:
    """近250日年化波动率（百分数）。"""
    if len(closes) < 60:
        return None
    arr = np.asarray(closes[-251:], dtype=float)
    rets = np.diff(arr) / arr[:-1]
    return round(float(np.std(rets) * np.sqrt(250) * 100), 2)


def _benchmark_ret20(db: Session) -> float | None:
    """基准指数近 20 个交易日收益（百分数），无数据返回 None。"""
    rows = (db.query(IndexDaily.close)
            .filter(IndexDaily.code == BENCHMARK,
                    IndexDaily.close.isnot(None))
            .order_by(IndexDaily.trade_date.desc()).limit(21).all())
    closes = [float(r[0]) for r in rows]
    if len(closes) < 21 or closes[-1] <= 0:
        return None
    closes.reverse()
    return round((closes[-1] / closes[0] - 1) * 100, 2)


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
              intraday: bool = False,
              bench_ret20: float | None = None) -> dict | None:
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
    # MA20 斜率：用最近 MA20 与 5 天前 MA20 差 / 5（日），相对斜率 = 绝对斜率 / ma20
    # closes 是时间正序（最早在前），closes[-20:] = 最近 20 天，closes[-25:-5] = 倒数 25~倒数 5 天（即 5 天前的 MA20）
    # closes 长度已由 len(closes) >= 250 保证，无需额外检查
    ma20_past = float(np.mean(closes[-25:-5]))   # 5 天前的 MA20
    ma20_slope = (ma20 - ma20_past) / 5.0        # 绝对斜率：元/日
    ma20_slope_pct = (ma20 / ma20_past - 1) * 100 / 5.0 if ma20_past else 0.0   # 相对斜率：%/日
    ma20_slope_up = ma20_slope_pct > 0.0         # 短期均线拐头向上

    center = round(ma250, 2)
    band_lo, band_hi = _band_pct(closes)
    zone_low = round(center * (1 - band_lo), 2)
    zone_high = round(center * (1 + band_hi), 2)
    ret20 = round((closes[-1] / closes[-21] - 1) * 100, 2) if closes[-21] else None
    ret20_rel = (round(ret20 - bench_ret20, 2)
                 if ret20 is not None and bench_ret20 is not None else None)

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
              "ma20": round(ma20, 2), "ma20_slope": round(ma20_slope, 4),
              "ma20_slope_pct": round(ma20_slope_pct, 4),
              "ma20_slope_up": ma20_slope_up,
              "band_lo_pct": round(band_lo * 100, 2),
              "band_hi_pct": round(band_hi * 100, 2),
              "band_mode": "dev_percentile",
              "vol250": _vol250(closes),
              "ret20": ret20, "ret20_rel": ret20_rel,
              "benchmark": BENCHMARK if bench_ret20 is not None else None,
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
    bench_ret20 = _benchmark_ret20(db)   # 只算一次，避免逐只股票查基准
    # 同日重算：先清当日旧记录再插入
    db.query(SafetyZone).filter(SafetyZone.calc_date == calc_date).delete()
    for code in codes:
        try:
            z = _calc_one(db, code, calc_date, intraday=intraday,
                          bench_ret20=bench_ret20)
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
