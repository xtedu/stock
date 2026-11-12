"""数据采集：AKShare 主数据源（免费）。

- 股票列表 / 日线行情（并发 + 断点续传）
- 财务指标（报告期）/ 估值历史 PE/PB（个股）
"""
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import akshare as ak
import numpy as np
import pandas as pd
import requests
from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import (DailyQuote, FinIndicator, IndexDaily, IntradaySpot,
                      Stock, ValuationDaily, is_main_board)

log = logging.getLogger(__name__)


def _f(v):
    """安全转 float（数据源中常见 '--'/空串）。"""
    try:
        v = float(v)
        return v if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def sync_stock_list(db: Session) -> int:
    """拉取 A 股股票列表入库（增量 upsert）。"""
    df = ak.stock_info_a_code_name()
    count = 0
    for _, row in df.iterrows():
        code = str(row["code"])
        if not is_main_board(code):
            continue
        stmt = insert(Stock).values(code=code, name=str(row["name"]))
        stmt = stmt.on_conflict_do_nothing(index_elements=["code"])
        db.execute(stmt)
        count += 1
    db.commit()
    log.info("stock list synced: %d", count)
    return count


def _sina_symbol(code: str) -> str | None:
    """沪深代码 → 新浪符号（sh/sz 前缀）；非沪深返回 None。"""
    if code.startswith("6"):
        return "sh" + code
    if code[0] in "03":
        return "sz" + code
    return None


_SINA_RENAME = {"date": "日期", "open": "开盘", "high": "最高",
                "low": "最低", "close": "收盘", "volume": "成交量",
                "amount": "成交额"}

# 腾讯前复权日线：akshare 的 sina 接口在多线程下会段错误，这里直连 HTTP
_TX_KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 "?param={sym},day,{start},{end},640,qfq")
_TX_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _fetch_em(code: str, start: str, end: str) -> pd.DataFrame:
    return ak.stock_zh_a_hist(symbol=code, period="daily",
                              start_date=start, end_date=end, adjust="qfq")


