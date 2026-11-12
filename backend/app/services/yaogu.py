"""妖股筛选器：六维打分 + 八层漏斗 + 连板梯队。

参考 HTML 策略文档：
- 妖股筛选策略_六维漏斗.html
- 跨年妖股_押板块跟踪梯队策略.html

数据源现状（2026-11-10）：
- daily_quotes: OHLCV + amount，无流通市值/换手率
- valuation_daily: 空
- akshare spot_em: 被封

因此第一版以下字段用代理：
  流通市值  → 成交额代理（amount ∈ [1亿, 30亿] = 小盘活跃）
  换手率    → 振幅代理（amplitude ∈ [5%, 20%] = 换手健康）
  筹码安全值 → 年内涨停次数代理（近 250 交易日涨停 ≤ 3 = 新面孔）
  题材纯度  → 同行业今日涨停家数代理（板块涨停越多越主线）
  主力净流入 → 暂不用（东财封了）

后续补数时替换：
  流通市值/换手率/主力净流入 ← akshare spot_em 解封后
  筹码安全值 ← 通达信 or 专门爬
"""
import copy
import logging
import re
import time
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import DailyQuote, Stock, YaoguSnapshot, is_bj_code
from .datafeed import fetch_spot_bars

_CST = timezone(timedelta(hours=8))

log = logging.getLogger(__name__)

# 涨跌停阈值（%）—— 科创板/创业板 20%，主板 10%
LIMIT_RATE_MAIN = 0.095
LIMIT_RATE_GEM = 0.195


# --------------------------------------------------------------------------- #
# 数据加载
# --------------------------------------------------------------------------- #

def _load_quotes(db: Session, days: int = 300) -> pd.DataFrame:
    """一次性把沪深最近 N 天 daily_quotes 拉进 DataFrame。"""
    cutoff = date.today() - timedelta(days=days)
    rows = db.query(
        DailyQuote.code,
        DailyQuote.trade_date,
        DailyQuote.open,
        DailyQuote.high,
        DailyQuote.low,
        DailyQuote.close,
        DailyQuote.volume,
        DailyQuote.amount,
    ).filter(DailyQuote.trade_date >= cutoff).all()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    # 过滤北交所 + close 为零 / 空
    df = df[~df["code"].apply(is_bj_code)]
    df = df[df["close"].fillna(0) > 0]
    df = df.sort_values(["code", "trade_date"])
    return df


def _load_stocks(db: Session) -> dict[str, Stock]:
    return {s.code: s for s in db.query(Stock).all() if not is_bj_code(s.code)}


# --------------------------------------------------------------------------- #
# 涨停原因（同花顺涨停池）
# --------------------------------------------------------------------------- #

_ZT_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_ZT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
}
# 同日涨停原因复用，避免每次刷新页面都请求外部接口
_ZT_CACHE: dict[str, tuple[float, dict]] = {}
_ZT_TTL_TODAY = 180.0
_ZT_TTL_HISTORY = 3600.0


def _fmt_ts(v) -> str | None:
    """同花顺封板时间（秒级时间戳）→ 'HH:MM:SS'。"""
    try:
        return datetime.fromtimestamp(int(v), tz=_CST).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _fetch_limit_reasons(trade_date: date) -> dict[str, dict]:
    """同花顺涨停池 → {code: {原因/封板时间/炸板次数/封单额/换手率...}}。

    失败时返回空 dict，页面降级为不展示原因，不影响梯队本身。
    同花顺历史数据只保留约 1 年，更早日期返回空。
    """
    key = trade_date.strftime("%Y%m%d")
    cached = _ZT_CACHE.get(key)
    ttl = _ZT_TTL_TODAY if trade_date >= date.today() else _ZT_TTL_HISTORY
    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    params = {
        "page": "1",
        "limit": "200",  # 同花顺单页上限 200
        "field": "199112,10,9001,330323,330324,330325,9002,330329,"
                 "133971,133970,1968584,3475914,9003,9004",
        "filter": "HS,GEM2STAR",
        "order_field": "330324",
        "order_type": "0",
        "date": key,
    }
    info: list = []
    try:
        for page in range(1, 5):
            params["page"] = str(page)
            r = requests.get(_ZT_URL, params=params, headers=_ZT_HEADERS,
                             timeout=10)
            batch = ((r.json().get("data") or {}).get("info")) or []
            info.extend(batch)
            if len(batch) < 200:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("zt reason fetch failed (%s): %s", key, str(e)[:100])
        if not info:
            return {}

    out: dict[str, dict] = {}
    for it in info:
        code = str(it.get("code") or "").zfill(6)
        if len(code) != 6 or is_bj_code(code):
            continue
        seal = it.get("order_amount")
        mv = it.get("currency_value")
        turnover = it.get("turnover_rate")
        # 成交额 ≈ 换手率 × 流通市值（同花顺池不直接给成交额）
        amount_yi = None
        if mv and turnover:
            amount_yi = round(float(mv) * float(turnover) / 100 / 1e8, 2)
        out[code] = {
            "name": it.get("name"),
            "close": it.get("latest"),
            "change_rate": it.get("change_rate"),
            "amount_yi": amount_yi,
            "reason": (it.get("reason_type") or "").strip() or None,
            "high_days": it.get("high_days"),
            "limit_up_type": it.get("limit_up_type"),
            "first_limit_time": _fmt_ts(it.get("first_limit_up_time")),
            "last_limit_time": _fmt_ts(it.get("last_limit_up_time")),
            "break_count": it.get("open_num"),
            "seal_amount_yi": round(seal / 1e8, 2) if seal is not None else None,
            "turnover_rate": turnover,
            "float_mv_yi": round(mv / 1e8, 2) if mv is not None else None,
        }
    log.info("zt reason fetched: %s -> %d stocks", key, len(out))
    _ZT_CACHE[key] = (time.time(), out)
    return out


