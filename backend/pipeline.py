"""一次性流水线：等待行情同步 → 基本面同步 → 筛选 → 安全区间。

用法（容器内）：python pipeline.py
日志：/app/pipeline.log
"""
import logging
import os
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("/app/pipeline.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("pipeline")


def quotes_running() -> bool:
    """通过 /proc 检测行情同步进程是否仍在运行（slim 镜像无 ps）。"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="ignore")
            if "sync_all_quotes" in cmd:
                return True
        except OSError:
            continue
    return False


def main() -> None:
    waited = 0
    while quotes_running():
        waited += 1
        if waited % 5 == 0:
            log.info("waiting for quotes sync... (%d min)", waited)
        time.sleep(60)
    log.info("quotes sync finished (waited %d min), starting fundamentals", waited)

    from app.db import SessionLocal
    from app.services import datafeed, safety_zone, screener

    db = SessionLocal()
    try:
        r1 = datafeed.sync_all_fundamentals(db, years=5)
        log.info("fundamentals done: %s", r1)
        r2 = screener.run_screen(db)
        log.info("screen done: %s", r2)
        r3 = safety_zone.calc_zones(db)
        log.info("zones done: %s", r3)
        log.info("PIPELINE ALL DONE")
    except Exception:
        log.exception("pipeline failed")
    finally:
        db.close()


if __name__ == "__main__":
    main()
