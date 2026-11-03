"""Local dashboard for the job DB.   .venv/bin/python dashboard.py  ->  http://127.0.0.1:8765"""
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jobfinder import db

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="auto-submit dashboard")

LIST_COLS = ("job_id, category, title, company, location, posted, url, years, keep, reason, found_at, "
             "sent_at, status, score, match_reason, applied_at, apply_note, "
             "(apply_answers IS NOT NULL) AS has_answers")
# statuses you can set by hand from the board (drag & drop / job panel)
#   queued   -> apply.py will try it        applied -> you applied (by apply.py or by hand)
#   assignment / interview1 / interview2 / offer / rejected -> your own pipeline stages
#   new / low / external / failed are accepted too, so the board's Undo can restore them
MANUAL_STATUSES = {"review", "queued", "applied", "assignment", "interview1", "interview2",
                   "offer", "rejected", "skipped", "new", "low", "external", "failed"}


@app.get("/")
def index():
    return FileResponse(ROOT / "dashboard" / "index.html")


@app.get("/api/jobs")
def jobs():
    con = db.connect()
    rows = con.execute(f"SELECT {LIST_COLS} FROM jobs ORDER BY found_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    con = db.connect()
    r = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "job not found")
    d = dict(r)
    d["apply_answers"] = json.loads(d["apply_answers"]) if d.get("apply_answers") else None
    return d


class StatusIn(BaseModel):
    status: str


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: str, body: StatusIn):
    if body.status not in MANUAL_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(MANUAL_STATUSES)}")
    con = db.connect()
    if not con.execute("SELECT 1 FROM jobs WHERE job_id=? AND keep=1", (job_id,)).fetchone():
        raise HTTPException(404, "job not found (or dropped by filters)")
    db.set_status(con, job_id, body.status, note="set from dashboard")
    return {"ok": True}


if __name__ == "__main__":
    print("Dashboard: http://127.0.0.1:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
