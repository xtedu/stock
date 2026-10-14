"""APScheduler 定时任务：盘后采集、区间计算、盘中监控。"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal
from .services import datafeed, safety_zone, screener
from .services.monitor import run_monitor_job

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _with_db(fn, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, **kwargs)
    except Exception as e:
        log.exception("scheduled job failed: %s", e)
    finally:
        db.close()


def _intraday_chain() -> None:
    """14:00 盘中链：快照 → 筛选 → 区间 → 监控提醒（收盘前出结果）。"""
    db = SessionLocal()
    try:
        n = datafeed.sync_spot_snapshot(db)
        log.info("intraday: spot synced %d", n)
        screener.run_screen(db, intraday=True)
        safety_zone.calc_zones(db, intraday=True)
        from .services.monitor import check_and_notify
        check_and_notify(db)
        log.info("intraday chain done")
    except Exception as e:
        log.exception("intraday chain failed: %s", e)
    finally:
        db.close()


def setup_jobs() -> None:
    # 交易日 14:00 盘中链：快照+筛选+区间+提醒，收盘前出结果
    scheduler.add_job(_intraday_chain, "cron", day_of_week="mon-fri",
                      hour=14, minute=0, id="intraday_run",
                      replace_existing=True, max_instances=1,
                      misfire_grace_time=1800)
    # 交易日 15:40 收盘行情（6 线程约 15-20 分钟）
    scheduler.add_job(lambda: _with_db(datafeed.sync_all_quotes, years=5),
                      "cron", day_of_week="mon-fri", hour=15, minute=40,
                      id="sync_quotes", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
    # 16:10 筛选 + 16:15 区间计算
    scheduler.add_job(lambda: _with_db(screener.run_screen),
                      "cron", day_of_week="mon-fri", hour=16, minute=10,
                      id="screen", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    scheduler.add_job(lambda: _with_db(safety_zone.calc_zones),
                      "cron", day_of_week="mon-fri", hour=16, minute=15,
                      id="calc_zones", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 周六 09:00 全市场基本面同步（财务 + 估值，耗时数小时）
    scheduler.add_job(lambda: _with_db(datafeed.sync_all_fundamentals, years=5),
                      "cron", day_of_week="sat", hour=9, minute=0,
                      id="sync_fundamentals", replace_existing=True,
                      max_instances=1, misfire_grace_time=86400)
    # 盘中每 30 分钟监控（9:30-15:00）
    scheduler.add_job(run_monitor_job, "cron", day_of_week="mon-fri",
                      hour="9-14", minute="*/30", id="monitor",
                      replace_existing=True)


def start() -> None:
    setup_jobs()
    if not scheduler.running:
        scheduler.start()
        log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