def _fetch_tencent(code: str, start: str, end: str) -> pd.DataFrame | None:
    """腾讯前复权日线（仅沪深）。纯 requests 实现，线程安全。

    腾讯单次最多返回 640 根，超出时按最早日期向前翻页。
    接口只提供 成交量（手），成交额按 成交量 × 100 × 收盘价 近似。
    """
    sym = _sina_symbol(code)
    if sym is None:
        return None
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    rows: list[list] = []
    for _ in range(6):  # 最多向前翻 6 页（约 15 年）
        url = _TX_KLINE_URL.format(sym=sym, start=s, end=e)
        payload = requests.get(url, timeout=15, headers=_TX_HEADERS).json()
        node = (payload.get("data") or {}).get(sym) or {}
        day = node.get("qfqday") or node.get("day") or []
        if not day:
            break
        rows = day + rows
        if len(day) < 640 or day[0][0] <= s:
            break
        e = (pd.Timestamp(day[0][0]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if not rows:
        return None
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
    for c in ("开盘", "收盘", "最高", "最低", "成交量"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.drop_duplicates(subset=["日期"]).sort_values("日期")
    df["成交额"] = df["成交量"] * 100.0 * df["收盘"]
    return df.reset_index(drop=True)


def _fetch_sina(code: str, start: str, end: str) -> pd.DataFrame | None:
    """新浪前复权日线（第三级降级）。成交量单位由「股」换算成「手」。"""
    sym = _sina_symbol(code)
    if sym is None:
        return None
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end,
                             adjust="qfq")
    if df is None or df.empty:
        return None
    df = df.rename(columns={"date": "日期", "open": "开盘", "high": "最高",
                            "low": "最低", "close": "收盘",
                            "volume": "成交量", "amount": "成交额"})
    df["日期"] = pd.to_datetime(df["日期"])
    df["成交量"] = pd.to_numeric(df["成交量"], errors="coerce") / 100.0
    return df


def _retry(fn, attempts: int = 3):
    """带指数退避 + 随机抖动的重试；返回非空 DataFrame，否则 None。"""
    for i in range(attempts):
        try:
            df = fn()
            if df is not None and not df.empty:
                return df
        except Exception as e:
            log.debug("fetch attempt %d failed: %s", i + 1, str(e)[:80])
        time.sleep(random.uniform(0.5, 1.2) * (i + 1))
    return None


_FETCHERS = {"em": _fetch_em, "tx": _fetch_tencent, "sina": _fetch_sina}
_SRC_LABEL = {"em": "eastmoney", "tx": "tencent", "sina": "sina"}


def _fetch_quotes(code: str, start: str, end: str,
                  order: tuple[str, ...] = ("em", "tx", "sina")):
    """单只日线：按 order 依次尝试数据源。返回 (code, df, source)。

    主源 3 次退避重试，降级源只试 1 次（源已被限流时死磕只会延长封禁）。
    """
    if not is_main_board(code):
        return code, None, "skipped"
    for i, name in enumerate(order):
        fn = _FETCHERS[name]
        df = _retry(lambda: fn(code, start, end), attempts=3 if i == 0 else 1)
        time.sleep(random.uniform(0.2, 0.6))
        if df is not None and not df.empty:
            return code, df, _SRC_LABEL[name]
    return code, None, "fail"


def _upsert_quotes(db: Session, code: str, df: pd.DataFrame) -> int:
    rows = [{
        "code": code,
        "trade_date": pd.to_datetime(r["日期"]).date(),
        "open": _f(r.get("开盘")), "high": _f(r.get("最高")),
        "low": _f(r.get("最低")), "close": _f(r.get("收盘")),
        "volume": int(r.get("成交量")) if pd.notna(r.get("成交量")) else None,
        "amount": _f(r.get("成交额")),
    } for _, r in df.iterrows()]
    if not rows:
        return 0
    stmt = insert(DailyQuote).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={"open": stmt.excluded.open, "high": stmt.excluded.high,
              "low": stmt.excluded.low, "close": stmt.excluded.close,
              "volume": stmt.excluded.volume, "amount": stmt.excluded.amount},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_all_quotes(db: Session, years: int = 5, workers: int = 2,
                    limit: int | None = None,
                    only_existing: bool = False) -> dict:
    """全市场日线增量同步（限速 + 熔断，东财优先、失败降级腾讯）。

    - 只补 latest_date 之后的新 bar（新股才拉 years 年历史窗口），请求量极小
    - 并发默认 2，每笔请求后随机 sleep 0.3-0.8s
    - 连续失败 25 次判定数据源不可用，直接中止本次任务（不再反复撞击）
    - 北交所（4/8/92 开头）全链路排除，本项目只处理沪深
    - only_existing=True 时只修「已有历史」的增量缺口，不拉全历史；
      当日全市场 bar 由 sync_daily_bars_from_spot 一次性写入，正常情况这里是空跑
    """
    # 股票列表接口（沪深交易所站点）偶发超时，失败时沿用库里已有列表继续跑
    try:
        total = sync_stock_list(db)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        total = db.query(func.count(Stock.code)).scalar() or 0
        log.warning("stock list sync failed, use existing %d: %s",
                    total, str(e)[:80])
    codes = [c for (c,) in db.query(Stock.code).all()]

    latest = dict(db.query(DailyQuote.code,
                           func.max(DailyQuote.trade_date))
                  .group_by(DailyQuote.code).all())
    today = date.today()
    stale = [c for c in codes if latest.get(c) is None or latest[c] < today]
    todo = [c for c in stale if is_main_board(c)]
    if only_existing:
        todo = [c for c in todo if latest.get(c) is not None]
    skipped = len(codes) - len(todo)
    if limit:
        todo = todo[:limit]
    log.info("quotes sync: %d total, %d todo (北交所/无历史 %d 只跳过)",
             len(codes), len(todo), skipped)
    if not todo:
        return {"stocks": total, "fetched": 0, "rows": 0, "source": "-"}

    end = today.strftime("%Y%m%d")
    # 探活：东财 → 腾讯 → 新浪，跳过已被限流的源（死磕只会延长封禁）
    order: tuple[str, ...] = ("em", "tx", "sina")
    probe = next((c for c in todo if is_main_board(c)), None)
    if probe is not None:
        p_start = (today - timedelta(days=10)).strftime("%Y%m%d")
        for i, name in enumerate(order):
            try:
                df = _FETCHERS[name](probe, p_start, end)
            except Exception as e:  # noqa: BLE001
                log.warning("probe %s failed: %s", name, str(e)[:80])
                continue
            if df is not None and not df.empty:
                order = order[i:]
                if name != "em":
                    log.warning("eastmoney quotes unavailable, primary source -> %s",
                                name)
                else:
                    log.info("eastmoney quotes alive, use it first")
                break
    use_em = order[0] == "em"

    # 并发统一压到 2：akshare 底层原生扩展在多线程并发下曾出现 SIGSEGV
    # （2 核小内存机上尤为明显），与本地长时间验证过的配置保持一致。
    max_workers = int(os.getenv("QUOTES_SYNC_WORKERS", "2"))
    em_workers = max_workers
    sg_workers = max_workers

    def _start_of(code: str) -> str:
        last = latest.get(code)
        if last is None:
            return (today - timedelta(days=years * 365)).strftime("%Y%m%d")
        # 新浪 stock_zh_a_daily 不认 start==end 的单天窗口。
        # 对 last 接近 today 的股票（差 1-2 天），把 start 提前到 today-2
        # 确保至少 3 天窗口；对 last 较早的股票（差 >2 天），直接用 last+1。
        ideal = last + timedelta(days=1)
        return min(ideal, today - timedelta(days=2)).strftime("%Y%m%d")

    ok = rows = 0
    fail = 0
    src: dict[str, int] = {}
    # 熔断改成**成功率判定**：处理到 50 只时如果 fail >= 45（90% 失败），
    # 说明两个源都挂了才 abort；否则跑完。避免"连续 25 只 fail 就 abort"
    # 的激进逻辑——线程池并发时前几批很可能因为网络抖动全挂。
    _abort_ratio = 0.90
    _abort_window = 50
    processed = 0

    def _pick_workers(use_em_flag: bool) -> int:
        return em_workers if use_em_flag else sg_workers

    with ThreadPoolExecutor(max_workers=_pick_workers(use_em)) as ex:
        futs = {ex.submit(_fetch_quotes, c, _start_of(c), end, order): c
                for c in todo}
        for fut in as_completed(futs):
            code, df, source = fut.result()
            processed += 1
            src[source] = src.get(source, 0) + 1
            if source == "fail":
                fail += 1
            if df is not None and not df.empty:
                rows += _upsert_quotes(db, code, df)
                ok += 1
            # 熔断：处理足够样本后，失败率超过阈值才判定两源全挂
            if (processed >= _abort_window
                    and fail / processed >= _abort_ratio
                    and fail >= _abort_window * _abort_ratio):
                log.error("quotes sync aborted: %d/%d failed after %d processed (%.0f%%)",
                          fail, len(todo), processed, fail / processed * 100)
                for f in futs:
                    f.cancel()
                break
    # 跑完后检查覆盖率：如果今日行情覆盖率低于 80%，记 warning
    db.commit()
    coverage = db.query(func.count(DailyQuote.code.distinct())
                        .filter(DailyQuote.trade_date == today)).scalar() or 0
    hs_total = len([c for c, in db.query(Stock.code).all()
                    if is_main_board(c)])
    cov_pct = coverage / hs_total * 100 if hs_total else 0
    if cov_pct < 80:
        log.warning("daily quotes coverage low: %.1f%% (%d/%d)",
                    cov_pct, coverage, hs_total)
    log.info("quotes synced: %d/%d stocks, %d rows, src=%s, coverage=%.1f%%",
             ok, len(todo), rows, src, cov_pct)
    return {"stocks": total, "fetched": ok, "rows": rows, "source": src,
            "eastmoney": use_em, "coverage": round(cov_pct, 1)}


# ---------- 指数日线（基准） ----------

INDEX_CODES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000"}


def _fetch_index(code: str, start: str, end: str) -> tuple[pd.DataFrame | None, str]:
    """指数日线：东财 → 东财全历史 → 新浪，逐级降级（同一 IP 被限流时依次切换）。"""
    try:
        df = ak.index_zh_a_hist(symbol=code, period="daily",
                                start_date=start, end_date=end)
        if df is not None and not df.empty:
            return df, "eastmoney"
    except Exception as e:
        log.warning("index_zh_a_hist failed %s: %s", code, str(e)[:80])
    prefix = "sh" if code.startswith("00") else "sz"
    try:
        df = ak.stock_zh_index_daily_em(symbol=prefix + code)
        if df is not None and not df.empty:
            return df, "em_daily"
    except Exception as e:
        log.warning("stock_zh_index_daily_em failed %s: %s", code, str(e)[:80])
    return ak.stock_zh_index_daily(symbol=prefix + code), "sina"


def _index_rows(df: pd.DataFrame, start: date) -> list[dict]:
    """把两种接口的列名统一成 (date, close, pct_chg)。"""
    if df is None or df.empty:
        return []
    if "日期" in df.columns:
        dates = pd.to_datetime(df["日期"], errors="coerce")
        closes = df["收盘"].map(_f)
        pcts = (df["涨跌幅"].map(_f) if "涨跌幅" in df.columns
                else pd.Series([None] * len(df)))
    else:
        dates = pd.to_datetime(df["date"], errors="coerce")
        closes = df["close"].map(_f)
        pcts = pd.Series([None] * len(df))
    rows = []
    for d, c, p in zip(dates, closes, pcts):
        if pd.isna(d) or c is None:
            continue
        day = d.date()
        if day < start:
            continue
        rows.append({"trade_date": day, "close": c, "pct_chg": p})
    return rows


def sync_index_quotes(db: Session, years: int = 5) -> dict:
    """同步基准指数日线（增量）。单只失败只记 warning，不中断。"""
    today = date.today()
    done: dict[str, int] = {}
    for code in INDEX_CODES:
        try:
            latest = db.query(func.max(IndexDaily.trade_date)).filter(
                IndexDaily.code == code).scalar()
            start = (latest + timedelta(days=1) if latest
                     else today - timedelta(days=years * 365))
            if start > today:
                done[code] = 0
                continue
            df, src = _fetch_index(code, start.strftime("%Y%m%d"),
                                   today.strftime("%Y%m%d"))
            rows = [{"code": code, **r} for r in _index_rows(df, start)]
            if not rows:
                log.warning("index %s no rows (source=%s)", code, src)
                done[code] = 0
                continue
            stmt = insert(IndexDaily).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "trade_date"],
                set_={"close": stmt.excluded.close,
                      "pct_chg": stmt.excluded.pct_chg},
            )
            db.execute(stmt)
            db.commit()
            done[code] = len(rows)
        except Exception as e:
            db.rollback()
            log.warning("index sync failed %s: %s", code, str(e)[:120])
            done[code] = 0
    log.info("index quotes synced: %s", done)
    return {"indexes": done}


