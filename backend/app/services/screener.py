"""优质股筛选：硬门槛 + 加权打分，结果写 stock_pool。

硬门槛（可调）：
- 非 ST、上市满 3 年（以日线历史长度近似）
- 最近 4 个报告期 ROE 均值 >= 10%
- 最新资产负债率 <= 60%
- 最新扣非净利润 > 0（近一年无亏损）

打分（0-100）：
- ROE 均值            35%
- 营收 3 年复合增速    15%
- 净利 3 年复合增速    15%
- 现金流质量(OCF/NP)  15%
- PE 5年分位（低好）   20%
"""
import logging
from datetime import date

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import DailyQuote, FinIndicator, IntradaySpot, Stock, StockPool, ValuationDaily

log = logging.getLogger(__name__)

GATES = {
    "roe_min": 10.0,       # 近4期 ROE 均值下限
    "debt_max": 60.0,      # 资产负债率上限
    "min_history_days": 750,  # 日线历史条数（≈3年）
}
WEIGHTS = {"roe": 0.35, "rev": 0.15, "profit": 0.15,
           "cash": 0.15, "pe_pct": 0.20}


def _cap(v, full=100.0):
    return max(0.0, min(100.0, v / full * 100.0))


def _cagr(yoy_list: list[float]) -> float | None:
    """由最近 N 期同比增速合成复合增速（%），乘积<=0 时回退均值。"""
    ys = [y for y in yoy_list if y is not None]
    if not ys:
        return None
    product = 1.0
    for y in ys:
        product *= 1 + y / 100.0
    if product > 0:
        return (product ** (1.0 / len(ys)) - 1) * 100.0
    return float(np.mean(ys))


def _recent_fin(db: Session, code: str, n: int = 4) -> list[FinIndicator]:
    return (db.query(FinIndicator)
            .filter(FinIndicator.code == code)
            .order_by(FinIndicator.report_date.desc()).limit(n).all())


def _pe_percentile(db: Session, code: str,
                   cur_override: float | None = None) -> tuple[float | None, dict]:
    """当前 PE 在近5年分布中的分位（0-100，越低越便宜）。

    cur_override：盘中模式下用快照价折算的 TTM PE。
    """
    rows = (db.query(ValuationDaily.pe)
            .filter(ValuationDaily.code == code,
                    ValuationDaily.pe.isnot(None), ValuationDaily.pe > 0)
            .order_by(ValuationDaily.trade_date).all())
    pes = [float(r[0]) for r in rows]
    if len(pes) < 60:  # 数据不足，放弃该项
        return None, {"pe_points": len(pes)}
    cur = cur_override if (cur_override and cur_override > 0) else pes[-1]
    pct = float(np.sum(np.array(pes) <= cur) / len(pes) * 100)
    return pct, {"pe_current": round(cur, 2), "pe_percentile": round(pct, 1),
                 "pe_points": len(pes)}


