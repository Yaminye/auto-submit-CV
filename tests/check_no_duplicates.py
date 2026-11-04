"""Proves: a job is never saved twice and never scored twice.
Runs the real scan.py / score.py code on fake LinkedIn data and a fake Claude scorer, against a
throwaway database (your real data is never touched, no LinkedIn requests, no API cost).

    .venv/bin/python tests/check_no_duplicates.py      (MongoDB must be running)
"""
import os
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["MONGO_DB"] = TEST_DB = f"autosubmit_test_{uuid.uuid4().hex[:8]}"
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-scoring-is-faked")

import yaml  # noqa: E402

import scan  # noqa: E402
import score  # noqa: E402
from jobfinder import claude, db, linkedin  # noqa: E402

d = db.connect()
assert d.name == TEST_DB, "refusing to run on a real database"
TEL_AVIV = "Tel Aviv-Yafo, Tel Aviv District, Israel"


def card(job_id, title, company):
    return {"job_id": job_id, "title": title, "company": company, "location": TEL_AVIV,
            "posted": "2026-11-04", "posted_text": "1 hour ago", "url": f"https://example.com/{job_id}"}


# Same job under two keywords (A), a repost of A with a new id and different spacing/case (B),
# another good job (C) and one that asks for 5+ years (D).
RESULTS = {
    "Backend Developer": [card("A", "Backend Developer", "MockCo"), card("C", "Python Developer", "OtherCo")],
    "Backend Engineer": [card("A", "Backend Developer", "MockCo"), card("B", "backend  developer", "MockCo ")],
    "Python Developer": [card("D", "Backend Developer", "SeniorCo")],
}
DESCRIPTIONS = {"A": "2+ years of experience with Python.", "B": "2+ years of experience with Python.",
                "C": "Junior role, Python and Docker.", "D": "5+ years of experience required."}
detail_calls = Counter()
score_calls = Counter()
lock = threading.Lock()


def fake_search(kw, *args, **kwargs):
    return [dict(c) for c in RESULTS.get(kw, [])]


def fake_details(job_id):
    detail_calls[job_id] += 1
    return {"description": DESCRIPTIONS.get(job_id, "Junior role, Python."), "seniority": "", "applicants": "",
            "easy_apply": True}


def fake_score(job, model=None):
    time.sleep(0.05)               # long enough for parallel runs to overlap
    with lock:
        score_calls[job["job_id"]] += 1
    return 80, "fake score"


linkedin.search, linkedin.details, linkedin._pause = fake_search, fake_details, lambda *a: None
claude.score_job = fake_score
failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


try:
    print("1) scan twice")
    scan.main()
    scan.main()
    docs = {j["_id"]: j for j in d.jobs.find()}
    check("each job saved once (4 documents)", len(docs) == 4, f"{sorted(docs)}")
    check("job seen under 2 keywords: page fetched once", detail_calls["A"] == 1, f"{dict(detail_calls)}")
    check("repost saved as 'duplicate', not on the board", docs["B"]["keep"] is False and docs["B"]["reason"] == "duplicate")
    check("5+ years job dropped", docs["D"]["keep"] is False)
    check("second scan scored nothing again", dict(score_calls) == {"A": 1, "C": 1}, f"{dict(score_calls)}")

    print("2) four score runs at the same time on 20 new jobs")
    d.jobs.insert_many([{"_id": f"P{i}", "job_id": f"P{i}", "title": f"Job {i}", "company": "ParCo",
                         "keep": True, "score": None, "status": "new", "description": "Junior Python."}
                        for i in range(20)])
    score_calls.clear()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    runs = [threading.Thread(target=score.score_pending, args=(d, cfg)) for _ in range(4)]
    [t.start() for t in runs]
    [t.join() for t in runs]
    check("every job scored exactly once", len(score_calls) == 20 and set(score_calls.values()) == {1},
          f"{dict(score_calls)}")
    check("no claims left behind", d.jobs.count_documents({"scoring_at": {"$exists": True}}) == 0)

    print("3) claims")
    d.jobs.insert_one({"_id": "S", "job_id": "S", "title": "Stale", "company": "X", "keep": True, "score": None,
                       "status": "new", "description": "Junior.", "scoring_at": db.now() - db.CLAIM_TTL * 2})
    d.jobs.insert_one({"_id": "F", "job_id": "F", "title": "Fresh", "company": "X", "keep": True, "score": None,
                       "status": "new", "description": "Junior.", "scoring_at": db.now()})
    score_calls.clear()
    score.score_pending(d, cfg)
    check("abandoned claim (crashed run) gets scored", score_calls["S"] == 1)
    check("job another run is scoring right now is skipped", score_calls["F"] == 0)
    score_calls.clear()
    score.score_pending(d, cfg)
    check("scored job is never picked again", score_calls["S"] == 0 and sum(score_calls.values()) == 0)
finally:
    d.client.drop_database(TEST_DB)

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'} (test database dropped, real data untouched)")
sys.exit(1 if failures else 0)
