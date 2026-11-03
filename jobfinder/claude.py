"""Claude calls: score a job vs the CV, and answer Easy Apply form fields."""
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

import anthropic
import yaml
from pypdf import PdfReader

log = logging.getLogger("claude")
ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"   # re-runs on another model if a request is refused


@lru_cache
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic()   # reads ANTHROPIC_API_KEY (loaded from .env)


@lru_cache
def cv_text() -> str:
    path = os.getenv("CV_PATH")
    if not path or not Path(path).exists():
        raise SystemExit("CV_PATH missing or not found - set it in .env")
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


@lru_cache
def profile() -> dict:
    p = ROOT / "profile.yaml"
    if not p.exists():
        raise SystemExit("profile.yaml missing - copy profile.example.yaml and fill it")
    return yaml.safe_load(p.read_text())


def _call(system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict | None:
    """One structured-output call. Returns parsed JSON or None on failure."""
    try:
        resp = client().beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise SystemExit("Anthropic auth failed - set ANTHROPIC_API_KEY in .env")
    except anthropic.RateLimitError:
        log.warning("rate limited by Anthropic API")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Anthropic API error %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Anthropic API connection error")
        return None

    if resp.stop_reason == "refusal":
        log.warning("request refused: %s", resp.stop_details)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("bad JSON from model: %r", text[:200])
        return None


# ---------------------------------------------------------------- scoring

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 fit of this candidate for this job"},
        "reason": {"type": "string", "description": "one short sentence: main reason for the score"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}


def _score_system() -> str:
    return f"""You screen job postings for one candidate and rate how well the candidate fits each job.

Candidate CV:
<cv>
{cv_text()}
</cv>

Candidate preferences: Israel, roles in backend, full stack, AI engineering, data science, or DevOps.
The candidate has about 1 year of professional experience plus a completed B.Sc. (GPA 91).

Scoring guide (0-100):
- 85-100: strong fit - core stack matches and the seniority is junior / ~0-3 years.
- 70-84: good fit - most requirements match; a gap or two that a fast learner can close.
- 50-69: partial fit - the domain is relevant, but there's a significant stack gap, or it asks for more
  experience than the candidate has.
- 0-49: poor fit - a different domain, a senior/lead role in practice, or a hard requirement the candidate
  lacks (e.g. a required MSc/PhD, a specific clearance or tech the candidate never used).
Judge on the actual requirements, not on keywords. Write the reason in English, at most 20 words."""


def score_job(job: dict) -> tuple[int, str] | None:
    user = (f"Job: {job['title']} at {job['company']} ({job.get('location', '')})\n\n"
            f"Description:\n{(job.get('description') or '')[:12000]}")
    out = _call(_score_system(), user, SCORE_SCHEMA, max_tokens=2000)
    if not out:
        return None
    return max(0, min(100, int(out["score"]))), out["reason"]


# ---------------------------------------------------------------- form answers

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "value": {"type": "string"},
                    "confident": {"type": "boolean",
                                  "description": "false if the profile/CV doesn't clearly support this answer"},
                },
                "required": ["id", "value", "confident"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def _answer_system() -> str:
    return f"""You fill in job application form fields for a candidate. Answer truthfully, using only the
profile and CV below. Never invent experience, certificates, or facts.

Profile (authoritative):
<profile>
{yaml.safe_dump(profile(), allow_unicode=True, sort_keys=False)}
</profile>

CV:
<cv>
{cv_text()}
</cv>

Rules:
- For select/radio fields, "value" must be exactly one of the given options (copy the text).
- Numeric fields (input_type number, or the label asks "how many"): digits only.
- "Years of experience with X": use profile.skill_years when X is listed (or clearly the same skill).
  Otherwise, estimate conservatively from the CV, and set confident=false. Never answer above the CV's evidence.
- Notice period: "2-4 weeks"; if the field is numeric in weeks answer 3, in days answer 21.
- Salary: profile.expected_salary is empty, so answer "Negotiable" for text fields. For a required numeric salary
  field, answer "" with confident=false (the human will fill it in).
- Yes/no eligibility (authorized to work in Israel, can commute, etc.): answer from the profile.
- If you cannot answer truthfully, return value "" and confident=false.
- Free-text questions ("why do you want to join", summary): 2-3 honest sentences based on the CV and the job,
  in the language the question is written in."""


def answer_fields(fields: list[dict], job: dict) -> dict[str, tuple[str, bool]]:
    """fields: [{id, kind, label, options?, input_type?, required}] -> {id: (value, confident)}"""
    if not fields:
        return {}
    user = (f"Job: {job['title']} at {job['company']}\n"
            f"Job description (excerpt):\n{(job.get('description') or '')[:4000]}\n\n"
            f"Form fields to fill:\n{json.dumps(fields, ensure_ascii=False, indent=1)}")
    out = _call(_answer_system(), user, ANSWER_SCHEMA, max_tokens=6000)
    if not out:
        return {}
    return {a["id"]: (a["value"], a["confident"]) for a in out["answers"]}