# --------------------------------------------------------------------------- #
# 核心计算
# --------------------------------------------------------------------------- #

def _limit_rate(code: str) -> float:
    """创业板(300/301)/科创板(688) 20%，其余 10%。"""
    if code.startswith(("300", "301", "688")):
        return LIMIT_RATE_GEM
    return LIMIT_RATE_MAIN


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """给每个 code 的日度序列补上：昨收、涨停、20/60 日涨幅、振幅。"""
    df = df.copy()
    g = df.groupby("code", sort=False)
    df["prev_close"] = g["close"].shift(1)

    # 涨停
    df["limit_rate"] = df["code"].apply(_limit_rate)
    df["pct_chg"] = (df["close"] - df["prev_close"]) / df["prev_close"]
    df["is_limit"] = df["pct_chg"] >= df["limit_rate"] - 0.005  # -0.5% 容差（四舍五入）

    # 振幅 = (high - low) / prev_close
    df["amplitude"] = (df["high"] - df["low"]) / df["prev_close"]

    # 20 / 60 日涨幅（用 close / 20日前 close - 1）
    df["ret20"] = df["close"] / g["close"].shift(20) - 1
    df["ret60"] = df["close"] / g["close"].shift(60) - 1

    # 连板天数：从上一个非涨停日开始向前数
    df["streak"] = (df["is_limit"]
                    .astype(int)
                    .groupby([df["code"], (~df["is_limit"]).cumsum()])
                    .cumcount() + 1)
    df.loc[~df["is_limit"], "streak"] = 0

    # 年内涨停次数（近 250 交易日，"新面孔"代理）
    df["limit_count_1y"] = (df["is_limit"].astype(int)
                            .groupby(df["code"])
                            .rolling(window=250, min_periods=1)
                            .sum()
                            .reset_index(level=0, drop=True))

    return df


# --------------------------------------------------------------------------- #
# 七维打分（满分 100）
# --------------------------------------------------------------------------- #

def _score_price(close: float) -> int:
    """价格维度 15 分：≤5元 15；5-8 元 11；8-10 元 7；>10 元 3。"""
    if close <= 5:
        return 15
    if close <= 8:
        return 11
    if close <= 10:
        return 7
    return 3


def _score_amount(amount_yi: float) -> int:
    """流通市值缺失时的成交额代理 20 分。"""
    if 5 <= amount_yi <= 20:
        return 20
    if (1 <= amount_yi < 5) or (20 < amount_yi <= 50):
        return 15
    return 5


def _score_float_mv(float_mv_yi: float) -> int:
    """流通市值 20 分：20-60 亿 → 20；60-100 或 10-20 → 15；100-200 或 5-10 → 10；其余 5。"""
    if 20 <= float_mv_yi <= 60:
        return 20
    if 60 < float_mv_yi <= 100 or 10 <= float_mv_yi < 20:
        return 15
    if 100 < float_mv_yi <= 200 or 5 <= float_mv_yi < 10:
        return 10
    return 5


