from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class StockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    industry: str | None = None


class PoolItemOut(BaseModel):
    code: str
    name: str | None = None
    score: float | None = None
    reason: dict[str, Any] | None = None
    batch_date: date
    source: str | None = None          # 入选来源：intraday(盘中) / official(收盘)
    industry: str | None = None         # 所属行业（申万二级）
    price: float | None = None         # 现价（优先盘中快照，否则最新收盘）
    open: float | None = None          # 最新交易日开盘价
    close: float | None = None         # 最新交易日收盘价
    change_pct: float | None = None    # 涨幅（现价相对上一交易日收盘，%）
    quote_date: date | None = None     # 行情日期
    # 安全区间 + MA20 斜率（左/右侧判定来自 safety_zone.detail JSONB）
    zone_low: float | None = None
    zone_high: float | None = None
    zone_status: str | None = None     # above / in / below
    ma20_slope_pct: float | None = None   # MA20 相对斜率 %/日（正值=拐头向上）
    ma20_slope_up: bool | None = None     # MA20 斜率是否 > 0
    trend_side: str | None = None         # right=右侧确认 / left=左侧观察 / None=缺失


class SafetyZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    zone_low: float | None = None
    zone_high: float | None = None
    method: str | None = None
    detail: dict[str, Any] | None = None
    calc_date: date


class QuoteOut(BaseModel):
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


class AlertLogOut(BaseModel):
    id: int
    code: str | None = None
    price: float | None = None
    zone_low: float | None = None
    zone_high: float | None = None
    notified_at: datetime
    ai_summary: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AlertRuleIn(BaseModel):
    code: str | None = None
    trigger: str = "enter_zone"
    channels: list[str] = ["feishu"]
    enabled: bool = True


class AlertRuleOut(AlertRuleIn):
    id: int

    model_config = ConfigDict(from_attributes=True)


class InsightIn(BaseModel):
    code: str


class CompareIn(BaseModel):
    codes: list[str]


class CompareItemOut(BaseModel):
    code: str
    name: str | None = None
    price: float | None = None            # 现价（优先盘中快照）
    change_pct: float | None = None       # 涨幅 %
    zone_low: float | None = None
    zone_high: float | None = None
    zone_status: str | None = None        # above(高于上沿) / in(区间内) / below(低于下沿)
    ma250_dev: float | None = None        # 相对 MA250 中枢乖离率 %
    score: float | None = None
    roe_avg: float | None = None
    rev_cagr_3y: float | None = None
    profit_cagr_3y: float | None = None
    pe_percentile: float | None = None
    main_net: float | None = None         # 主力净额（元）
    main_net_pct: float | None = None     # 主力净占比 %
    super_net: float | None = None        # 超大单净额（元）
    super_net_pct: float | None = None
    big_net: float | None = None          # 大单净额（元）
    big_net_pct: float | None = None
    main_net_5d: float | None = None      # 近5日主力净额（元）
    flow_date: date | None = None         # 资金流日期
    view: str | None = None               # 大模型定性判断：看多/中性/看空
    reason: str | None = None             # 判断理由
    support: float | None = None          # 支撑位
    resistance: float | None = None       # 压力位
    confidence: str | None = None         # 置信度：高/中/低


class CompareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_date: date
    codes: list[str] = []
    items: list[CompareItemOut] = []
    ai_summary: str | None = None
    created_at: datetime | None = None


class CompareBriefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_date: date
    codes: list[str] = []
    ai_summary: str | None = None
    created_at: datetime | None = None
