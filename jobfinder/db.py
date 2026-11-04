"""MongoDB storage. Database `autosubmit`:
  jobs     one document per LinkedIn job, _id = job_id
  answers  form answers you gave before, _id = normalized question label
  meta     small state docs (last scan, scheduler runs)

MONGO_URL: mongodb://localhost:27017 on the Mac (default), mongodb://mongo:27017 inside Docker.
Times are stored as UTC datetimes; `posted` stays a "YYYY-MM-DD" string from LinkedIn.

Job statuses: new -> queued (score >= apply) / review / low -> applied / skipped / external / failed
              board stages set by you: assignment / interview1 / interview2 / offer / rejected
"""
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import ServerSelectionTimeoutError

WAITING = ["new", "queued", "review", "low"]      # statuses a re-score / re-check may change
CLAIM_TTL = timedelta(minutes=10)                  # a scoring claim older than this counts as abandoned


def now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def connect() -> Database:
    url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    d = MongoClient(url, tz_aware=True, serverSelectionTimeoutMS=5000)[os.getenv("MONGO_DB", "autosubmit")]
    try:
        d.jobs.create_index([("keep", ASCENDING), ("status", ASCENDING)])
        d.jobs.create_index([("found_at", DESCENDING)])
    except ServerSelectionTimeoutError:
        raise SystemExit(f"Can't reach MongoDB at {url} - start it with: docker compose up -d")
    return d


# ---------------------------------------------------------------- jobs: read

def get_job(d: Database, job_id: str) -> dict | None:
    return d.jobs.find_one({"_id": job_id})


def find_jobs(d: Database, query: dict, projection: dict | None = None, sort=None, limit: int = 0) -> list[dict]:
    cur = d.jobs.find(query, projection)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def known_ids(d: Database) -> set[str]:
    return set(d.jobs.distinct("_id"))


def title_company_key(title: str, company: str) -> tuple[str, str]:
    """Same job reposted under a new LinkedIn id -> same key (case and spacing ignored)."""
    return " ".join((title or "").lower().split()), " ".join((company or "").lower().split())


def known_title_company(d: Database) -> set[tuple[str, str]]:
    return {title_company_key(j["title"], j["company"])
            for j in d.jobs.find({"keep": True}, {"title": 1, "company": 1})}


def unsent(d: Database) -> list[dict]:
    return find_jobs(d, {"keep": True, "sent_at": None},
                     sort=[("score", DESCENDING), ("category", ASCENDING)])


def unscored(d: Database) -> list[dict]:
    return find_jobs(d, {"keep": True, "score": None})


def queued(d: Database, limit: int | None = None) -> list[dict]:
    return find_jobs(d, {"keep": True, "status": "queued"},
                     sort=[("score", DESCENDING), ("found_at", DESCENDING)], limit=limit or 0)


def applied_today(d: Database) -> int:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return d.jobs.count_documents({"status": "applied", "applied_at": {"$gte": start}})


# ---------------------------------------------------------------- jobs: write

def insert(d: Database, job: dict, keep: bool, reason: str) -> None:
    doc = {k: job.get(k) for k in ("category", "title", "company", "location", "posted", "url", "years", "applicants", "easy_apply")}
    doc.update(_id=job["job_id"], job_id=job["job_id"], keep=keep, reason=reason, found_at=now(),
               description=job.get("description") if keep else None,
               status="new", score=None, sent_at=None)
    d.jobs.update_one({"_id": job["job_id"]}, {"$setOnInsert": doc}, upsert=True)


def update_job(d: Database, job_id: str, fields: dict) -> None:
    d.jobs.update_one({"_id": job_id}, {"$set": fields})


def mark_sent(d: Database, ids: list[str]) -> None:
    d.jobs.update_many({"_id": {"$in": ids}}, {"$set": {"sent_at": now()}})


def claim_for_scoring(d: Database, job_id: str) -> bool:
    """Atomically take an unscored job for scoring. False if it's already scored, or another run is
    scoring it right now - so a job is never scored twice, even by two runs at the same time."""
    r = d.jobs.update_one(
        {"_id": job_id, "keep": True, "score": None,
         "$or": [{"scoring_at": None}, {"scoring_at": {"$lt": now() - CLAIM_TTL}}]},
        {"$set": {"scoring_at": now()}})
    return r.modified_count == 1


def release_claim(d: Database, job_id: str) -> None:
    d.jobs.update_one({"_id": job_id}, {"$unset": {"scoring_at": ""}})


def set_score(d: Database, job_id: str, score: int, reason: str, status: str, description: str | None = None) -> None:
    fields = {"score": score, "match_reason": reason, "status": status}
    if description:
        fields["description"] = description
    d.jobs.update_one({"_id": job_id}, {"$set": fields, "$unset": {"scoring_at": ""}})


def reset_scores_waiting(d: Database) -> int:
    """For score.py --rescore: jobs still waiting, except ones you moved by hand on the board."""
    r = d.jobs.update_many({"keep": True, "status": {"$in": WAITING}, "apply_note": {"$ne": "set from dashboard"}},
                           {"$set": {"score": None}})
    return r.modified_count


def set_status(d: Database, job_id: str, status: str, note: str = "") -> None:
    fields = {"status": status, "apply_note": note}
    if status == "applied":
        job = get_job(d, job_id) or {}
        if not job.get("applied_at"):
            fields["applied_at"] = now()
    update_job(d, job_id, fields)


def save_answers(d: Database, job_id: str, log: list, dry_run: bool = False) -> None:
    update_job(d, job_id, {"apply_answers": {"dry_run": dry_run, "at": now(), "answers": log}})


# ---------------------------------------------------------------- saved form answers

def load_answers(d: Database) -> dict[str, str]:
    return {a["_id"]: a["value"] for a in d.answers.find()}


def save_answer(d: Database, key: str, label: str, value: str) -> None:
    d.answers.update_one({"_id": key}, {"$set": {"label": label, "value": value, "updated_at": now()}}, upsert=True)


def all_answers(d: Database) -> list[dict]:
    return list(d.answers.find().sort("label", ASCENDING))


def delete_answer(d: Database, key: str) -> bool:
    return d.answers.delete_one({"_id": key}).deleted_count == 1


# ---------------------------------------------------------------- meta

def set_meta(d: Database, key: str, value: dict) -> None:
    d.meta.update_one({"_id": key}, {"$set": value}, upsert=True)


def get_meta(d: Database, key: str) -> dict:
    return d.meta.find_one({"_id": key}) or {}
