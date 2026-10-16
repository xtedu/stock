"""手动触发任务：同步行情/基本面、筛选、区间计算、监控。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import datafeed, money_flow, monitor, safety_zone, screener
from ..services.datafeed import sync_spot_snapshot

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("/sync-quotes")
def sync_quotes(years: int = 5, limit: int | None = None,
                db: Session = Depends(get_db)):
    """全市场日线增量同步（东财优先、失败降级新浪，限速 + 熔断）。

    limit 用于小批量试跑（如 limit=40 只沪深票）。
    """
    return datafeed.sync_all_quotes(db, years=years, limit=limit)


@router.post("/sync-fundamentals")
def sync_fundamentals(years: int = 5, db: Session = Depends(get_db)):
    """财务指标 + 估值历史全量同步，耗时较长（建议后台执行）。"""
    return datafeed.sync_all_fundamentals(db, years=years)


@router.post("/sync-one/{code}")
def sync_one(code: str, db: Session = Depends(get_db)):
    return datafeed.sync_stock_fundamentals(db, code)


@router.post("/sync-industry")
def sync_industry(db: Session = Depends(get_db)):
    """同步申万二级行业分类到 stocks.industry（准静态，手动触发）。"""
    return datafeed.sync_industry(db)


@router.post("/screen")
def screen(db: Session = Depends(get_db)):
    return screener.run_screen(db)


@router.post("/calc-zones")
def calc_zones(db: Session = Depends(get_db)):
    return safety_zone.calc_zones(db)


@router.post("/intraday")
def intraday_run(db: Session = Depends(get_db)):
    """盘中链手动触发：快照 → 资金流 → 筛选 → 区间 → 监控（约 1-2 分钟）。"""
    from ..services.screener import run_screen
    n = sync_spot_snapshot(db)
    mf = money_flow.sync_money_flow(db, intraday=True)
    r1 = run_screen(db, intraday=True)
    r2 = safety_zone.calc_zones(db, intraday=True)
    r3 = monitor.check_and_notify(db)
    return {"spot": n, "money_flow": mf, "screen": r1, "zones": r2,
            "monitor": r3}


@router.post("/money-flow")
def sync_money_flow(intraday: bool = False, db: Session = Depends(get_db)):
    """资金流同步：intraday=true 取盘中累计值，false 取收盘官方数据。"""
    return money_flow.sync_money_flow(db, intraday=intraday)


@router.post("/monitor")
def monitor_now(db: Session = Depends(get_db)):
    return monitor.check_and_notify(db)
