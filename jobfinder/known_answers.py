"""Deterministic answers for Easy Apply fields - from profile.yaml and answers you approved before.
No guessing: anything not matched here returns None and apply.py stops for you to answer."""
import re


def norm(label: str) -> str:
    return re.sub(r"\s+", " ", label or "").strip().lower()


def _pick(value: str, field: dict) -> str | None:
    """For select/radio, return the option matching value (case-insensitive), else None."""
    if field["kind"] not in ("select", "radio"):
        return value
    v = value.strip().lower()
    for o in field.get("options", []):
        ol = o.strip().lower()
        if ol == v or ol.startswith(v + " ") or ol.startswith(v + "("):
            return o
    return None


def _yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


def _skill_alternatives(key: str) -> list[str]:
    # "LLM / GenAI (LangChain, LangGraph)" -> ["llm / genai (langchain, langgraph)", "llm", "genai", "langchain", ...]
    parts = [p.strip().lower() for p in re.split(r"[/,()]", key) if p.strip()]
    return [key.lower()] + [p for p in parts if len(p) >= 2]


def _mentions(label: str, term: str) -> bool:
    return re.search(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", label) is not None


def skill_years(label: str, profile: dict) -> str | None:
    best, best_len = None, 0
    for key, years in (profile.get("skill_years") or {}).items():
        if not isinstance(years, (int, float)):
            continue
        for alt in _skill_alternatives(str(key)):
            if _mentions(label, alt) and len(alt) > best_len:
                best, best_len = str(int(years)), len(alt)
    return best


def from_profile(field: dict, profile: dict) -> str | None:
    lab = norm(field["label"])
    is_yes_no = field["kind"] in ("select", "radio") and any(
        o.strip().lower() in ("yes", "no", "כן", "לא") for o in field.get("options", []))

    # years of experience
    if re.search(r"how many years|years of (\w+ )?experience|שנות ניסיון", lab):
        y = skill_years(lab, profile)
        if y is None and re.fullmatch(
                r"how many years of (total |professional |work )?experience do you have\??", lab):
            y = str(profile.get("total_professional_years", ""))
        return _pick(y, field) if y else None

    # yes / no facts
    if is_yes_no:
        rules = [
            (r"authori[sz]ed to work|legally authori[sz]ed|work permit|right to work", profile.get("work_authorization_israel")),
            (r"sponsorship|visa", profile.get("requires_sponsorship")),
            (r"relocat", profile.get("willing_to_relocate")),
            (r"master|m\.?sc|ph\.?d|doctor", False),
            (r"bachelor|b\.?sc|undergraduate|academic degree", True),
            (r"english|hebrew", True),
            (r"military|idf|army", True),
        ]
        for rx, flag in rules:
            if flag is not None and re.search(rx, lab):
                return _pick(_yes_no(bool(flag)), field)
        return None

    # plain contact fields
    simple = [
        (r"country code", "phone_country"),
        (r"first name|given name", "first_name"),
        (r"last name|surname|family name", "last_name"),
        (r"e-?mail", "email"),
        (r"mobile|phone", "phone"),
        (r"linkedin", "linkedin_url"),
        (r"github", "github_url"),
        (r"website|portfolio", "website"),
        (r"\bcity\b|location \(city\)", "city"),
    ]
    for rx, key in simple:
        if re.search(rx, lab):
            val = profile.get(key)
            return _pick(str(val), field) if val else None
    return None


def lookup(field: dict, profile: dict, cache: dict) -> tuple[str, str] | None:
    """Return (value, source) or None if this is a new question."""
    cached = cache.get(norm(field["label"]))
    if cached:
        v = _pick(cached, field)
        if v:
            return v, "saved"
    v = from_profile(field, profile)
    if v:
        return v, "profile"
    return None