def run_screen(db: Session, batch_date: date | None = None,
               intraday: bool = False) -> dict:
    batch_date = batch_date or date.today()

    # 盘中模式：加载当日快照价
    spot_map: dict[str, IntradaySpot] = {}
    if intraday:
        spot_map = {s.code: s for s in db.query(IntradaySpot)
                    .filter(IntradaySpot.trade_date == batch_date).all()}
        if not spot_map:
            log.warning("intraday screen: no fresh spot snapshot, "
                        "fallback to official close")

    # 使旧批次失效
    db.query(StockPool).update({"active": False})

    # 近 4 期财务聚合
    fin_rows = (db.query(
        FinIndicator.code,
        func.avg(FinIndicator.roe).label("roe_avg"),
        func.avg(FinIndicator.revenue_yoy).label("rev_avg"),
        func.avg(FinIndicator.profit_yoy).label("profit_avg"),
        func.count(FinIndicator.id).label("n"),
        func.max(FinIndicator.report_date).label("last_date"),
    ).group_by(FinIndicator.code)
        .having(func.count(FinIndicator.id) >= 4).all())
    if not fin_rows:
        log.warning("no fin indicators yet, run sync-fundamentals first")
        db.commit()
        return {"batch_date": str(batch_date), "selected": 0,
                "reason": "no fin data"}

    # 最新一期明细（负债率 / 扣非净利润）
    latest_fin = {}
    latest_dates = {r.code: r.last_date for r in fin_rows}
    for code, rd in latest_dates.items():
        f = (db.query(FinIndicator)
             .filter(FinIndicator.code == code,
                     FinIndicator.report_date == rd).first())
        if f:
            latest_fin[code] = f

    # 日线历史长度（近似上市时间门槛）
    hist = dict((db.query(DailyQuote.code, func.count(DailyQuote.id))
                .group_by(DailyQuote.code)).all())

    # ST 过滤
    st_codes = {c for (c,) in db.query(Stock.code)
                .filter(Stock.name.like("%ST%")).all()}

    selected = 0
    for r in fin_rows:
        code = r.code
        if code in st_codes or hist.get(code, 0) < GATES["min_history_days"]:
            continue
        lf = latest_fin.get(code)
        if not lf:
            continue
        roe_avg = float(r.roe_avg or 0)
        if roe_avg < GATES["roe_min"]:
            continue
        if lf.debt_ratio is not None and float(lf.debt_ratio) > GATES["debt_max"]:
            continue
        if lf.np_deducted is not None and float(lf.np_deducted) <= 0:
            continue

        # 打分
        recents = _recent_fin(db, code, 4)
        cur_override = None
        spot = spot_map.get(code)
        if spot and spot.price:
            lc = (db.query(DailyQuote.close)
                  .filter(DailyQuote.code == code)
                  .order_by(DailyQuote.trade_date.desc())
                  .limit(1).scalar())
            vp = (db.query(ValuationDaily.pe)
                  .filter(ValuationDaily.code == code,
                          ValuationDaily.pe.isnot(None))
                  .order_by(ValuationDaily.trade_date.desc())
                  .limit(1).scalar())
            if lc and vp and float(vp) > 0:
                # 快照价折算 TTM PE，保持与估值历史同一口径
                cur_override = float(spot.price) * float(vp) / float(lc)
        pe_pct, pe_detail = _pe_percentile(db, code, cur_override)
        s_roe = _cap(roe_avg, 20.0)
        rev_cagr = _cagr([float(f.revenue_yoy) if f.revenue_yoy is not None else None
                          for f in recents][::-1][:3])
        profit_cagr = _cagr([float(f.profit_yoy) if f.profit_yoy is not None else None
                             for f in recents][::-1][:3])
        cash_vals = [float(f.ocf_np_ratio) for f in recents
                     if f.ocf_np_ratio is not None]
        cash_avg = float(np.mean(cash_vals)) if cash_vals else None
        if cash_avg is not None and cash_avg <= 2.0:
            # 源数据可能为小数比率（0.9=90%），自适应转为百分数口径
            cash_avg *= 100.0
        s_rev = _cap(rev_cagr, 30.0)
        s_profit = _cap(profit_cagr, 30.0)
        s_cash = min(100.0, max(0.0, cash_avg)) if cash_avg is not None else 50.0
        s_pe = (100.0 - pe_pct) if pe_pct is not None else 50.0
        score = round(s_roe * WEIGHTS["roe"] + s_rev * WEIGHTS["rev"]
                      + s_profit * WEIGHTS["profit"] + s_cash * WEIGHTS["cash"]
                      + s_pe * WEIGHTS["pe_pct"], 2)

        db.add(StockPool(
            code=code, score=score, batch_date=batch_date, active=True,
            reason={"source": "intraday" if intraday else "official",
                    "roe_avg": round(roe_avg, 2),
                    "rev_cagr_3y": round(rev_cagr, 2) if rev_cagr is not None else None,
                    "profit_cagr_3y": round(profit_cagr, 2) if profit_cagr is not None else None,
                    "cash_quality": round(cash_avg, 1) if cash_avg is not None else None,
                    "debt_ratio": float(lf.debt_ratio) if lf.debt_ratio else None,
                    "report_date": str(lf.report_date),
                    "scores": {"roe": round(s_roe, 1), "rev": round(s_rev, 1),
                               "profit": round(s_profit, 1),
                               "cash": round(s_cash, 1),
                               "pe_pct": round(s_pe, 1)},
                    **pe_detail}))
        selected += 1
        if selected >= 200:  # 防御上限
            break

    db.commit()
    log.info("screen done: %d selected @ %s", selected, batch_date)
    return {"batch_date": str(batch_date), "selected": selected}
