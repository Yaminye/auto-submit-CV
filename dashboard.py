"""The job board -> http://127.0.0.1:8765
Runs in Docker (docker compose up -d). Without Docker for the app: .venv/bin/python dashboard.py
(MongoDB must be running either way)."""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jobfinder import db

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="auto-submit board")

LIST_PROJECTION = {"description": 0, "apply_answers": 0}   # the board doesn't need the big fields
# statuses you can set from the board (drag & drop / job panel)
#   queued   -> apply.py will try it        applied -> you applied (by apply.py or by hand)
#   assignment / interview1 / interview2 / offer / rejected -> your own pipeline stages
#   new / low / external / failed are accepted too, so the board's Undo can restore them
STATUSES = {"review", "queued", "applied", "assignment", "interview1", "interview2",
            "offer", "rejected", "skipped", "new", "low", "external", "failed"}


@app.get("/")
def index():
    return FileResponse(ROOT / "dashboard" / "index.html")


@app.get("/api/jobs")
def jobs():
    return db.find_jobs(db.connect(), {}, LIST_PROJECTION, sort=[("found_at", -1)])


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    j = db.get_job(db.connect(), job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


class StatusIn(BaseModel):
    status: str


@app.post("/api/jobs/{job_id}/status")
def set_status(job_id: str, body: StatusIn):
    if body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(STATUSES)}")
    d = db.connect()
    j = db.get_job(d, job_id)
    if not j or not j.get("keep"):
        raise HTTPException(404, "job not found (or dropped by filters)")
    db.set_status(d, job_id, body.status, note="set from dashboard")
    return {"ok": True}


# ---- saved form answers (used by apply.py)

@app.get("/api/answers")
def answers():
    return db.all_answers(db.connect())


class AnswerIn(BaseModel):
    value: str


@app.put("/api/answers/{key:path}")
def put_answer(key: str, body: AnswerIn):
    d = db.connect()
    cur = d.answers.find_one({"_id": key})
    if not cur:
        raise HTTPException(404, "answer not found")
    db.save_answer(d, key, cur.get("label", key), body.value.strip())
    return {"ok": True}


@app.delete("/api/answers/{key:path}")
def delete_answer(key: str):
    if not db.delete_answer(db.connect(), key):
        raise HTTPException(404, "answer not found")
    return {"ok": True}


@app.get("/api/meta")
def meta():
    return {"last_scan": db.get_meta(db.connect(), "last_scan") or None}


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")   # 0.0.0.0 inside Docker (port is published on localhost only)
    print("Board: http://127.0.0.1:8765", flush=True)
    uvicorn.run(app, host=host, port=8765, log_level="warning")
