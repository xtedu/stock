"""APScheduler 定时任务：盘后采集、区间计算、盘中监控。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time

from apscheduler.schedulers.background import BackgroundScheduler

from .db import SessionLocal
from .services import (datafeed, features, labels, money_flow, picks_track,
                       safety_zone, screener, trading_calendar, yaogu)
from .services.monitor import run_digest_job, run_monitor_job

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

# 进程内锁：同一任务在跑时，新触发直接返回"已在跑"，避免并发撞击数据源
_lock: dict[str, threading.Lock] = {}


def _acquire(job_id: str) -> threading.Lock | None:
    """获取指定任务的锁；已在跑则返回 None。"""
    lock = _lock.setdefault(job_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    return lock


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
        mf = money_flow.sync_money_flow(db, intraday=True)
        log.info("intraday: money flow %s", mf)
        screener.run_screen(db, intraday=True)
        safety_zone.calc_zones(db, intraday=True)
        from .services.monitor import check_and_notify
        check_and_notify(db)
        log.info("intraday chain done")
    except Exception as e:
        log.exception("intraday chain failed: %s", e)
    finally:
        db.close()


def _news_chain() -> None:
    """消息面链：抓取政策/快讯/公告 → 算利好消化度 → 同步前瞻日历 → AI 简报。"""
    from .services import calendar as cal_svc
    from .services import news as news_svc
    db = SessionLocal()
    try:
        log.info("news: %s", news_svc.sync_news(db))
        log.info("float mv: %s", news_svc.sync_float_mv(db))
        log.info("news digest: %s", news_svc.attach_digest(db))
        log.info("calendar: %s", cal_svc.sync_calendar(db))
        b = news_svc.build_brief(db)
        log.info("news brief: cached=%s ok=%s", b.get("cached"),
                 b.get("payload") is not None)
    except Exception as e:
        log.exception("news chain failed: %s", e)
    finally:
        db.close()


def setup_jobs() -> None:
    # 交易日 14:00 盘中链
    scheduler.add_job(_intraday_chain, "cron", day_of_week="mon-fri",
                      hour=14, minute=0, id="intraday_run",
                      replace_existing=True, max_instances=1,
                      misfire_grace_time=1800)
    # 交易日 15:40 收盘日线：一次全市场快照写入，避免逐只接口被限流
    scheduler.add_job(lambda: _with_db(datafeed.sync_daily_bars_from_spot),
                      "cron", day_of_week="mon-fri", hour=15, minute=40,
                      id="sync_daily_bars", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
    # 15:45 逐只增量补漏（只修已有历史的缺口，正常为 0 只）
    scheduler.add_job(lambda: _with_db(datafeed.sync_all_quotes, years=5,
                                       only_existing=True),
                      "cron", day_of_week="mon-fri", hour=15, minute=45,
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
    # 16:20 资金流收盘官方数据
    scheduler.add_job(lambda: _with_db(money_flow.sync_money_flow),
                      "cron", day_of_week="mon-fri", hour=16, minute=20,
                      id="sync_money_flow", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 15:50 基准指数日线（1 只 1 请求，很快）
    scheduler.add_job(lambda: _with_db(datafeed.sync_index_quotes),
                      "cron", day_of_week="mon-fri", hour=15, minute=50,
                      id="sync_index", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
    # 16:22 全市场截面特征（等收盘行情 + 资金流就绪）
    scheduler.add_job(lambda: _with_db(features.build_features),
                      "cron", day_of_week="mon-fri", hour=16, minute=22,
                      id="build_features", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 16:25 选股快照落库（在 16:10 筛选、16:15 区间之后）
    scheduler.add_job(lambda: _with_db(picks_track.save_snapshot),
                      "cron", day_of_week="mon-fri", hour=16, minute=25,
                      id="picks_snapshot", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 16:30 回填前向收益（T+5/10/20/60，已存在的不覆盖）
    scheduler.add_job(lambda: _with_db(picks_track.update_forward_returns),
                      "cron", day_of_week="mon-fri", hour=16, minute=30,
                      id="update_fwd", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 16:32 全市场前向标签回填（供可行性检验与模型训练）
    scheduler.add_job(lambda: _with_db(labels.build_labels),
                      "cron", day_of_week="mon-fri", hour=16, minute=32,
                      id="build_labels", replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    # 16:45 低估观察汇总（仅跌破下沿、未满足右侧确认的标的合并一条）
    scheduler.add_job(run_digest_job, "cron", day_of_week="mon-fri",
                      hour=16, minute=45, id="zone_digest",
                      replace_existing=True, max_instances=1,
                      misfire_grace_time=1800)
    # 18:00 妖股快照落库（等 15:40 起的收盘行情同步跑完）
    scheduler.add_job(lambda: _with_db(yaogu.save_today_snapshot),
                      "cron", day_of_week="mon-fri", hour=18, minute=0,
                      id="yaogu_snapshot", replace_existing=True,
                      max_instances=1, misfire_grace_time=3600)
    # 周六 09:00 全市场基本面同步
    scheduler.add_job(lambda: _with_db(datafeed.sync_all_fundamentals, years=5),
                      "cron", day_of_week="sat", hour=9, minute=0,
                      id="sync_fundamentals", replace_existing=True,
                      max_instances=1, misfire_grace_time=86400)
    # 周日 09:00 行业分类同步
    scheduler.add_job(lambda: _with_db(datafeed.sync_industry),
                      "cron", day_of_week="sun", hour=9, minute=0,
                      id="sync_industry", replace_existing=True,
                      max_instances=1, misfire_grace_time=86400)
    # 周日 08:50 交易日历同步（真实开市日，供特征/标签/调度判断）
    scheduler.add_job(
        lambda: _with_db(trading_calendar.sync_trade_calendar),
        "cron", day_of_week="sun", hour=8, minute=50,
        id="sync_trade_calendar", replace_existing=True,
        max_instances=1, misfire_grace_time=86400)
    # 盘中每 30 分钟监控（9:30-15:00）
    scheduler.add_job(run_monitor_job, "cron", day_of_week="mon-fri",
                      hour="9-14", minute="*/30", id="monitor",
                      replace_existing=True)
    # 消息面：早盘前 / 午间 / 收盘后各抓一次并更新 AI 简报
    for hh, mm in ((8, 30), (12, 30), (17, 30)):
        scheduler.add_job(_news_chain, "cron", day_of_week="mon-fri",
                          hour=hh, minute=mm, id=f"news_{hh:02d}{mm:02d}",
                          replace_existing=True, max_instances=1,
                          misfire_grace_time=3600)


def start() -> None:
    setup_jobs()
    if not scheduler.running:
        scheduler.start()
        log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))

    # 启动补跑：如果容器启动时已经过了今天的 cron 触发点，且下一次触发
    # 已跳到明天，说明今天被 misfire_grace_time 跳过了，这里手动补跑一次。
    from datetime import timedelta
    now = datetime.now(scheduler.timezone)
    log.info("startup catch-up check: now=%s dow=%s", now, now.weekday())

    _WEEKDAY_ONLY = {0, 1, 2, 3, 4}
    _SAT = {5}
    _SUN = {6}

    catch_up = [
        ("sync_daily_bars",   time(15, 40), _WEEKDAY_ONLY,
         lambda: _with_db(datafeed.sync_daily_bars_from_spot)),
        ("sync_quotes",       time(15, 45), _WEEKDAY_ONLY,
         lambda: _with_db(datafeed.sync_all_quotes, years=5,
                          only_existing=True)),
        ("screen",            time(16, 10), _WEEKDAY_ONLY,
         lambda: _with_db(screener.run_screen)),
        ("calc_zones",        time(16, 15), _WEEKDAY_ONLY,
         lambda: _with_db(safety_zone.calc_zones)),
        ("sync_money_flow",   time(16, 20), _WEEKDAY_ONLY,
         lambda: _with_db(money_flow.sync_money_flow)),
        ("sync_index",        time(15, 50), _WEEKDAY_ONLY,
         lambda: _with_db(datafeed.sync_index_quotes)),
        ("build_features",    time(16, 22), _WEEKDAY_ONLY,
         lambda: _with_db(features.build_features)),
        ("picks_snapshot",    time(16, 25), _WEEKDAY_ONLY,
         lambda: _with_db(picks_track.save_snapshot)),
        ("update_fwd",        time(16, 30), _WEEKDAY_ONLY,
         lambda: _with_db(picks_track.update_forward_returns)),
        ("build_labels",      time(16, 32), _WEEKDAY_ONLY,
         lambda: _with_db(labels.build_labels)),
        ("zone_digest",       time(16, 45), _WEEKDAY_ONLY, run_digest_job),
        ("intraday_run",      time(14, 0),  _WEEKDAY_ONLY, _intraday_chain),
        ("sync_fundamentals", time(9, 0),  _SAT,
         lambda: _with_db(datafeed.sync_all_fundamentals, years=5)),
        ("sync_industry",     time(9, 0),  _SUN,
         lambda: _with_db(datafeed.sync_industry)),
        ("sync_trade_calendar", time(8, 50), _SUN,
         lambda: _with_db(trading_calendar.sync_trade_calendar)),
    ]

    for job_id, t, allowed_days, fn in catch_up:
        if now.weekday() not in allowed_days:
            continue
        target = datetime.combine(now.date(), t, tzinfo=now.tzinfo)
        if now <= target:
            continue   # 还没到触发点，等 cron 自己跑
        if (now - target).total_seconds() > 24 * 3600:
            continue   # 超过 24 小时就不补了（不是今天的事）
        j = scheduler.get_job(job_id)
        if j and j.next_run_time and j.next_run_time.date() > now.date():
            log.warning("startup catch-up: %s missed today, triggering now",
                        job_id)
            threading.Thread(target=fn, daemon=True).start()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