# ---------- 盘中全市场快照 ----------

def _fetch_spot_em() -> pd.DataFrame:
    return ak.stock_zh_a_spot_em()


def _fetch_spot_sina() -> pd.DataFrame:
    """新浪全市场快照：代码带 sh/sz/bj 前缀需去掉；不含 PE/PB。"""
    df = ak.stock_zh_a_spot()
    df["代码"] = df["代码"].astype(str).str.replace(r"^(sh|sz|bj)", "",
                                                    regex=True)
    return df


def _spot_bars() -> tuple[pd.DataFrame | None, str]:
    """全市场快照 → 标准化 OHLCV（只读，不落库），返回 (df, 数据源)。"""
    col_map = {"代码": "code", "今开": "open", "最高": "high", "最低": "low",
               "最新价": "close", "成交量": "volume", "成交额": "amount",
               "涨跌幅": "pct_chg"}
    try:
        df = _fetch_spot_em()
        src = "eastmoney"
    except Exception as e:  # noqa: BLE001
        log.warning("eastmoney spot unavailable, fallback to sina: %s",
                    str(e)[:80])
        try:
            df = _retry(_fetch_spot_sina)
            src = "sina"
        except Exception as e2:  # noqa: BLE001
            log.error("spot fetch failed: %s", str(e2)[:80])
            return None, "fail"
    if df is None or df.empty or "代码" not in df.columns:
        return None, src
    keep = {k: v for k, v in col_map.items() if k in df.columns}
    out = df[list(keep)].rename(columns=keep)
    for c in out.columns:
        if c != "code":
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out["code"] = (out["code"].astype(str)
                   .str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6))
    out = out[out["code"].str.len() == 6]
    out = out[out["code"].apply(is_main_board)]
    out = out[out["close"].fillna(0) > 0]
    for c in ("open", "high", "low", "volume", "amount", "pct_chg"):
        if c not in out.columns:
            out[c] = np.nan
    if src == "sina":
        # 新浪快照成交量单位是「股」，统一成「手」与东财/历史接口一致
        out["volume"] = out["volume"] / 100.0
    return out.reset_index(drop=True), src


