"""手动触发任务：同步行情/基本面、筛选、区间计算、监控。

所有长耗时任务走「立即返回 + 后台线程执行」，避免 HTTP 阻塞与 nginx 超时。
同一任务正在跑时，新请求直接返回 {"started": false, "reason": "already_running"}。

**后台线程统一用 scheduler._with_db 管理 Session**——FastAPI handler 注入的 db
只用于同步响应的即时查询；线程池/后台线程必须自己 SessionLocal()，避免
SQLAlchemy Session 跨线程损坏。
"""
import threading
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..services import (datafeed, money_flow, monitor, picks_track,
                        safety_zone, screener, yaogu)
from ..services.datafeed import sync_spot_snapshot
from ..scheduler import _acquire, _with_db

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 最近一次触发的任务状态（内存 dict，重启丢失）
_STATUS: dict[str, dict] = {}


def _run_async(job_id: str, fn) -> dict:
    """尝试获取锁，成功则后台线程跑 fn 并返回 started=true。"""
    lock = _acquire(job_id)
    if lock is None:
        last = _STATUS.get(job_id, {})
        return {"started": False, "reason": "already_running",
                "last": last.get("status"),
                "updated_at": last.get("updated_at")}

    def _wrapper():
        try:
            _STATUS[job_id] = {"status": "running",
                               "updated_at": datetime.now().isoformat()}
            result = fn()
            _STATUS[job_id] = {"status": "done", "result": result,
                               "updated_at": datetime.now().isoformat()}
        except Exception as e:
            _STATUS[job_id] = {"status": "failed", "error": str(e)[:500],
                               "updated_at": datetime.now().isoformat()}
        finally:
            lock.release()

    # 主线程先写 queued，确保 GET /status 在后台线程真正调度前也能看到
    _STATUS[job_id] = {"status": "queued",
                       "updated_at": datetime.now().isoformat()}
    threading.Thread(target=_wrapper, daemon=True).start()
    return {"started": True, "job_id": job_id}


@router.get("/status")
def job_status():
    """返回各任务最近一次触发的状态快照。"""
    return _STATUS


@router.post("/sync-quotes")
def sync_quotes(years: int = 5, limit: int | None = None,
                only_existing: bool = False, db: Session = Depends(get_db)):
    # **不用** handler 注入的 db，后台线程自己 SessionLocal()
    return _run_async("sync_quotes",
                      lambda: _with_db(datafeed.sync_all_quotes, years=years,
                                       limit=limit,
                                       only_existing=only_existing))


@router.post("/daily-bars")
def daily_bars(db: Session = Depends(get_db)):
    """收盘后用一次全市场快照写入当日日线（不逐只请求，抗限流）。"""
    return _run_async("daily_bars",
                      lambda: _with_db(datafeed.sync_daily_bars_from_spot))


@router.post("/sync-fundamentals")
def sync_fundamentals(years: int = 5, db: Session = Depends(get_db)):
    return _run_async("sync_fundamentals",
                      lambda: _with_db(datafeed.sync_all_fundamentals,
                                       years=years))


@router.post("/sync-one/{code}")
def sync_one(code: str, db: Session = Depends(get_db)):
    return datafeed.sync_stock_fundamentals(db, code)


@router.post("/sync-industry")
def sync_industry(db: Session = Depends(get_db)):
    return _run_async("sync_industry",
                      lambda: _with_db(datafeed.sync_industry))


@router.post("/screen")
def screen(db: Session = Depends(get_db)):
    return _run_async("screen",
                      lambda: _with_db(screener.run_screen))


@router.post("/calc-zones")
def calc_zones(db: Session = Depends(get_db)):
    return _run_async("calc_zones",
                      lambda: _with_db(safety_zone.calc_zones))


@router.post("/intraday")
def intraday_run(db: Session = Depends(get_db)):
    def _run():
        from ..services.screener import run_screen
        from ..services.monitor import check_and_notify
        db_local = SessionLocal()
        try:
            n = sync_spot_snapshot(db_local)
            mf = money_flow.sync_money_flow(db_local, intraday=True)
            r1 = run_screen(db_local, intraday=True)
            r2 = safety_zone.calc_zones(db_local, intraday=True)
            r3 = check_and_notify(db_local)
            return {"spot": n, "money_flow": mf, "screen": r1,
                    "zones": r2, "monitor": r3}
        finally:
            db_local.close()
    return _run_async("intraday", _run)


@router.post("/money-flow")
def sync_money_flow(intraday: bool = False, db: Session = Depends(get_db)):
    return _run_async("money_flow",
                      lambda: _with_db(money_flow.sync_money_flow,
                                       intraday=intraday))


@router.post("/monitor")
def monitor_now(db: Session = Depends(get_db)):
    return monitor.check_and_notify(db)


@router.post("/sync-index")
def sync_index(years: int = 5, db: Session = Depends(get_db)):
    """同步沪深300/中证500/中证1000 基准日线。"""
    return _run_async("sync_index",
                      lambda: _with_db(datafeed.sync_index_quotes, years=years))


@router.post("/picks-snapshot")
def picks_snapshot(limit: int = 3, db: Session = Depends(get_db)):
    """把当日选股结果落库（官方快照）。"""
    return _run_async("picks_snapshot",
                      lambda: _with_db(picks_track.save_snapshot, limit=limit))


@router.post("/picks-backfill")
def picks_backfill(days: int = 250, limit: int = 3,
                   db: Session = Depends(get_db)):
    """按历史交易日重算并落库选股快照（避免前视偏差，仍含幸存者偏差）。"""
    return _run_async("picks_backfill",
                      lambda: _with_db(picks_track.backfill_snapshots,
                                       days=days, limit=limit))


@router.post("/update-fwd")
def update_fwd(db: Session = Depends(get_db)):
    """回填选股快照的 T+5/10/20/60 前向收益。"""
    return _run_async("update_fwd",
                      lambda: _with_db(picks_track.update_forward_returns))


@router.post("/yaogu-snapshot")
def yaogu_snapshot(db: Session = Depends(get_db)):
    """收盘后重算并保存当日妖股快照（交易日 18:00 也会自动跑）。"""
    return _run_async("yaogu_snapshot",
                      lambda: _with_db(yaogu.save_today_snapshot))


@router.post("/yaogu-backfill")
def yaogu_backfill(days: int = 250, db: Session = Depends(get_db)):
    """回填历史快照（日线重算 + 同花顺历史涨停池原因）。"""
    return _run_async("yaogu_backfill",
                      lambda: _with_db(yaogu.backfill_snapshots, days=days))