def _score_amplitude(amp: float) -> int:
    """换手率缺失时的振幅代理 15 分。"""
    if amp >= 0.05 and amp <= 0.20:
        return 15
    if amp > 0.20:
        return 10
    return 8


def _score_turnover(turnover_rate: float) -> int:
    """换手健康度 15 分：5%-20% → 15；20%-35% 或 3%-5% → 11；>35% → 6；<3% → 5。"""
    if 5 <= turnover_rate <= 20:
        return 15
    if 20 < turnover_rate <= 35 or 3 <= turnover_rate < 5:
        return 11
    if turnover_rate > 35:
        return 6
    return 5


def _score_seal_ratio(seal_ratio: float) -> int:
    """封单额/流通市值 15 分：≥3% → 15；1%-3% → 11；0.3%-1% → 7；<0.3% → 3。"""
    if seal_ratio >= 0.03:
        return 15
    if seal_ratio >= 0.01:
        return 11
    if seal_ratio >= 0.003:
        return 7
    return 3


def _score_chip(limit_count_1y: float) -> int:
    """筹码干净代理 10 分：年内涨停 ≤ 3 → 10；4-6 → 7；>6 → 3。"""
    if limit_count_1y <= 3:
        return 10
    if limit_count_1y <= 6:
        return 7
    return 3


def _score_position(ret20: float, is_open_board: bool) -> int:
    """板型与位置 10 分：20 日涨幅 ≤ 0.15 + 换手板 → 10；0.15-0.25 → 7；>0.25 → 4。"""
    if ret20 is None or np.isnan(ret20):
        return 0
    if ret20 <= 0.15:
        base = 10
    elif ret20 <= 0.25:
        base = 7
    else:
        base = 4
    # 一字板（开盘=涨停且全天没打开）扣 2 分
    if not is_open_board:
        base = max(0, base - 2)
    return base


def _score_theme(industry_limit_count: int) -> int:
    """题材纯度 15 分：行业今日涨停家数 ≥ 3 → 15；2 → 12；1 → 7。"""
    if industry_limit_count >= 3:
        return 15
    if industry_limit_count == 2:
        return 12
    if industry_limit_count == 1:
        return 7
    return 3


def _is_open_board(row: pd.Series) -> bool:
    """换手板 = 收盘是涨停 + 当日最高 ≠ 当日最低 ≠ 开盘（开板过）。"""
    if not row["is_limit"]:
        return False
    close_limit = row["close"] >= (row["prev_close"] * (1 + row["limit_rate"]) - 0.01)
    if not close_limit:
        return False
    # 一字板：open ≈ high ≈ low ≈ close 且 volume 极低
    tol = row["prev_close"] * 0.005
    if (abs(row["open"] - row["high"]) < tol
            and abs(row["open"] - row["low"]) < tol
            and abs(row["open"] - row["close"]) < tol):
        return False
    return True


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

# 全市场 400 天日线重算一次约 9s，加进程内缓存避免每次打开页面都等
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 120.0


def run_yaogu(db: Session, batch_date: date | None = None,
              force: bool = False, live: bool = False) -> dict:
    """执行妖股筛选，带 120s 进程内缓存；force=True 强制重算。

    live=True 时，若处于交易时段则抓全市场快照拼成当日临时 K 线参与筛选。
    """
    now = time.time()
    if (not force and _CACHE["data"] is not None
            and now - _CACHE["ts"] < _CACHE_TTL
            and (not live or _CACHE.get("live"))):
        log.info("yaogu: cache hit (%.0fs ago)", now - _CACHE["ts"])
        return copy.deepcopy(_CACHE["data"])

    result = _run_screen(db, batch_date, live=live)
    _CACHE["data"] = result
    _CACHE["ts"] = now
    _CACHE["live"] = result.get("data_mode") == "intraday"
    return copy.deepcopy(result)


