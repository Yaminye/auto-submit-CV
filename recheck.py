"""Re-run the filters after changing config.yaml / filters.py.

  .venv/bin/python recheck.py            # re-check kept jobs that are still waiting (new/queued/review/low)
  .venv/bin/python recheck.py --dropped  # re-check jobs dropped by the TITLE filter (e.g. after
                                         # turning exclude_senior_titles off) - fetches their description

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
WAITING = ("new", "queued", "review", "low")


def update(con, job: dict, keep: bool, reason: str) -> None:
    con.execute("UPDATE jobs SET keep=?, reason=?, years=?, description=COALESCE(?, description) WHERE job_id=?",
                (int(keep), reason, job.get("years"), job.get("description") if keep else None, job["job_id"]))
    con.commit()


def recheck_kept(con, cfg) -> None:
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM jobs WHERE keep=1 AND status IN ({','.join('?' * len(WAITING))})", WAITING)]
    dropped = 0
    for job in rows:
        if not filters.title_ok(job["title"], filters.title_excludes(cfg), cfg.get("title_include")):
            keep, reason = False, "title"
        else:
            if not job.get("description"):
                job.update(linkedin.details(job["job_id"]))
                linkedin._pause(1.0, 2.0)
            keep, reason = filters.evaluate(job, cfg)
        update(con, job, keep, reason)
        if not keep:
            dropped += 1
            print(f"  - {job['title']} @ {job['company']}: {reason}")
    print(f"rechecked {len(rows)} kept jobs, dropped {dropped}")


def recheck_dropped(con, cfg) -> None:
    rows = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE keep=0 AND reason='title'")]
    candidates = [j for j in rows
                  if filters.title_ok(j["title"], filters.title_excludes(cfg), cfg.get("title_include"))]
    print(f"{len(rows)} title-dropped jobs, {len(candidates)} pass the title filter now")
    kept = 0
    for job in candidates:
        job.update(linkedin.details(job["job_id"]))
        keep, reason = filters.evaluate(job, cfg)
        update(con, job, keep, reason)
        if keep:
            kept += 1
            con.execute("UPDATE jobs SET status='new', score=NULL WHERE job_id=?", (job["job_id"],))
            con.commit()
            print(f"  + {job['title']} @ {job['company']} (yrs={job.get('years')})")
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
