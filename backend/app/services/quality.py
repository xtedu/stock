"""数据质量门禁：覆盖率/完整率落库，供下游判断是否降级。"""
import logging
from datetime import date

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import DataQuality

log = logging.getLogger(__name__)


def record(db: Session, dataset: str, as_of: date, metric: str,
           value: float | None, status: str = "ok",
           detail: dict | None = None) -> None:
    stmt = insert(DataQuality).values(
        dataset=dataset, as_of=as_of, metric=metric, value=value,
        status=status, detail=detail)
    stmt = stmt.on_conflict_do_update(
        index_elements=["dataset", "as_of", "metric"],
        set_={"value": stmt.excluded.value, "status": stmt.excluded.status,
              "detail": stmt.excluded.detail})
    db.execute(stmt)


def status_of(value: float | None, warn: float, fail: float) -> str:
    """value 低于 warn 记 warn、低于 fail 记 fail。"""
    if value is None:
        return "fail"
    if value < fail:
        return "fail"
    if value < warn:
        return "warn"
    return "ok"
