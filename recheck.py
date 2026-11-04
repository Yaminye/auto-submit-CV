"""Re-run the filters after changing config.yaml / filters.py.

  .venv/bin/python recheck.py            # re-check kept jobs that are still waiting (new/queued/review/low)
  .venv/bin/python recheck.py --dropped  # re-check jobs dropped by a config rule: the title filter or
                                         # easy_apply_only (e.g. after turning one of them off)

Jobs you moved forward (applied / interview / offer / ...) are never touched.
New keeps get scored by Claude at the end (if ANTHROPIC_API_KEY is set).
"""
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jobfinder import db, filters, linkedin

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
for _n in ("httpx", "httpx2"):
    logging.getLogger(_n).setLevel(logging.WARNING)


def update(con, job: dict, keep: bool, reason: str) -> None:
    fields = {"keep": keep, "reason": reason, "years": job.get("years")}
    if job.get("easy_apply") is not None:
        fields["easy_apply"] = job["easy_apply"]
    if keep and job.get("description"):
        fields["description"] = job["description"]
    db.update_job(con, job["job_id"], fields)


def recheck_kept(con, cfg) -> None:
    rows = db.find_jobs(con, {"keep": True, "status": {"$in": db.WAITING}})
    dropped = 0
    for job in rows:
        if not filters.title_ok(job["title"], filters.title_excludes(cfg), cfg.get("title_include")):
            keep, reason = False, "title"
        else:
            if not job.get("description") or "easy_apply" not in job:
                job.update(linkedin.details(job["job_id"]))
                linkedin._pause(1.0, 2.0)
            keep, reason = filters.evaluate(job, cfg)
        update(con, job, keep, reason)
        if not keep:
            dropped += 1
            print(f"  - {job['title']} @ {job['company']}: {reason}")
    print(f"rechecked {len(rows)} kept jobs, dropped {dropped}")


def recheck_dropped(con, cfg) -> None:
    rows = db.find_jobs(con, {"keep": False, "reason": {"$in": ["title", "not Easy Apply"]}})
    candidates = [j for j in rows
                  if filters.title_ok(j["title"], filters.title_excludes(cfg), cfg.get("title_include"))]
    print(f"{len(rows)} jobs dropped by a config rule, {len(candidates)} pass the title filter now")
    kept = 0
    for job in candidates:
        fetch = not job.get("description")          # title-dropped jobs were never fetched
        if fetch:
            job.update(linkedin.details(job["job_id"]))
        keep, reason = filters.evaluate(job, cfg)
        update(con, job, keep, reason)
        if keep:
            kept += 1
            if job.get("score") is None:              # never scored -> score at the end
                db.update_job(con, job["job_id"], {"status": "new"})
            print(f"  + [{job.get('score') if job.get('score') is not None else 'new'}] "
                  f"{job['title']} @ {job['company']} ({job.get('status')})")
        if fetch:
            linkedin._pause(1.0, 2.5)
    print(f"now kept: {kept} of {len(candidates)}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    con = db.connect()
    if "--dropped" in sys.argv:
        recheck_dropped(con, cfg)
    else:
        recheck_kept(con, cfg)
    if os.getenv("ANTHROPIC_API_KEY"):
        from score import score_pending
        score_pending(con, cfg)


if __name__ == "__main__":
    main()
