"""Decide if a job is relevant: title, years of experience, degree requirement."""
import re

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_num = r"(\d{1,2}|" + "|".join(WORD_NUM) + r")"

# "5+ years", "3-5 years", "at least 5 years", "minimum of 6 yrs"
EN_YEARS = re.compile(
    _num + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*" + _num + r")?\s*\+?\s*(?:years?|yrs?)",
    re.I,
)
# "5 שנות ניסיון", "3-5 שנים", "לפחות 5 שנים"
HE_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–)?\s*(\d{1,2})?\s*\+?\s*(?:שנות|שנים|שנה)")
EXP_CONTEXT = re.compile(
    r"experien|\bexp\b|hands[- ]on|background|working|building|developing|professional|industry"
    r"|production|as a |ניסיון|נסיון|בפיתוח|בתחום", re.I)
# "at least 5 years", "minimum of 5", "over 5 years", "לפחות 5"
STRONG_PREFIX = re.compile(r"(?:at least|minimum(?: of)?|min\.?|over|more than|לפחות|מעל)\s*$", re.I)
NOT_EXP = re.compile(r"founded|ago|since|old\b|history|in business|track record of|הוקמה|לפני", re.I)

DEGREE = re.compile(r"m\.?\s?sc|master'?s|\bms\b|ph\.?\s?d|doctorate|תואר שני|דוקטורט", re.I)
MUST = re.compile(r"requir|must|mandatory|חובה|נדרש", re.I)
SOFT = re.compile(r"prefer|advantage|plus|nice to have|or equivalent|bonus|יתרון|רצוי", re.I)


def _to_int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.lower()
    return int(s) if s.isdigit() else WORD_NUM.get(s)


def years_required(text: str) -> int | None:
    """Highest *minimum* years of experience mentioned in the description."""
    found = []
    for rx in (EN_YEARS, HE_YEARS):
        for m in rx.finditer(text):
            ctx = text[max(0, m.start() - 60): m.end() + 60]
            if NOT_EXP.search(text[max(0, m.start() - 30): m.end() + 15]):
                continue
            token = m.group(0)
            strong = ("+" in token or "plus" in token.lower() or m.group(2)
                      or STRONG_PREFIX.search(text[max(0, m.start() - 20): m.start()]))
            if not strong and not EXP_CONTEXT.search(ctx):
                continue
            n = _to_int(m.group(1))
            if n is not None and 0 < n <= 20:
                found.append(n)
    return max(found) if found else None


def requires_advanced_degree(text: str) -> bool:
    for m in DEGREE.finditer(text):
        ctx = text[max(0, m.start() - 80): m.end() + 80]
        if MUST.search(ctx) and not SOFT.search(ctx):
            return True
    return False


def title_excludes(cfg: dict) -> list[str]:
    """Words that drop a title: always title_exclude, plus senior_title_words if exclude_senior_titles."""
    words = list(cfg.get("title_exclude") or [])
    if cfg.get("exclude_senior_titles"):
        words += cfg.get("senior_title_words") or []
    return words


def title_ok(title: str, excludes: list[str], includes: list[str] | None = None) -> bool:
    t = f" {title.lower()} "
    if any(x.lower() in t for x in excludes):
        return False
    return not includes or any(x.lower() in t for x in includes)


def location_ok(location: str, cfg: dict) -> bool:
    loc = (location or "").lower()
    return not any(x.lower() in loc for x in cfg.get("location_exclude") or [])


def evaluate(job: dict, cfg: dict) -> tuple[bool, str]:
    """Return (keep, reason)."""
    if not title_ok(job["title"], title_excludes(cfg), cfg.get("title_include")):
        return False, "title"
    if not location_ok(job.get("location", ""), cfg):
        return False, "location"
    if any(c.lower() in job["company"].lower() for c in cfg.get("company_blacklist") or []):
        return False, "blacklist"
    if cfg.get("easy_apply_only") and job.get("easy_apply") is False:
        return False, "not Easy Apply"
    if (job.get("seniority") or "").lower() in {"director", "executive"}:
        return False, "seniority"
    desc = job.get("description", "")
    yrs = years_required(desc)
    job["years"] = yrs
    if yrs is not None and yrs > cfg["max_years_required"]:
        return False, f"years>{cfg['max_years_required']} ({yrs})"
    if job["category"] == "Data Science" and requires_advanced_degree(desc):
        return False, "msc/phd required"
    return True, "ok"