def fetch_spot_bars() -> pd.DataFrame | None:
    """盘中全市场快照 → 标准化 OHLCV（只读，不落库）。

    东财优先、新浪降级，失败返回 None。供妖股盘中模式拼当日临时 K 线。
    """
    return _spot_bars()[0]


_CST = timezone(timedelta(hours=8))


def sync_daily_bars_from_spot(db: Session) -> dict:
    """收盘后用**一次**全市场快照写入当日日线。

    逐只历史接口（5000+ 次请求）在收盘后被东财限流时会整批失败，
    这里改成 1 次快照请求，覆盖所有已有历史的代码：
    - 只写已有历史 bar 的代码，避免只有单日 bar 的股票把涨跌家数统计拉偏
    - 15:00 前不写（快照还是盘中价，不是收盘价）
    - 节假日守卫：与上一根 bar 价格几乎完全一致时判定为休市，不写入
    """
    if datetime.now(_CST).hour < 15:
        return {"rows": 0, "reason": "before close (15:00 CST)"}
    df, src = _spot_bars()
    if df is None or df.empty:
        return {"rows": 0, "source": src, "reason": "spot unavailable"}

    today = date.today()
    last = {code: float(close) for code, close in db.execute(text(
        "SELECT DISTINCT ON (code) code, close FROM daily_quotes "
        "WHERE trade_date < :d AND close IS NOT NULL "
        "ORDER BY code, trade_date DESC"), {"d": today}).all()}

    rows, same = [], 0
    for _, r in df.iterrows():
        code = r["code"]
        prev = last.get(code)
        if prev is None:
            continue
        close = _f(r.get("close"))
        if not close or close <= 0:
            continue
        if abs(prev - close) < 1e-6:
            same += 1
        vol = _f(r.get("volume"))
        rows.append({
            "code": code, "trade_date": today,
            "open": _f(r.get("open")), "high": _f(r.get("high")),
            "low": _f(r.get("low")), "close": close,
            "volume": int(vol) if vol is not None else None,
            "amount": _f(r.get("amount")),
        })
    if not rows:
        return {"rows": 0, "source": src, "reason": "no tracked code in spot"}
    if same / len(rows) >= 0.9:
        log.info("daily bars from spot: %.0f%% identical to last session, "
                 "skip (non-trading day)", same / len(rows) * 100)
        return {"rows": 0, "source": src, "reason": "no new session"}

    stmt = insert(DailyQuote).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={"open": stmt.excluded.open, "high": stmt.excluded.high,
              "low": stmt.excluded.low, "close": stmt.excluded.close,
              "volume": stmt.excluded.volume, "amount": stmt.excluded.amount},
    )
    db.execute(stmt)
    db.commit()
    log.info("daily bars from spot: %d rows (source=%s, %s)",
             len(rows), src, today)
    return {"rows": len(rows), "source": src, "trade_date": str(today)}


