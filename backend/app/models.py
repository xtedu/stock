from datetime import date

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer, BigInteger,
                        Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Stock(Base):
    __tablename__ = "stocks"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    industry: Mapped[str | None] = mapped_column(String(50))
    market: Mapped[str | None] = mapped_column(String(10))
    list_date: Mapped[Date | None] = mapped_column(Date)
    # 流通市值（元），用于消息消化度的市值分层
    float_mv: Mapped[float | None] = mapped_column(Numeric(20, 2))


# 沪深主板前缀：60x 沪市主板，00x 深市主板（含原中小板 002/003）
# 创业板 300/301、科创板 688/689、北交所 4/8/92 均不在本项目范围内
MAIN_BOARD_PREFIXES = ("60", "00")


def is_main_board(code: str) -> bool:
    """是否沪深主板（本项目唯一股票池口径）。"""
    return code.startswith(MAIN_BOARD_PREFIXES)


class DailyQuote(Base):
    __tablename__ = "daily_quotes"
    __table_args__ = (UniqueConstraint("code", "trade_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    trade_date: Mapped[Date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Numeric(10, 2))
    high: Mapped[float | None] = mapped_column(Numeric(10, 2))
    low: Mapped[float | None] = mapped_column(Numeric(10, 2))
    close: Mapped[float | None] = mapped_column(Numeric(10, 2))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2))


