"""等待补扫完成 → 跑筛选 + 安全区间 → 输出最终数据质量。"""
import logging
import os
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("/app/after_resume.log",
                                                  encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("after")


def resume_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
            if "resume_fin.py" in cmd:
                return True
        except OSError:
            continue
    return False


def main() -> None:
    while resume_running():
        time.sleep(60)
    log.info("resume finished, running screen + zones")
    from app.db import SessionLocal
    from app.models import (AlertLog, DailyQuote, FinIndicator, SafetyZone,
                            Stock, StockPool, ValuationDaily)
    from app.services import safety_zone, screener

    db = SessionLocal()
    try:
        r1 = screener.run_screen(db)
        log.info("screen done: %s", r1)
        r2 = safety_zone.calc_zones(db)
        log.info("zones done: %s", r2)

        total = db.query(Stock.code).count()
        stats = {
            "stocks": total,
            "quotes_codes": db.query(DailyQuote.code).distinct().count(),
            "fin_codes": db.query(FinIndicator.code).distinct().count(),
            "val_codes": db.query(ValuationDaily.code).distinct().count(),
            "pool_active": db.query(StockPool).filter(StockPool.active.is_(True)).count(),
            "zones": db.query(SafetyZone).count(),
            "alerts": db.query(AlertLog).count(),
        }
        log.info("QUALITY STATS: %s", stats)
    finally:
        db.close()
    log.info("FINAL DONE")


if __name__ == "__main__":
    main()
