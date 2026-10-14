"""补扫缺失的基本面数据（财务指标 + 估值），幂等。"""
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("resume")

sys.path.insert(0, "/app")
from app.db import SessionLocal  # noqa: E402
from app.models import FinIndicator, Stock, ValuationDaily  # noqa: E402
from app.services.datafeed import _fetch_fin, _fetch_val, _upsert_fin, _upsert_val  # noqa: E402


def main() -> None:
    db = SessionLocal()
    try:
        all_codes = {c for (c,) in db.query(Stock.code).all()}
        fin_have = {c for (c,) in db.query(FinIndicator.code).distinct().all()}
        val_have = {c for (c,) in db.query(ValuationDaily.code).distinct().all()}
        fin_todo = sorted(all_codes - fin_have)
        val_todo = sorted(all_codes - val_have)
        log.info("missing fin=%d val=%d", len(fin_todo), len(val_todo))

        start_year = str(date.today().year - 5)
        ok = fail = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_fetch_fin, c, start_year): c for c in fin_todo}
            for fut in as_completed(futs):
                code, df = fut.result()
                try:
                    if _upsert_fin(db, code, df):
                        ok += 1
                except Exception as e:
                    db.rollback()
                    fail += 1
                    log.warning("fin upsert failed %s: %s", code, e)
        log.info("fin resume done: ok=%d fail=%d", ok, fail)

        ok = fail = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_fetch_val, c): c for c in val_todo}
            for fut in as_completed(futs):
                code, df = fut.result()
                try:
                    if _upsert_val(db, code, df, 5):
                        ok += 1
                except Exception as e:
                    db.rollback()
                    fail += 1
                    log.warning("val upsert failed %s: %s", code, e)
        log.info("val resume done: ok=%d fail=%d", ok, fail)
        log.info("RESUME DONE")
    finally:
        db.close()


if __name__ == "__main__":
    main()