def _load_live_bars(daily: pd.DataFrame) -> tuple[pd.DataFrame | None, bool]:
    """盘中把全市场快照拼成当日临时 K 线（不落库）。

    非交易时段、节假日或快照明显滞后时返回 (None, False)，由调用方回退到日线。
    """
    today = date.today()
    if today.weekday() >= 5:
        return None, False
    if not (dtime(9, 15) <= datetime.now().time() <= dtime(15, 35)):
        return None, False
    last = daily["trade_date"].max()
    # 当日日线已较完整时无需拼接快照；只同步了零星几只（如盘中半截同步）
    # 时仍然要用全市场快照补齐，否则候选池会几乎为空
    today_rows = int((daily["trade_date"] == pd.Timestamp(today)).sum())
    if today_rows >= 1000:
        return None, False
    spot = fetch_spot_bars()
    if spot is None or spot.empty:
        return None, False
    # 节假日/停市：快照涨跌幅几乎全为 0，视为无效
    has_chg = spot["pct_chg"].notna()
    if has_chg.sum() < 1000 or (
            spot.loc[has_chg, "pct_chg"].abs() > 0.01).mean() < 0.1:
        log.warning("yaogu live: spot looks stale, skip")
        return None, False
    # 不按已有日线覆盖范围过滤：全市场快照都要进来，否则漏掉大半涨停股
    live = spot.copy()
    if live.empty:
        return None, False
    live["trade_date"] = pd.Timestamp(today)
    live["high"] = live[["high", "close"]].max(axis=1)
    live["low"] = live[["low", "close"]].min(axis=1)
    live["open"] = live["open"].where(live["open"] > 0, live["close"])
    live["volume"] = live["volume"].fillna(0)
    live["amount"] = live["amount"].fillna(0)
    return live[["code", "trade_date", "open", "high", "low", "close",
                 "volume", "amount"]], True


def _pct(v) -> float | None:
    """小数 → 百分数（保留 1 位），NaN/None → None。"""
    if v is None or pd.isna(v):
        return None
    return round(float(v) * 100, 1)


def _parse_streak(text: str | None) -> int | None:
    """同花顺 high_days → 连板数：'首板'→1，'3连板'→3，'6天5板'→5。"""
    if not text:
        return None
    if "首板" in text:
        return 1
    nums = re.findall(r"\d+", text)
    return int(nums[-1]) if nums else None


def _build_ladder(today_df: pd.DataFrame, stocks: dict[str, Stock],
                  reasons: dict[str, dict]) -> list[dict]:
    """当日涨停按行业分组，附涨停原因与封板细节。

    以同花顺涨停池（全市场）为准：本地 daily_quotes 只覆盖部分股票，
    不能用来判定"今天谁涨停"。日线仅用于补充 20 日涨幅、振幅等指标。
    池子不可用时退回日线口径（覆盖不全，仅作降级）。
    """
    quote_map = ({r["code"]: r for _, r in today_df.iterrows()}
                 if not today_df.empty else {})

    items: list[dict] = []
    for code, info in reasons.items():
        r = quote_map.get(code)
        s = stocks.get(code)
        streak = _parse_streak(info.get("high_days"))
        if streak is None and r is not None:
            streak = int(r["streak"])
        if streak is None:
            streak = 1

        close = info.get("close")
        if close is None and r is not None:
            close = round(float(r["close"]), 2)
        amount = info.get("amount_yi")
        if amount is None and r is not None:
            amount = round(float(r["amount"] or 0) / 1e8, 2)

        if info.get("limit_up_type"):
            is_open = info["limit_up_type"] != "一字板"
        elif r is not None:
            is_open = _is_open_board(r)
        else:
            is_open = None

        items.append({
            "code": code,
            "name": (s.name if s and s.name else info.get("name")),
            "industry": (s.industry if s and s.industry else None),
            "close": close,
            "amount_yi": amount,
            "streak": streak,
            "is_open_board": is_open,
            "reason": info.get("reason"),
            "high_days": info.get("high_days"),
            "limit_up_type": info.get("limit_up_type"),
            "first_limit_time": info.get("first_limit_time"),
            "last_limit_time": info.get("last_limit_time"),
            "break_count": info.get("break_count"),
            "seal_amount_yi": info.get("seal_amount_yi"),
            "turnover_rate": info.get("turnover_rate"),
            "float_mv_yi": info.get("float_mv_yi"),
            "ret20_pct": _pct(r["ret20"]) if r is not None else None,
            "amplitude_pct": _pct(r["amplitude"]) if r is not None else None,
        })

    if not items:
        # 降级：池子拿不到时用日线重算（覆盖率不足，可能少很多）
        ladder_df = today_df[today_df["is_limit"]
                             & (today_df["streak"] >= 1)].copy()
        if ladder_df.empty:
            return []
        for _, r in ladder_df.iterrows():
            s = stocks.get(r["code"])
            items.append({
                "code": r["code"],
                "name": s.name if s else None,
                "industry": (s.industry if s and s.industry else None),
                "close": round(float(r["close"]), 2),
                "amount_yi": round(float(r["amount"] or 0) / 1e8, 2),
                "streak": int(r["streak"]),
                "is_open_board": _is_open_board(r),
                "reason": None, "high_days": None, "limit_up_type": None,
                "first_limit_time": None, "last_limit_time": None,
                "break_count": None, "seal_amount_yi": None,
                "turnover_rate": None, "float_mv_yi": None,
                "ret20_pct": _pct(r["ret20"]),
                "amplitude_pct": _pct(r["amplitude"]),
            })

    groups: dict[str, list[dict]] = {}
    for it in items:
        ind = it.pop("industry") or "未分类"
        groups.setdefault(ind, []).append(it)

    ladder = []
    for ind, arr in groups.items():
        arr.sort(key=lambda x: (-(x["streak"] or 1),
                                -(x["amount_yi"] or 0)))
        ladder.append({
            "industry": ind,
            "count": len(arr),
            "max_streak": max(x["streak"] or 1 for x in arr),
            "stocks": arr[:40],
        })
    ladder.sort(key=lambda g: (g["count"], g["max_streak"]), reverse=True)
    return ladder


