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


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def known_ids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT job_id FROM jobs")}


def known_title_company(con) -> set[tuple[str, str]]:
    return {(r[0].lower(), r[1].lower()) for r in con.execute("SELECT title, company FROM jobs WHERE keep=1")}


def insert(con, job: dict, keep: bool, reason: str) -> None:
    con.execute(
        """INSERT OR IGNORE INTO jobs
           (job_id, category, title, company, location, posted, url, years, applicants, keep, reason, found_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job["job_id"], job["category"], job["title"], job["company"], job["location"],
         job["posted"], job["url"], job.get("years"), job.get("applicants", ""),
         int(keep), reason, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()


def unsent(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM jobs WHERE keep=1 AND sent_at IS NULL ORDER BY category, posted DESC"
    ).fetchall()


def mark_sent(con, ids: list[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    con.executemany("UPDATE jobs SET sent_at=? WHERE job_id=?", [(now, i) for i in ids])
    con.commit()