def sync_spot_snapshot(db: Session) -> int:
    """全市场最新价快照：东财优先，失败降级新浪，覆盖写入 intraday_spot。

    本表 pe/pb 无任何消费方（各处只读 price），新浪源缺失时写 None。
    """
    today = date.today()
    try:
        df = _fetch_spot_em()
        pe_col, pb_col, src = "市盈率-动态", "市净率", "eastmoney"
    except Exception as e:
        log.warning("eastmoney spot unavailable, fallback to sina: %s",
                    str(e)[:80])
        df = _retry(_fetch_spot_sina)
        pe_col = pb_col = None
        src = "sina"
    if df is None:
        log.error("spot snapshot failed: eastmoney and sina both unavailable")
        return 0
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", ""))
        if len(code) != 6 or not is_main_board(code):
            continue
        rows.append({"code": code, "price": _f(r.get("最新价")),
                     "pe": _f(r.get(pe_col)) if pe_col else None,
                     "pb": _f(r.get(pb_col)) if pb_col else None,
                     "trade_date": today})
    if not rows:
        return 0
    stmt = insert(IntradaySpot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={"price": stmt.excluded.price, "pe": stmt.excluded.pe,
              "pb": stmt.excluded.pb, "trade_date": stmt.excluded.trade_date,
              "updated_at": func.now()},
    )
    db.execute(stmt)
    db.commit()
    log.info("spot snapshot synced: %d rows (src=%s)", len(rows), src)
    return len(rows)


