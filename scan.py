"""Runs daily (scheduler.py, 17:00): search LinkedIn, filter, save to MongoDB, score with Claude."""
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jobfinder import db, filters, linkedin
from jobfinder.cleanup import auto_clean

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
for _n in ("httpx", "httpx2"):
    logging.getLogger(_n).setLevel(logging.WARNING)   # hide per-request API logs
log = logging.getLogger("scan")


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    auto_clean(cfg)                    # logs/ from earlier days (keep_logs_days)
    con = db.connect()
    started = db.now()
    db.set_meta(con, "last_scan", {"started_at": started, "finished_at": None, "running": True})
    seen = db.known_ids(con)
    seen_tc = db.known_title_company(con)
    kept = dropped = 0

    for category, keywords in cfg["queries"].items():
        for kw in keywords:
            results = linkedin.search(kw, cfg["location"], cfg["geo_id"],
                                      cfg["posted_within_hours"], cfg["pages_per_query"],
                                      easy_apply_only=bool(cfg.get("easy_apply_only")))
            log.info("%-25s %3d results", kw, len(results))
            for job in results:
                if job["job_id"] in seen:
                    continue
                seen.add(job["job_id"])
                job["category"] = category

                # cheap title filter first, skip detail fetch if obviously irrelevant
                if not filters.title_ok(job["title"], filters.title_excludes(cfg), cfg.get("title_include")):
                    db.insert(con, job, False, "title")
                    dropped += 1
                    continue
                if not filters.location_ok(job.get("location", ""), cfg):
                    db.insert(con, job, False, "location")
                    dropped += 1
                    continue
                if db.title_company_key(job["title"], job["company"]) in seen_tc:
                    db.insert(con, job, False, "duplicate")
                    continue

                job.update(linkedin.details(job["job_id"]))
                keep, reason = filters.evaluate(job, cfg)
                db.insert(con, job, keep, reason)
                if keep:
                    kept += 1
                    seen_tc.add(db.title_company_key(job["title"], job["company"]))
                    log.info("  + %s @ %s (yrs=%s)", job["title"], job["company"], job.get("years"))
                else:
                    dropped += 1
                linkedin._pause(1.0, 2.5)

    log.info("done: kept=%d dropped=%d", kept, dropped)

    if os.getenv("ANTHROPIC_API_KEY"):
        from score import score_pending
        score_pending(con, cfg)
    else:
        log.info("ANTHROPIC_API_KEY not set - skipping Claude scoring")
    db.set_meta(con, "last_scan", {"started_at": started, "finished_at": db.now(), "running": False,
                                   "kept": kept, "dropped": dropped})


if __name__ == "__main__":
    main()