class FinIndicator(Base):
    __tablename__ = "fin_indicators"
    __table_args__ = (UniqueConstraint("code", "report_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    report_date: Mapped[Date] = mapped_column(Date)
    roe: Mapped[float | None] = mapped_column(Numeric(10, 2))
    revenue_yoy: Mapped[float | None] = mapped_column(Numeric(10, 2))
    profit_yoy: Mapped[float | None] = mapped_column(Numeric(10, 2))
    debt_ratio: Mapped[float | None] = mapped_column(Numeric(10, 2))
    ocf: Mapped[float | None] = mapped_column(Numeric(18, 2))
    np_deducted: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ocf_np_ratio: Mapped[float | None] = mapped_column(Numeric(12, 2))
    pe: Mapped[float | None] = mapped_column(Numeric(12, 2))
    pb: Mapped[float | None] = mapped_column(Numeric(12, 2))


class ValuationDaily(Base):
    __tablename__ = "valuation_daily"
    __table_args__ = (UniqueConstraint("code", "trade_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    trade_date: Mapped[Date] = mapped_column(Date)
    pe: Mapped[float | None] = mapped_column(Numeric(10, 2))
    pb: Mapped[float | None] = mapped_column(Numeric(10, 2))


class StockPool(Base):
    __tablename__ = "stock_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    reason: Mapped[dict | None] = mapped_column(JSONB)
    batch_date: Mapped[Date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SafetyZone(Base):
    __tablename__ = "safety_zones"
    __table_args__ = (UniqueConstraint("code", "calc_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    zone_low: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_high: Mapped[float | None] = mapped_column(Numeric(10, 2))
    method: Mapped[str | None] = mapped_column(String(30))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    calc_date: Mapped[Date] = mapped_column(Date)


class IntradaySpot(Base):
    """盘中全市场快照（每日覆盖更新，不进正式日线）。"""
    __tablename__ = "intraday_spot"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    pe: Mapped[float | None] = mapped_column(Numeric(10, 2))
    pb: Mapped[float | None] = mapped_column(Numeric(10, 2))
    trade_date: Mapped[Date] = mapped_column(Date)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(10))
    trigger: Mapped[str] = mapped_column(String(20), default="enter_zone")
    channels: Mapped[dict | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str | None] = mapped_column(String(10))
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_low: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_high: Mapped[float | None] = mapped_column(Numeric(10, 2))
    notified_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    ai_summary: Mapped[str | None] = mapped_column(Text)


class CompareAnalysis(Base):
    """股票池对比分析记录：指标对比 + 大模型次日定性研判。"""
    __tablename__ = "compare_analyses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[Date] = mapped_column(Date)
    codes: Mapped[list | None] = mapped_column(JSONB)
    items: Mapped[list | None] = mapped_column(JSONB)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class MoneyFlow(Base):
    """个股资金流（东方财富汇总口径，按单笔成交金额分档推算）。

    主力净额 = 超大单 + 大单净额。盘中为「开盘至今累计」，收盘后为全天值；
    同一交易日的 official 数据覆盖 intraday 数据。
    """
    __tablename__ = "money_flow"
    __table_args__ = (UniqueConstraint("code", "trade_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), ForeignKey("stocks.code"))
    trade_date: Mapped[Date] = mapped_column(Date)
    main_net: Mapped[float | None] = mapped_column(Numeric(18, 2))      # 主力净额（元）
    main_net_pct: Mapped[float | None] = mapped_column(Numeric(10, 2))  # 主力净占比 %
    super_net: Mapped[float | None] = mapped_column(Numeric(18, 2))     # 超大单净额（元）
    super_net_pct: Mapped[float | None] = mapped_column(Numeric(10, 2))
    big_net: Mapped[float | None] = mapped_column(Numeric(18, 2))       # 大单净额（元）
    big_net_pct: Mapped[float | None] = mapped_column(Numeric(10, 2))
    main_net_5d: Mapped[float | None] = mapped_column(Numeric(18, 2))   # 近5日主力净额（元）
    source: Mapped[str | None] = mapped_column(String(10))             # intraday / official
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class NewsItem(Base):
    """政策 / 快讯 / 公司公告聚合。source+title 唯一用于幂等去重。"""
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("source", "title"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20))      # cls/em/sina/cctv/notice
    category: Mapped[str | None] = mapped_column(String(20))  # policy/macro/industry/company
    title: Mapped[str] = mapped_column(String(400))
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True))
    trade_date: Mapped[date] = mapped_column(Date)
    related_codes: Mapped[list | None] = mapped_column(JSONB)
    # 利好消化度：{code: {ret5, day_chg, vol_ratio, from_high20, state}}
    digest: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class NewsBrief(Base):
    """每日消息面 AI 决策简报（一天一条）。"""
    __tablename__ = "news_briefs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brief_date: Mapped[date] = mapped_column(Date, unique=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class CalendarEvent(Base):
    """前瞻事件日历：宏观数据、限售解禁、业绩预告。

    与 news_items 的区别是「还没发生」，用于提前布局/规避。
    """
    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("kind", "event_date", "title"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))   # macro / release / forecast
    event_date: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    importance: Mapped[int] = mapped_column(Integer, default=1)  # 1-3
    related_codes: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class IndexDaily(Base):
    """指数日线（基准用）：沪深300 / 中证500 / 中证1000。"""
    __tablename__ = "index_daily"
    __table_args__ = (UniqueConstraint("code", "trade_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10))
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[float | None] = mapped_column(Numeric(12, 2))
    pct_chg: Mapped[float | None] = mapped_column(Numeric(10, 4))


class PickSnapshot(Base):
    """每日选股落库：策略评估与前向收益跟踪的底表。

    同一 (batch_date, code) 只保留一条，收盘官方快照覆盖回填记录。
    """
    __tablename__ = "picks"
    __table_args__ = (UniqueConstraint("batch_date", "code",
                                       name="uq_picks_date_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(10), index=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    close: Mapped[float | None] = mapped_column(Numeric(10, 2))
    industry: Mapped[str | None] = mapped_column(String(50))
    profit_growth: Mapped[float | None] = mapped_column(Numeric(10, 2))
    revenue_growth: Mapped[float | None] = mapped_column(Numeric(10, 2))
    roe_avg: Mapped[float | None] = mapped_column(Numeric(10, 2))
    debt_ratio: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_low: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_high: Mapped[float | None] = mapped_column(Numeric(10, 2))
    zone_status: Mapped[str | None] = mapped_column(String(10))
    ma20_slope_up: Mapped[bool | None] = mapped_column(Boolean)
    ret20: Mapped[float | None] = mapped_column(Numeric(8, 2))
    ret60: Mapped[float | None] = mapped_column(Numeric(8, 2))
    tags: Mapped[list | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(10))   # backfill / official
    fwd_ret_5: Mapped[float | None] = mapped_column(Numeric(10, 4))
    fwd_ret_10: Mapped[float | None] = mapped_column(Numeric(10, 4))
    fwd_ret_20: Mapped[float | None] = mapped_column(Numeric(10, 4))
    fwd_ret_60: Mapped[float | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class YaoguSnapshot(Base):
    """妖股/涨停池每日快照（一个交易日 × 一只涨停股一行）。

    落库后可回看梯队演化、行业涨停分布，也是后续「晋级/断板」统计的样本底表。
    """
    __tablename__ = "yaogu_snapshot"
    __table_args__ = (UniqueConstraint("trade_date", "code",
                                       name="uq_yaogu_snapshot_date_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str | None] = mapped_column(String(50))
    industry: Mapped[str | None] = mapped_column(String(50))
    close: Mapped[float | None] = mapped_column(Numeric(10, 2))
    amount_yi: Mapped[float | None] = mapped_column(Numeric(12, 2))
    streak: Mapped[int] = mapped_column(Integer, default=1)
    high_days: Mapped[str | None] = mapped_column(String(20))
    limit_up_type: Mapped[str | None] = mapped_column(String(20))
    first_limit_time: Mapped[str | None] = mapped_column(String(10))
    last_limit_time: Mapped[str | None] = mapped_column(String(10))
    break_count: Mapped[int | None] = mapped_column(Integer)
    seal_amount_yi: Mapped[float | None] = mapped_column(Numeric(12, 2))
    turnover_rate: Mapped[float | None] = mapped_column(Numeric(8, 2))
    float_mv_yi: Mapped[float | None] = mapped_column(Numeric(12, 2))
    ret20_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    amplitude_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    reason: Mapped[str | None] = mapped_column(Text)
    is_open_board: Mapped[bool | None] = mapped_column(Boolean)
    score: Mapped[int | None] = mapped_column(Integer)
    # 打分是否使用代理值（流通市值/换手率缺失时为 True）
    score_fallback: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class TradeCalendar(Base):
    """交易日历（真实开市日，替代按工作日近似）。"""
    __tablename__ = "dim_trade_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


class FeatureStockDaily(Base):
    """全市场每日截面特征（沪深主板），所有策略的统一取数底座。

    (code, as_of) 唯一；feature_version 用于区分口径版本，重算时同日覆盖。
    """
    __tablename__ = "feat_stock_daily"
    __table_args__ = (UniqueConstraint("code", "as_of",
                                       name="uq_feat_stock_daily_code_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
                                    autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    industry: Mapped[str | None] = mapped_column(String(50))
    close: Mapped[float | None] = mapped_column(Numeric(10, 2))
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2))
    pct_chg: Mapped[float | None] = mapped_column(Numeric(8, 4))
    ret5: Mapped[float | None] = mapped_column(Numeric(8, 4))
    ret20: Mapped[float | None] = mapped_column(Numeric(8, 4))
    ret60: Mapped[float | None] = mapped_column(Numeric(8, 4))
    ma20: Mapped[float | None] = mapped_column(Numeric(10, 2))
    ma60: Mapped[float | None] = mapped_column(Numeric(10, 2))
    ma250: Mapped[float | None] = mapped_column(Numeric(10, 2))
    bias20: Mapped[float | None] = mapped_column(Numeric(8, 4))
    ma20_slope_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    vol20: Mapped[float | None] = mapped_column(Numeric(8, 4))
    vol60: Mapped[float | None] = mapped_column(Numeric(8, 4))
    amt20: Mapped[float | None] = mapped_column(Numeric(18, 2))
    amt_ratio: Mapped[float | None] = mapped_column(Numeric(8, 4))
    pos60: Mapped[float | None] = mapped_column(Numeric(8, 4))
    limit_up: Mapped[bool | None] = mapped_column(Boolean)
    streak: Mapped[int | None] = mapped_column(Integer)
    pe: Mapped[float | None] = mapped_column(Numeric(10, 2))
    pb: Mapped[float | None] = mapped_column(Numeric(10, 2))
    pe_pct5y: Mapped[float | None] = mapped_column(Numeric(8, 2))
    roe_avg: Mapped[float | None] = mapped_column(Numeric(8, 2))
    profit_cagr: Mapped[float | None] = mapped_column(Numeric(10, 2))
    rev_cagr: Mapped[float | None] = mapped_column(Numeric(10, 2))
    debt_ratio: Mapped[float | None] = mapped_column(Numeric(8, 2))
    mf_main_net_pct: Mapped[float | None] = mapped_column(Numeric(8, 2))
    mf_main_net_5d: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ind_heat3: Mapped[int | None] = mapped_column(Integer)
    feature_version: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class LabelFwd(Base):
    """全市场前向收益标签，任何策略共用，不再各自回填。"""
    __tablename__ = "label_fwd"
    __table_args__ = (UniqueConstraint("code", "as_of", "horizon",
                                       name="uq_label_fwd_code_date_horizon"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
                                    autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    horizon: Mapped[int] = mapped_column(Integer)
    fwd_ret: Mapped[float | None] = mapped_column(Numeric(10, 4))
    bench_ret: Mapped[float | None] = mapped_column(Numeric(10, 4))
    excess: Mapped[float | None] = mapped_column(Numeric(10, 4))
    fwd_max_dd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    label_version: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class StrategyParam(Base):
    """策略参数中心：阈值/权重带版本，回测与实盘共用。"""
    __tablename__ = "strategy_params"
    __table_args__ = (UniqueConstraint("key", "version",
                                       name="uq_strategy_params_key_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    key: Mapped[str] = mapped_column(String(60))
    value: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[str] = mapped_column(String(20))
    effective_from: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class DataQuality(Base):
    """数据质量门禁记录：覆盖率/完整率/异常值，供降级与告警判断。"""
    __tablename__ = "data_quality"
    __table_args__ = (UniqueConstraint("dataset", "as_of", "metric",
                                       name="uq_data_quality_ds_date_metric"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
                                    autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(40))
    as_of: Mapped[date] = mapped_column(Date, index=True)
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[float | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str | None] = mapped_column(String(10))   # ok / warn / fail
    detail: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