# ---------- 财务指标（报告期） ----------

FIN_COLS = {
    "净资产收益率(%)": "roe",
    "主营业务收入增长率(%)": "revenue_yoy",
    "净利润增长率(%)": "profit_yoy",
    "资产负债率(%)": "debt_ratio",
    "每股经营性现金流(元)": "ocf",
    "扣除非经常性损益后的净利润(元)": "np_deducted",
    "经营现金净流量与净利润的比率(%)": "ocf_np_ratio",
}


def _fetch_fin(code: str, start_year: str):
    try:
        return code, ak.stock_financial_analysis_indicator(
            symbol=code, start_year=start_year)
    except Exception as e:
        log.warning("fetch fin failed %s: %s", code, e)
        return code, None


def _upsert_fin(db: Session, code: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        rd = pd.to_datetime(r["日期"], errors="coerce")
        if pd.isna(rd):
            continue
        rows.append({"code": code, "report_date": rd.date(),
                     **{v: _f(r.get(k)) for k, v in FIN_COLS.items()}})
    if not rows:
        return 0
    stmt = insert(FinIndicator).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "report_date"],
        set_={v: getattr(stmt.excluded, v) for v in FIN_COLS.values()},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_fin_indicators(db: Session, code: str, years: int = 5) -> int:
    """拉取单只股票近 N 年报告期财务指标并 upsert。"""
    df = _fetch_fin(code, str(date.today().year - years))[1]
    return _upsert_fin(db, code, df)


# ---------- 估值历史（PE/PB 日度） ----------

def _fetch_val(code: str):
    try:
        return code, ak.stock_value_em(symbol=code)
    except Exception as e:
        log.warning("fetch valuation failed %s: %s", code, e)
        return code, None


