"""股票对比分析：生成对比指标 + 调用大模型做次日定性研判，结果落库可回看。"""
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (CompareAnalysis, DailyQuote, IntradaySpot, SafetyZone,
                      Stock, StockPool)
from . import llm, money_flow

log = logging.getLogger(__name__)

MAX_CODES = 8   # 单次对比上限，控制大模型输出长度


def _quotes(db: Session, codes: list[str]) -> dict[str, dict]:
    """每只股票最近两根日线 + 盘中快照，现价优先取比日线更新的快照。"""
    rn = func.row_number().over(
        partition_by=DailyQuote.code,
        order_by=DailyQuote.trade_date.desc()).label("rn")
    sub = (select(DailyQuote.code, DailyQuote.trade_date, DailyQuote.close, rn)
           .where(DailyQuote.code.in_(codes)).subquery())
    out: dict[str, dict] = {}
    for code, td, cl, r in db.execute(select(sub).where(sub.c.rn <= 2)):
        q = out.setdefault(code, {})
        if r == 1:
            q["quote_date"] = td
            q["close"] = float(cl) if cl is not None else None
        else:
            q["prev_close"] = float(cl) if cl is not None else None

    spots = {s.code: s for s in db.query(IntradaySpot)
             .filter(IntradaySpot.code.in_(codes)).all()}
    for code, q in out.items():
        spot = spots.get(code)
        if (spot and spot.price is not None and spot.trade_date is not None
                and (q.get("quote_date") is None
                     or spot.trade_date > q["quote_date"])):
            q["price"], q["source"] = float(spot.price), "intraday"
        else:
            q["price"], q["source"] = q.get("close"), "official"
    return out


def _zones(db: Session, codes: list[str]) -> dict[str, SafetyZone]:
    out: dict[str, SafetyZone] = {}
    for z in (db.query(SafetyZone).filter(SafetyZone.code.in_(codes))
              .order_by(SafetyZone.calc_date.desc()).all()):
        out.setdefault(z.code, z)
    return out


def build_metrics(db: Session, codes: list[str]) -> list[dict]:
    """按选定顺序生成对比指标（仅取仍在股池 active 的标的）。"""
    rows = (db.query(StockPool, Stock.name)
            .join(Stock, Stock.code == StockPool.code)
            .filter(StockPool.code.in_(codes),
                    StockPool.active.is_(True)).all())
    found = {p.code: (p, name) for p, name in rows}
    quotes = _quotes(db, codes)
    zones = _zones(db, codes)
    flows = money_flow.load_latest(db, codes)

    items: list[dict] = []
    for code in codes:
        entry = found.get(code)
        if not entry:
            continue
        p, name = entry
        reason = p.reason or {}
        q = quotes.get(code, {})
        price = q.get("price")
        # 现价来自盘中快照时，涨幅基准为最新收盘价；否则为上一交易日收盘价
        base = q.get("close") if q.get("source") == "intraday" else q.get("prev_close")
        change_pct = (round((price - base) / base * 100, 2)
                      if price is not None and base else None)

        z = zones.get(code)
        detail = z.detail if z and isinstance(z.detail, dict) else {}
        zone_low = float(z.zone_low) if z and z.zone_low is not None else None
        zone_high = float(z.zone_high) if z and z.zone_high is not None else None
        center = detail.get("center_ma250")
        ma250_dev = (round((price - float(center)) / float(center) * 100, 2)
                     if price and center else None)
        if price is None or zone_low is None or zone_high is None:
            zone_status = None
        elif price > zone_high:
            zone_status = "above"
        elif price < zone_low:
            zone_status = "below"
        else:
            zone_status = "in"

        flow = flows.get(code, {})
        items.append({
            "code": code, "name": name, "price": price,
            "change_pct": change_pct, "zone_low": zone_low,
            "zone_high": zone_high, "zone_status": zone_status,
            "ma250_dev": ma250_dev,
            "score": float(p.score) if p.score is not None else None,
            "roe_avg": reason.get("roe_avg"),
            "rev_cagr_3y": reason.get("rev_cagr_3y"),
            "profit_cagr_3y": reason.get("profit_cagr_3y"),
            "pe_percentile": reason.get("pe_percentile"),
            **{k: flow.get(k) for k in money_flow.FLOW_KEYS},
            # JSONB 落库需可序列化，date 转 ISO 字符串
            "flow_date": (flow["flow_date"].isoformat()
                          if flow.get("flow_date") else None),
        })
    return items


def analyze(db: Session, codes: list[str]) -> CompareAnalysis:
    """生成对比分析并落库。codes 为空或股池无匹配时抛 ValueError。"""
    codes = list(dict.fromkeys(codes))[:MAX_CODES]
    # 按需实时抓取所选标的当日资金流（一次请求），抓取失败自动降级为已落库数据
    money_flow.sync_money_flow(db, codes=codes, intraday=True)
    metrics = build_metrics(db, codes)
    if not metrics:
        raise ValueError("所选股票不在当前股池中")

    ai = llm.compare(metrics)
    ai_items = {it.get("code"): it for it in (ai or {}).get("items", [])
                if isinstance(it, dict)}
    items = []
    for m in metrics:
        a = ai_items.get(m["code"], {})
        items.append({**m, "view": a.get("view"), "reason": a.get("reason"),
                      "support": a.get("support"),
                      "resistance": a.get("resistance"),
                      "confidence": a.get("confidence")})

    rec = CompareAnalysis(trade_date=date.today(),
                          codes=[m["code"] for m in metrics], items=items,
                          ai_summary=(ai or {}).get("overall"))
    db.add(rec)
    db.commit()
    db.refresh(rec)
    log.info("compare analysis saved: id=%s codes=%s llm=%s",
             rec.id, rec.codes, bool(ai))
    return rec
