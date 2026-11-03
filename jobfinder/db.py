import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    category    TEXT,
    title       TEXT,
    company     TEXT,
    location    TEXT,
    posted      TEXT,
    url         TEXT,
    years       INTEGER,
    applicants  TEXT,
    keep        INTEGER,
    reason      TEXT,
    found_at    TEXT,
    sent_at     TEXT,
    status      TEXT DEFAULT 'new'   -- new / applied / skipped (for the submitter later)
);
"""


# columns added after v1 - applied as migrations on existing DBs
EXTRA_COLUMNS = {
    "description": "TEXT",
    "score": "INTEGER",
    "match_reason": "TEXT",
    "applied_at": "TEXT",
    "apply_note": "TEXT",
    "apply_answers": "TEXT",   # JSON: [{label, value, source}] + dry_run flag
}
# status: new -> queued (score>=apply) / review / low -> applied / skipped / external / failed
#         board stages set by you: assignment / interview1 / interview2 / offer / rejected


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for col, typ in EXTRA_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    con.commit()
    return con


def known_ids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT job_id FROM jobs")}


def known_title_company(con) -> set[tuple[str, str]]:
    return {(r[0].lower(), r[1].lower()) for r in con.execute("SELECT title, company FROM jobs WHERE keep=1")}


def insert(con, job: dict, keep: bool, reason: str) -> None:
    con.execute(
        """INSERT OR IGNORE INTO jobs
           (job_id, category, title, company, location, posted, url, years, applicants, keep, reason,
            found_at, description)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job["job_id"], job["category"], job["title"], job["company"], job["location"],
         job["posted"], job["url"], job.get("years"), job.get("applicants", ""),
         int(keep), reason, datetime.now().isoformat(timespec="seconds"),
         job.get("description") if keep else None),
    )
    con.commit()


def unsent(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM jobs WHERE keep=1 AND sent_at IS NULL ORDER BY score DESC NULLS LAST, category"
    ).fetchall()


def mark_sent(con, ids: list[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany("UPDATE jobs SET sent_at=? WHERE job_id=?", [(now, i) for i in ids])
    con.commit()


def unscored(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM jobs WHERE keep=1 AND score IS NULL").fetchall()


def set_score(con, job_id: str, score: int, reason: str, status: str, description: str | None = None) -> None:
    con.execute(
        "UPDATE jobs SET score=?, match_reason=?, status=?, description=COALESCE(?, description) WHERE job_id=?",
        (score, reason, status, description, job_id),
    )
    con.commit()


def queued(con, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM jobs WHERE keep=1 AND status='queued' ORDER BY score DESC, found_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql).fetchall()


def applied_today(con) -> int:
    today = datetime.now().date().isoformat()
    return con.execute("SELECT COUNT(*) FROM jobs WHERE status='applied' AND applied_at >= ?", (today,)).fetchone()[0]


def set_status(con, job_id: str, status: str, note: str = "") -> None:
    applied_at = datetime.now().isoformat(timespec="seconds") if status == "applied" else None
    con.execute("UPDATE jobs SET status=?, apply_note=?, applied_at=COALESCE(?, applied_at) WHERE job_id=?",
                (status, note, applied_at, job_id))
    con.commit()


def save_answers(con, job_id: str, log: list, dry_run: bool = False) -> None:
    import json
    payload = json.dumps({"dry_run": dry_run, "at": datetime.now().isoformat(timespec="seconds"),
                          "answers": log}, ensure_ascii=False)
    con.execute("UPDATE jobs SET apply_answers=? WHERE job_id=?", (payload, job_id))
    con.commit()
