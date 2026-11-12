"""消息面：政策 / 财经快讯 / 公司公告聚合，以及每日 AI 决策简报。

数据源（均在服务器容器内实测可用）：
  财联社电报   ak.stock_info_global_cls   —— 政策与突发事件
  东财全球快讯 ak.stock_info_global_em    —— 财经要闻 + 摘要 + 链接
  新浪全球快讯 ak.stock_info_global_sina  —— 实时快讯
  央视新闻联播 ak.news_cctv               —— 政策定调风向标
  全市场公告   ak.stock_notice_report     —— 仅保留池内标的，避免每日上千条膨胀

设计取舍：
- source+title 唯一约束做幂等去重，重复抓取不会产生重复行；
- 公告只存与股票池（stock_pool.active）相关的，个股动态才有决策价值；
- 简报按自然日缓存一条，force=True 时重算。
"""
import logging
import re
from datetime import date, datetime

import akshare as ak
import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import (CalendarEvent, DailyQuote, NewsBrief, NewsItem, Stock,
                      StockPool, is_main_board)

log = logging.getLogger(__name__)

# 分类关键词：政策 > 宏观 > 一般快讯；公告源直接归 company
_POLICY_KW = ("国务院", "央行", "证监会", "发改委", "财政部", "工信部", "商务部",
              "政策", "规划", "指导意见", "通知", "税收", "降准", "降息", "LPR",
              "专项债", "补贴", "监管", "会议", "十五五", "十五")
_MACRO_KW = ("CPI", "PPI", "GDP", "PMI", "美联储", "非农", "加息", "汇率",
             "人民币", "进出口", "社融", "M2", "外汇", "国债", "油价", "黄金")

_SOURCE_NAME = {"cls": "财联社", "em": "东财快讯", "sina": "新浪快讯",
                "cctv": "新闻联播", "notice": "公司公告"}


def _classify(text: str, source: str) -> str:
    if source == "notice":
        return "company"
    if source == "cctv":
        return "policy"
    if any(k in text for k in _POLICY_KW):
        return "policy"
    if any(k in text for k in _MACRO_KW):
        return "macro"
    return "general"


def _to_dt(value) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(value)
        return None if pd.isna(ts) else ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 各数据源抓取（统一返回 [{source,title,summary,url,published_at}]）
# --------------------------------------------------------------------------- #

def _fetch_cls() -> list[dict]:
    df = ak.stock_info_global_cls(symbol="全部")
    out = []
    for _, r in df.iterrows():
        title = str(r.get("标题") or "").strip()
        content = str(r.get("内容") or "").strip()
        if not title and not content:
            continue
        ts = _to_dt(f"{r.get('发布日期')} {r.get('发布时间')}")
        out.append({"source": "cls", "title": (title or content)[:400],
                    "summary": content[:2000] or None, "url": None,
                    "published_at": ts})
    return out


def _fetch_em() -> list[dict]:
    df = ak.stock_info_global_em()
    out = []
    for _, r in df.iterrows():
        title = str(r.get("标题") or "").strip()
        if not title:
            continue
        out.append({"source": "em", "title": title[:400],
                    "summary": (str(r.get("摘要") or "").strip() or None),
                    "url": (str(r.get("链接") or "").strip() or None),
                    "published_at": _to_dt(r.get("发布时间"))})
    return out


def _fetch_sina() -> list[dict]:
    df = ak.stock_info_global_sina()
    out = []
    for _, r in df.iterrows():
        text = str(r.get("内容") or "").strip()
        if not text:
            continue
        m = re.match(r"【(.+?)】(.*)", text, re.S)
        title, body = (m.group(1), m.group(2)) if m else (text[:80], text)
        out.append({"source": "sina", "title": title[:400],
                    "summary": body.strip()[:2000] or None, "url": None,
                    "published_at": _to_dt(r.get("时间"))})
    return out