def _run_screen(db: Session, batch_date: date | None = None,
                live: bool = False) -> dict:
    """实际执行筛选，返回 {batch_date, candidates: [...], ladder: [...]}。"""
    batch_date = batch_date or date.today()
    log.info("yaogu screen start @ %s (live=%s)", batch_date, live)

    # 1) 加载行情（live 时追加当日快照临时 K 线）
    df = _load_quotes(db, days=400)
    if df.empty:
        return {"batch_date": str(batch_date), "selected": [],
                "ladder": [], "reason": "no daily_quotes data"}
    live_df, is_live = (None, False)
    if live:
        live_df, is_live = _load_live_bars(df)
    if live_df is not None:
        df = pd.concat([df, live_df], ignore_index=True)
        # 盘中可能已有零星当日日线（半截同步），以快照为准去重
        df = df.drop_duplicates(subset=["code", "trade_date"], keep="last")
        df = df.sort_values(["code", "trade_date"]).reset_index(drop=True)
        log.info("yaogu live: appended %d intraday bars", len(live_df))
    df = _enrich(df)

    # 2) 选最新一个有行情的交易日
    all_dates = sorted(df["trade_date"].unique())
    if not all_dates:
        return {"batch_date": str(batch_date), "selected": [],
                "ladder": [], "reason": "empty"}
    latest = all_dates[-1]
    today_df = df[df["trade_date"] == latest].copy()
    # 确保每个 code 只取一行（已按 trade_date 过滤）
    today_df = today_df.drop_duplicates(subset="code", keep="last")
    log.info("yaogu: latest date=%s codes=%d", latest.date(), len(today_df))

    # 3) 行业映射
    stocks = _load_stocks(db)
    st_names = {c: s.name for c, s in stocks.items()
                if stocks[c].name and "ST" in stocks[c].name}

    # 同花顺涨停池（全市场口径）——涨停名单/原因/封板细节的主数据源
    reasons = _fetch_limit_reasons(latest.date())

    # 全市场今日涨停总数（无行业数据时用此代理题材热度）
    if reasons:
        total_limit_today = len(reasons)
        industry_limit_counts = Counter(
            stocks[c].industry for c in reasons
            if c in stocks and stocks[c].industry)
    else:
        total_limit_today = int(today_df["is_limit"].sum())
        industry_limit_counts = Counter(
            stocks[c].industry for c in today_df[today_df["is_limit"]]["code"]
            if c in stocks and stocks[c].industry)

    # 4) 八层漏斗 + 六维打分 → 候选池
    # 候选池只可能来自今日涨停，因此以同花顺涨停池为名单，
    # 日线行仅用于补充位置/振幅/历史涨停次数；日线缺失时用池子字段兜底
    quote_map = {r["code"]: r for _, r in today_df.iterrows()}
    if reasons:
        cand_codes = list(reasons.keys())
    else:
        cand_codes = [c for c, r in quote_map.items() if bool(r["is_limit"])]

    candidates = []
    for code in cand_codes:
        row = quote_map.get(code)
        info = reasons.get(code) or {}

        # --- L1 黑名单 ---
        if code in st_names:
            continue  # ST
        s = stocks.get(code)
        if s and s.list_date and (latest - s.list_date).days < 60:
            continue  # 上市 < 60 天

        # --- L2 价格 ≤ 10 元（日线优先，池子兜底）---
        close = None
        if row is not None and not pd.isna(row["close"]):
            close = float(row["close"])
        elif info.get("close") is not None:
            close = float(info["close"])
        if close is None or close > 10:
            continue

        # --- L3 成交额 ∈ [1亿, 30亿] ---
        if row is not None and not pd.isna(row["amount"]):
            amount_yi = float(row["amount"]) / 1e8
        else:
            amount_yi = float(info.get("amount_yi") or 0)
        if amount_yi < 1 or amount_yi > 30:
            continue

        # --- L4 位置不高：20日 ≤ 30%（无历史数据时不做拦截）---
        ret20 = (row["ret20"] if row is not None
                 and not pd.isna(row["ret20"]) else None)
        if ret20 is not None and ret20 > 0.30:
            continue

        # --- L5 今日涨停：候选来自涨停池，天然满足 ---
        if row is not None and not info and not bool(row["is_limit"]):
            continue

        # --- L6-L8 走六维打分 ---
        streak = None
        if row is not None and not pd.isna(row["streak"]):
            streak = int(row["streak"])
        if not streak:
            streak = _parse_streak(info.get("high_days")) or 1

        if info.get("limit_up_type"):
            is_open = info["limit_up_type"] != "一字板"
        elif row is not None:
            is_open = _is_open_board(row)
        else:
            is_open = True

        industry = s.industry if s else None
        if industry_limit_counts and industry:
            ind_cnt = industry_limit_counts.get(industry, 0)
        else:
            # 无行业数据 → 用全市场涨停热度代理（涨停越多题材越强）
            ind_cnt = total_limit_today if total_limit_today >= 3 else 0

        amplitude = (float(row["amplitude"])
                     if row is not None and not pd.isna(row["amplitude"])
                     else 0.0)
        limit_count_1y = (float(row["limit_count_1y"])
                          if row is not None
                          and not pd.isna(row["limit_count_1y"]) else 0.0)

        # 真值优先：同花顺涨停池已给出流通市值 / 换手率 / 封单额，缺失才回落代理
        float_mv = info.get("float_mv_yi")
        turnover = info.get("turnover_rate")
        seal_yi = info.get("seal_amount_yi")
        seal_ratio = (seal_yi / float_mv) if (seal_yi is not None and float_mv) else None
        fallback = float_mv is None or turnover is None

        s_price = _score_price(close)
        s_fmv = (_score_float_mv(float(float_mv)) if float_mv is not None
                 else _score_amount(amount_yi))
        s_theme = _score_theme(ind_cnt)
        s_chip = _score_chip(limit_count_1y)
        s_turn = (_score_turnover(float(turnover)) if turnover is not None
                  else _score_amplitude(amplitude))
        s_seal = _score_seal_ratio(seal_ratio) if seal_ratio is not None else 8
        s_pos = _score_position(ret20, is_open)
        total = (s_price + s_fmv + s_theme + s_chip + s_turn + s_seal + s_pos)

        # 八层漏斗打分门槛
        if total < 70:
            continue

        candidates.append({
            "code": code,
            "name": s.name if s else info.get("name"),
            "industry": industry,
            "close": round(close, 2),
            "amount_yi": round(amount_yi, 2),
            "ret20_pct": round((ret20 or 0) * 100, 1),
            "amplitude_pct": round(amplitude * 100, 1),
            "streak": streak,
            "is_open_board": is_open,
            "limit_count_1y": int(limit_count_1y),
            "industry_limit_count": ind_cnt,
            "score": total,
            "score_fallback": fallback,
            "float_mv_yi": float_mv,
            "turnover_rate": turnover,
            "seal_amount_yi": seal_yi,
            "scores": {"price": s_price, "float_mv": s_fmv,
                       "theme": s_theme, "chip": s_chip,
                       "turnover": s_turn, "seal": s_seal,
                       "position": s_pos},
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:30]  # 上限
    log.info("yaogu: limit-ups=%d, candidates=%d (threshold 70)",
             len(cand_codes), len(candidates))

    # 5) 连板梯队：按行业分组，行业内含全部涨停 + 涨停原因
    ladder = _build_ladder(today_df, stocks, reasons)

    log.info("yaogu: %d candidates, %d industry ladders, %d limit-ups",
             len(candidates), len(ladder), total_limit_today)
    result = {
        "batch_date": str(batch_date),
        "latest_quote_date": str(latest.date()),
        "data_mode": "intraday" if is_live else "close",
        "candidates": candidates,
        "ladder": ladder,
        "stats": {
            "total_limit_today": total_limit_today,
            "total_limit_industries": len(industry_limit_counts) or None,
            "top_theme": (industry_limit_counts.most_common(1)[0][0]
                          if industry_limit_counts else None),
        },
    }
    # 收盘数据自动落库，供历史回看/后续统计；失败不影响接口返回
    if not is_live:
        try:
            save_snapshot(db, result)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.warning("yaogu snapshot save failed: %s", str(e)[:120])
    return result


# --------------------------------------------------------------------------- #
# 每日快照 / 历史
# --------------------------------------------------------------------------- #

def _rows_from_ladder(trade_date: date, ladder: list[dict],
                      score_map: dict[str, int] | None = None) -> list[dict]:
    """把梯队结构摊平成 yaogu_snapshot 行。"""
    score_map = score_map or {}
    rows = []
    for g in ladder:
        for s in g.get("stocks", []):
            rows.append({
                "trade_date": trade_date,
                "code": s["code"],
                "name": s.get("name"),
                "industry": g.get("industry"),
                "close": s.get("close"),
                "amount_yi": s.get("amount_yi"),
                "streak": s.get("streak") or 1,
                "high_days": s.get("high_days"),
                "limit_up_type": s.get("limit_up_type"),
                "first_limit_time": s.get("first_limit_time"),
                "last_limit_time": s.get("last_limit_time"),
                "break_count": s.get("break_count"),
                "seal_amount_yi": s.get("seal_amount_yi"),
                "turnover_rate": s.get("turnover_rate"),
                "float_mv_yi": s.get("float_mv_yi"),
                "ret20_pct": s.get("ret20_pct"),
                "amplitude_pct": s.get("amplitude_pct"),
                "reason": s.get("reason"),
                "is_open_board": s.get("is_open_board"),
                "score": score_map.get(s["code"]),
            })
    return rows


def _upsert_snapshot(db: Session, trade_date: date, rows: list[dict]) -> int:
    if not rows:
        return 0
    # 先清掉该日已不在涨停池里的旧行（换数据源/重算口径变化时会残留）
    codes = [r["code"] for r in rows]
    (db.query(YaoguSnapshot)
     .filter(YaoguSnapshot.trade_date == trade_date,
             YaoguSnapshot.code.notin_(codes))
     .delete(synchronize_session=False))
    stmt = insert(YaoguSnapshot).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "code"],
        set_={c: getattr(stmt.excluded, c) for c in (
            "name", "industry", "close", "amount_yi", "streak", "high_days",
            "limit_up_type", "first_limit_time", "last_limit_time",
            "break_count", "seal_amount_yi", "turnover_rate", "float_mv_yi",
            "ret20_pct", "amplitude_pct", "reason", "is_open_board", "score")},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def save_snapshot(db: Session, result: dict,
                  trade_date: date | None = None) -> int:
    """把一次筛选结果（梯队）写入快照表，按 (trade_date, code) 覆盖。"""
    ladder = result.get("ladder") or []
    if not ladder:
        return 0
    td = trade_date or date.fromisoformat(
        result.get("latest_quote_date") or result["batch_date"])
    score_map = {c["code"]: c.get("score")
                 for c in result.get("candidates", [])}
    return _upsert_snapshot(db, td, _rows_from_ladder(td, ladder, score_map))


