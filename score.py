"""Score kept-but-unscored jobs against the CV with Claude.
Runs automatically at the end of scan.py; run it by hand to backfill or re-score."""
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from jobfinder import claude, db, linkedin

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
for _n in ("httpx", "httpx2"):
    logging.getLogger(_n).setLevel(logging.WARNING)   # hide per-request API logs
log = logging.getLogger("score")


def bucket(score: int, cfg: dict) -> str:
    if score >= cfg["apply_threshold"]:
        return "queued"
    if score >= cfg["review_threshold"]:
        return "review"
    return "low"


def score_pending(con, cfg: dict) -> None:
    rows = db.unscored(con)
    log.info("scoring %d jobs", len(rows))
    for job in rows:
        if not db.claim_for_scoring(con, job["job_id"]):
            continue   # already scored, or another run is scoring it right now
        try:
            fetched = None
            if not job.get("description"):
                fetched = linkedin.details(job["job_id"])["description"]
                job["description"] = fetched
                linkedin._pause(1.0, 2.0)
            res = claude.score_job(job, cfg.get("score_model"))
        except BaseException:
            db.release_claim(con, job["job_id"])
            raise
        if res is None:
            db.release_claim(con, job["job_id"])   # stays unscored, retried next run
            continue
        score, reason = res
        status = bucket(score, cfg)
        db.set_score(con, job["job_id"], score, reason, status, fetched)
        log.info("  %3d %-7s %s @ %s - %s", score, status, job["title"], job["company"], reason)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    con = db.connect()
    if "--rescore" in sys.argv:
        # the ONLY way a job gets scored again - on purpose, after confirming
        n = con.jobs.count_documents({"keep": True, "status": {"$in": db.WAITING},
                                      "apply_note": {"$ne": "set from dashboard"}})
        if "--yes" not in sys.argv and input(
                f"Score {n} jobs again with Claude (about ${n * 0.007:.2f})? Type yes: ").strip().lower() != "yes":
            sys.exit("cancelled - nothing re-scored")
        # only jobs still waiting; jobs you moved by hand on the board keep their place
        print(f"reset {db.reset_scores_waiting(con)} scores")
    score_pending(con, yaml.safe_load((ROOT / "config.yaml").read_text()))
