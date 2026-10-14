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