def save_today_snapshot(db: Session) -> dict:
    """收盘后强制重算并落库（定时任务入口）。"""
    res = run_yaogu(db, force=True, live=False)
    n = save_snapshot(db, res)
    return {"trade_date": res.get("latest_quote_date"), "rows": n}


def backfill_snapshots(db: Session, days: int = 120,
                       sleep: float = 0.25) -> dict:
    """回填历史快照：日线重算涨停 + 同花顺历史涨停池补原因/封板细节。

    同花顺历史数据约保留 1 年，更早日期只能落日线口径（无原因）。
    """
    df = _load_quotes(db, days=400)
    if df.empty:
        return {"dates": 0, "rows": 0}
    df = _enrich(df)
    stocks = _load_stocks(db)
    dates = sorted({pd.Timestamp(x).date()
                    for x in df["trade_date"].unique()})[-days:]

    total = done = skipped = 0
    first_saved = last_saved = None
    for i, td in enumerate(dates, 1):
        today_df = df[df["trade_date"] == pd.Timestamp(td)]
        today_df = today_df.drop_duplicates(subset="code", keep="last")
        reasons = _fetch_limit_reasons(td)
        if not reasons:
            # 同花顺池无该日期数据（超出约 1 年）→ 跳过；若库里存过
            # 日线口径的残缺快照，一并删除，避免历史统计被少量样本带偏
            if td < date.today() - timedelta(days=330):
                (db.query(YaoguSnapshot)
                 .filter(YaoguSnapshot.trade_date == td)
                 .delete(synchronize_session=False))
                db.commit()
            skipped += 1
            time.sleep(sleep)
            continue
        ladder = _build_ladder(today_df, stocks, reasons)
        n = _upsert_snapshot(db, td, _rows_from_ladder(td, ladder))
        total += n
        done += 1
        first_saved = first_saved or td
        last_saved = td
        if i % 20 == 0:
            log.info("backfill %d/%d dates, %d rows", i, len(dates), total)
        time.sleep(sleep)
    log.info("yaogu backfill done: %d dates saved, %d skipped, %d rows",
             done, skipped, total)
    return {"dates": done, "skipped": skipped, "rows": total,
            "start": str(first_saved) if first_saved else None,
            "end": str(last_saved) if last_saved else None}