def _fetch_cctv(day: date) -> list[dict]:
    df = ak.news_cctv(date=day.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        out.append({"source": "cctv", "title": title[:400],
                    "summary": (str(r.get("content") or "").strip()[:2000] or None),
                    "url": None, "published_at": None})
    return out


def _fetch_notice(day: date, pool_codes: set[str]) -> list[dict]:
    """全市场公告只保留池内标的，避免每日 1300+ 条噪声。"""
    if not pool_codes:
        return []
    df = ak.stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
    out = []
    for _, r in df.iterrows():
        code = str(r.get("代码") or "").strip()
        if code not in pool_codes or not is_main_board(code):
            continue
        title = str(r.get("公告标题") or "").strip()
        if not title:
            continue
        out.append({"source": "notice", "title": title[:400],
                    "summary": f"{r.get('名称')} · {r.get('公告类型')}",
                    "url": (str(r.get("网址") or "").strip() or None),
                    "published_at": _to_dt(r.get("公告日期")),
                    "related_codes": [code]})
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _pool_codes(db: Session) -> set[str]:
    rows = (db.query(StockPool.code)
            .filter(StockPool.active.is_(True)).all())
    return {c for (c,) in rows}


def _name_map(db: Session) -> dict[str, str]:
    """股票名 → 代码，用于把快讯关联到池内个股。"""
    rows = db.query(Stock.code, Stock.name).filter(Stock.name.isnot(None)).all()
    return {n: c for c, n in rows if n and len(n) >= 2 and is_main_board(c)}


def sync_news(db: Session, day: date | None = None) -> dict:
    """抓取各源并幂等落库。单个源失败不影响其他源。"""
    day = day or date.today()
    pool = _pool_codes(db)
    names = _name_map(db)
    pool_names = {n: c for n, c in names.items() if c in pool}

    log.info("news sync start @ %s pool=%d", day, len(pool))
    raw: list[dict] = []
    for fn in (_fetch_cls, _fetch_em, _fetch_sina):
        try:
            got = fn()
            raw += got
            log.info("news fetch %s: %d", fn.__name__, len(got))
        except Exception as e:
            log.warning("news fetch %s failed: %s", fn.__name__, e)
    try:
        got = _fetch_cctv(day)
        raw += got
        log.info("news fetch cctv: %d", len(got))
    except Exception as e:
        log.warning("news fetch cctv failed: %s", e)
    try:
        got = _fetch_notice(day, pool)
        raw += got
        log.info("news fetch notice: %d", len(got))
    except Exception as e:
        log.warning("news fetch notice failed: %s", e)

    rows = []
    for it in raw:
        text = f"{it['title']} {it.get('summary') or ''}"
        codes = it.get("related_codes")
        if not codes:
            codes = sorted({c for n, c in pool_names.items() if n in text}) or None
        rows.append({
            "source": it["source"],
            "category": _classify(text, it["source"]),
            "title": it["title"],
            "summary": it.get("summary"),
            "url": it.get("url"),
            "published_at": it.get("published_at"),
            "trade_date": day,
            "related_codes": codes,
        })

    inserted = 0
    if rows:
        # 源内去重（同一批里重复标题只留一条），再交给唯一约束兜底
        uniq = {(r["source"], r["title"]): r for r in rows}
        stmt = (insert(NewsItem).values(list(uniq.values()))
                .on_conflict_do_nothing(index_elements=["source", "title"]))
        inserted = db.execute(stmt).rowcount or 0
        db.commit()

    total = db.query(func.count(NewsItem.id)).filter(
        NewsItem.trade_date == day).scalar()
    log.info("news sync done: fetched=%d inserted=%d today_total=%d",
             len(rows), inserted, total)
    return {"date": str(day), "fetched": len(rows), "inserted": inserted,
            "today_total": total}


# --------------------------------------------------------------------------- #
# 利好消化度：判断消息是否已经被市场定价
# --------------------------------------------------------------------------- #

STATE_TEXT = {"priced_in": "已兑现(高位)", "fermenting": "发酵中",
              "oversold": "错杀(未反应)"}


def _size_factor(mv_yi: float | None) -> float:
    """市值分层系数：小盘股日常波动更大，同一涨幅不算兑现，阈值需放宽。"""
    if not mv_yi:
        return 1.0
    if mv_yi < 50:
        return 1.25
    if mv_yi < 200:
        return 1.1
    return 1.0


def sync_float_mv(db: Session, codes: set[str] | None = None) -> int:
    """更新流通市值（akshare 单只查询，约 0.2s/只，仅池内）。"""
    if codes is None:
        codes = _pool_codes(db)
    n = 0
    for code in sorted(codes):
        try:
            df = ak.stock_individual_info_em(symbol=code)
            info = dict(zip(df["item"], df["value"]))
            mv = info.get("流通市值")
            if mv in (None, "-", ""):
                continue
            db.query(Stock).filter(Stock.code == code).update(
                {"float_mv": float(mv)}, synchronize_session=False)
            n += 1
        except Exception as e:
            log.warning("float mv %s failed: %s", code, e)
    db.commit()
    log.info("float mv updated: %d/%d", n, len(codes))
    return n


def _digest_one(db: Session, code: str, day: date,
                float_mv: float | None = None) -> dict | None:
    """单只股票在消息日之前的表现，用于判断利好是否已被消化。

    阈值按个股波动率自适应 + 市值分层：先用近 20 个交易日的日收益标准差
    衡量该股「正常波动幅度」，折算成 5 日波动 σ5；再按流通市值放大小盘股阈值。
    高波动小盘股的 +8% 可能只是日常抖动，低波动大盘股的 +8% 才是真兑现。

    地板/天花板（防止极端值把阈值拉爆）：
      σ5 ∈ [3%, 15%] → 已兑现阈值 ∈ [5%, 25%]，错杀阈值 ∈ [-2%, -15%]
    """
    rows = (db.query(DailyQuote.close, DailyQuote.high, DailyQuote.volume)
            .filter(DailyQuote.code == code, DailyQuote.trade_date <= day)
            .order_by(DailyQuote.trade_date.desc()).limit(25).all())
    if len(rows) < 8:
        return None
    closes = [float(r[0]) for r in rows if r[0]]
    if len(closes) < 8:
        return None
    vols = [float(r[2] or 0) for r in rows]
    highs = [float(r[1] or 0) for r in rows[:20] if r[1]]

    ret5 = closes[0] / closes[5] - 1
    day_chg = closes[0] / closes[1] - 1
    base = sum(vols[1:6]) / 5 if len(vols) > 5 else 0
    vol_ratio = (vols[0] / base) if base else None
    high20 = max(highs) if highs else None
    from_high20 = (closes[0] / high20 - 1) if high20 else None

    # 个股日波动率 → 5 日波动率
    rets = [closes[i] / closes[i + 1] - 1 for i in range(min(20, len(closes) - 1))]
    sigma_d = float(pd.Series(rets).std()) if len(rets) > 2 else 0.02
    if not sigma_d or pd.isna(sigma_d):
        sigma_d = 0.02
    sigma5 = min(0.15, max(0.03, sigma_d * (5 ** 0.5)))

    mv_yi = (float(float_mv) / 1e8) if float_mv else None
    k = _size_factor(mv_yi)
    priced_th = min(0.25, max(0.05, 1.5 * sigma5) * k)
    over_th = -min(0.15, max(0.02, 0.8 * sigma5) * k)

    if ret5 >= priced_th:
        state = "priced_in"
    elif ret5 <= over_th:
        state = "oversold"
    else:
        state = "fermenting"
    # 显著上涨 + 当日冲高回落且放量 → 兑现信号更强（同样按市值放宽）
    if (ret5 >= sigma5 * k and day_chg <= -0.5 * sigma_d
            and (vol_ratio or 0) >= 1.2):
        state = "priced_in"

    return {"ret5": round(ret5 * 100, 2),
            "day_chg": round(day_chg * 100, 2),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "from_high20": round(from_high20 * 100, 2) if from_high20 is not None else None,
            # 判定依据：5 日波动率、市值分层、阈值、标准化涨幅
            "sigma5": round(sigma5 * 100, 2),
            "float_mv_yi": round(mv_yi, 1) if mv_yi else None,
            "size_k": k,
            "priced_th": round(priced_th * 100, 2),
            "over_th": round(over_th * 100, 2),
            "ret5_z": round(ret5 / sigma5, 2) if sigma5 else None,
            "state": state}


def attach_digest(db: Session, day: date | None = None) -> int:
    """为当日带关联标的的消息计算消化度并落库。"""
    day = day or date.today()
    rows = (db.query(NewsItem)
            .filter(NewsItem.trade_date == day,
                    NewsItem.related_codes.isnot(None)).all())
    mvs = dict(db.query(Stock.code, Stock.float_mv).all())
    n = 0
    for r in rows:
        dg = {c: d for c in (r.related_codes or [])
              if (d := _digest_one(db, c, day, mvs.get(c)))}
        if dg:
            r.digest = dg
            n += 1
    db.commit()
    log.info("news digest: %d/%d items", n, len(rows))
    return n


# --------------------------------------------------------------------------- #
# 每日 AI 决策简报
# --------------------------------------------------------------------------- #

_BRIEF_MAX_ITEMS = 60


def _pick_items(db: Session, day: date) -> list[NewsItem]:
    """优先政策 > 关联池内标的 > 宏观 > 其他，取前 N 条喂给大模型。"""
    rows = (db.query(NewsItem).filter(NewsItem.trade_date == day)
            .order_by(NewsItem.id.desc()).limit(500).all())

    def score(n: NewsItem) -> int:
        s = 0
        if n.category == "policy":
            s += 6
        elif n.category == "macro":
            s += 4
        elif n.category == "company":
            s += 2
        if n.related_codes:
            s += 5
        return s

    rows.sort(key=score, reverse=True)
    return rows[:_BRIEF_MAX_ITEMS]


def _format_items(items: list[NewsItem]) -> str:
    lines = []
    for i, n in enumerate(items, 1):
        tag = _SOURCE_NAME.get(n.source, n.source)
        cat = {"policy": "政策", "macro": "宏观", "company": "公告"}.get(
            n.category or "", "快讯")
        body = (n.summary or "").replace("\n", " ")[:160]
        line = f"{i}. [{tag}/{cat}] {n.title}｜{body}"
        if n.digest:
            dg = "; ".join(
                f"{c} 前5日{d['ret5']:+.1f}%(≈{d.get('ret5_z')}σ) "
                f"流通市值{d.get('float_mv_yi')}亿(系数{d.get('size_k')}) "
                f"该股5日波动±{d.get('sigma5')}% 兑现阈值{d.get('priced_th')}% "
                f"当日{d['day_chg']:+.1f}% "
                f"{STATE_TEXT.get(d['state'], d['state'])}"
                for c, d in n.digest.items())
            line += f" 【股价已反应度：{dg}】"
        lines.append(line)
    return "\n".join(lines)


def _format_calendar(db: Session) -> str:
    """未来事件，含预期值，供大模型判断预期差。"""
    rows = (db.query(CalendarEvent)
            .filter(CalendarEvent.event_date >= date.today())
            .order_by(CalendarEvent.event_date, CalendarEvent.importance.desc())
            .limit(40).all())
    lines = []
    for r in rows:
        d = r.detail or {}
        extra = ""
        if r.kind == "macro":
            fc, pv = d.get("forecast"), d.get("previous")
            if fc is not None or pv is not None:
                extra = f"（预期 {fc if fc is not None else '-'}／前值 {pv if pv is not None else '-'}）"
            extra = f"[{d.get('region', '')}] " + extra
        elif r.kind == "release":
            extra = f"（解禁 {d.get('ratio')}% 流通市值，池内={'是' if d.get('in_pool') else '否'}）"
        elif r.kind == "forecast":
            extra = f"（{d.get('type')} {d.get('amplitude')}%）"
        lines.append(f"{r.event_date} {r.title}{extra}")
    return "\n".join(lines)


def build_brief(db: Session, day: date | None = None,
                force: bool = False) -> dict:
    """生成/读取当日消息面简报（落库缓存，force 重算）。"""
    from . import llm

    day = day or date.today()
    exist = (db.query(NewsBrief).filter(NewsBrief.brief_date == day)
             .one_or_none())
    if exist and not force:
        return {"date": str(day), "cached": True,
                "payload": exist.payload, "created_at": exist.created_at.isoformat()}

    items = _pick_items(db, day)
    if not items:
        return {"date": str(day), "cached": False, "payload": None,
                "reason": "当日暂无消息面数据，请先执行抓取"}

    pool_rows = (db.query(Stock.code, Stock.name, Stock.industry)
                 .join(StockPool, StockPool.code == Stock.code)
                 .filter(StockPool.active.is_(True)).all())
    pool_txt = "\n".join(
        f"{c} {n} {ind or '-'}" for c, n, ind in pool_rows)

    payload = llm.news_brief(_format_items(items), pool_txt,
                             _format_calendar(db))
    if payload is None:
        return {"date": str(day), "cached": False, "payload": None,
                "reason": "大模型未启用或调用失败"}

    stmt = (insert(NewsBrief)
            .values(brief_date=day, payload=payload)
            .on_conflict_do_update(index_elements=["brief_date"],
                                   set_={"payload": payload,
                                         "created_at": func.now()}))
    db.execute(stmt)
    db.commit()
    return {"date": str(day), "cached": False, "payload": payload}