def _upsert_val(db: Session, code: str, df: pd.DataFrame, years: int = 5) -> int:
    if df is None or df.empty:
        return 0
    cutoff = date.today() - timedelta(days=years * 365)
    rows = []
    for _, r in df.iterrows():
        td = pd.to_datetime(r["数据日期"], errors="coerce")
        if pd.isna(td) or td.date() < cutoff:
            continue
        rows.append({"code": code, "trade_date": td.date(),
                     "pe": _f(r.get("PE(TTM)")), "pb": _f(r.get("市净率"))})
    if not rows:
        return 0
    stmt = insert(ValuationDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code", "trade_date"],
        set_={"pe": stmt.excluded.pe, "pb": stmt.excluded.pb},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def sync_valuation(db: Session, code: str, years: int = 5) -> int:
    """拉取单只股票 PE(TTM)/PB 历史并 upsert（限近 N 年）。"""
    df = _fetch_val(code)[1]
    return _upsert_val(db, code, df, years)


def sync_stock_fundamentals(db: Session, code: str) -> dict:
    return {"fin": sync_fin_indicators(db, code),
            "val": sync_valuation(db, code)}


def sync_all_fundamentals(db: Session, years: int = 5, workers: int = 4) -> dict:
    """全市场基本面同步：财务指标 + 估值历史（仅沪深，并发抓取、串行入库）。"""
    codes = [c for (c,) in db.query(Stock.code).all() if is_main_board(c)]
    ok_fin = ok_val = 0
    start_year = str(date.today().year - years)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs_fin = {ex.submit(_fetch_fin, c, start_year): c for c in codes}
        for fut in as_completed(futs_fin):
            code, df = fut.result()
            try:
                if _upsert_fin(db, code, df):
                    ok_fin += 1
            except Exception as e:
                db.rollback()
                log.warning("upsert fin failed %s: %s", code, e)
        futs_val = {ex.submit(_fetch_val, c): c for c in codes}
        for fut in as_completed(futs_val):
            code, df = fut.result()
            try:
                if _upsert_val(db, code, df, years):
                    ok_val += 1
            except Exception as e:
                db.rollback()
                log.warning("upsert val failed %s: %s", code, e)
    log.info("fundamentals synced: fin=%d val=%d / %d", ok_fin, ok_val, len(codes))
    return {"stocks": len(codes), "fin_synced": ok_fin, "val_synced": ok_val}


# ---------- 行业分类（申万二级，准静态数据） ----------

_SW_LEVEL_SUFFIX = ("Ⅰ", "Ⅱ", "Ⅲ")


def _clean_sw_name(name) -> str:
    """去掉申万行业名末尾的分级罗马数字（如 白酒Ⅱ → 白酒）。"""
    n = str(name).strip()
    while n and n[-1] in _SW_LEVEL_SUFFIX:
        n = n[:-1]
    return n.strip()


def _fetch_sw_cons(sw_code: str, sw_name: str):
    try:
        df = ak.index_component_sw(symbol=sw_code)
        return sw_name, [str(c) for c in df["证券代码"].tolist()]
    except Exception as e:
        log.warning("fetch sw cons failed %s(%s): %s", sw_name, sw_code, e)
        return sw_name, []


def sync_industry(db: Session, workers: int = 6) -> dict:
    """申万二级行业分类 → stocks.industry（准静态，手动/周度触发）。

    东财行业接口（stock_board_industry_*）在当前网络下不可用，改用申万
    行业：先取二级行业清单（约 131 个），再逐个拉成分股，全市场约 131 次
    请求，代码→行业名批量写回 stocks.industry。独立路径，不挂日更链路。
    """
    info = ak.sw_index_second_info()
    pairs = [(str(r["行业代码"]).removesuffix(".SI"), _clean_sw_name(r["行业名称"]))
             for _, r in info.iterrows()]
    by_name: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_sw_cons, c, n) for c, n in pairs]
        for fut in as_completed(futs):
            name, codes = fut.result()
            if codes:
                by_name.setdefault(name, []).extend(codes)

    updated = 0
    for name, codes in by_name.items():
        res = db.execute(update(Stock).where(Stock.code.in_(codes))
                         .values(industry=name))
        updated += res.rowcount or 0
    db.commit()
    log.info("industry synced: %d industries, %d stocks updated",
             len(pairs), updated)
    return {"industries": len(pairs), "updated": updated}