def history_dates(db: Session, limit: int = 120) -> list[dict]:
    """快照日期列表（倒序），带每日涨停数/最高连板/涉及行业数。"""
    rows = (db.query(YaoguSnapshot.trade_date,
                     func.count(YaoguSnapshot.code),
                     func.max(YaoguSnapshot.streak),
                     func.count(func.distinct(YaoguSnapshot.industry)))
            .group_by(YaoguSnapshot.trade_date)
            .order_by(YaoguSnapshot.trade_date.desc())
            .limit(limit).all())
    return [{"trade_date": str(d), "count": int(c),
             "max_streak": int(ms or 1), "industries": int(ind)}
            for d, c, ms, ind in rows]


def history_detail(db: Session, trade_date: date) -> dict:
    """某日快照，按行业分组返回（结构与实时梯队一致）。"""
    rows = (db.query(YaoguSnapshot)
            .filter(YaoguSnapshot.trade_date == trade_date)
            .all())
    if not rows:
        return {"trade_date": str(trade_date), "ladder": [],
                "stats": {"total_limit": 0, "industries": 0,
                          "max_streak": 0, "top_theme": None}}

    groups: dict[str, list[YaoguSnapshot]] = {}
    for r in rows:
        groups.setdefault(r.industry or "未分类", []).append(r)

    def _item(r: YaoguSnapshot) -> dict:
        return {
            "code": r.code,
            "name": r.name,
            "close": float(r.close) if r.close is not None else None,
            "amount_yi": float(r.amount_yi) if r.amount_yi is not None else None,
            "streak": r.streak or 1,
            "is_open_board": r.is_open_board,
            "reason": r.reason,
            "high_days": r.high_days,
            "limit_up_type": r.limit_up_type,
            "first_limit_time": r.first_limit_time,
            "last_limit_time": r.last_limit_time,
            "break_count": r.break_count,
            "seal_amount_yi": (float(r.seal_amount_yi)
                               if r.seal_amount_yi is not None else None),
            "turnover_rate": (float(r.turnover_rate)
                              if r.turnover_rate is not None else None),
            "float_mv_yi": (float(r.float_mv_yi)
                            if r.float_mv_yi is not None else None),
            "ret20_pct": (float(r.ret20_pct)
                          if r.ret20_pct is not None else None),
            "amplitude_pct": (float(r.amplitude_pct)
                              if r.amplitude_pct is not None else None),
            "score": r.score,
        }

    ladder = []
    for ind, grp in groups.items():
        grp.sort(key=lambda x: (-(x.streak or 1),
                                -(float(x.amount_yi or 0))))
        ladder.append({
            "industry": ind,
            "count": len(grp),
            "max_streak": max((x.streak or 1) for x in grp),
            "stocks": [_item(x) for x in grp],
        })
    ladder.sort(key=lambda g: (g["count"], g["max_streak"]), reverse=True)

    return {
        "trade_date": str(trade_date),
        "ladder": ladder,
        "stats": {
            "total_limit": len(rows),
            "industries": len(groups),
            "max_streak": max((x.streak or 1) for x in rows),
            "top_theme": (max(ladder, key=lambda g: g["count"])["industry"]
                          if ladder else None),
        },
    }
