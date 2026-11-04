"""Fallback agent for company-site application forms (used by apply.py).

apply.py stays deterministic where it can: known answers, your CV, the form's own Submit button. When that gets
stuck - a required question it has no answer for, no Submit button, or the site doesn't confirm - Claude gets
`agent_tries` tries (config.yaml, default 4). One try = Claude sees the page (the form's elements as a list + a
screenshot) and does one batch of actions through tools: fill / select / tick / upload the CV / click.
In --step mode you approve every action; the click that SENDS an application you always approve.

Claude may use only facts from profile.yaml, your saved answers and your CV. A required question it can't answer
truthfully, a CAPTCHA or a login -> back to you. Some rules are enforced here in code too (no marketing / follow
boxes, no submit in --dry-run).

Notes: company/<Company>/notes.md - what worked and what didn't, appended after every agent run and read before
the next one, together with notes of other companies on the same site (e.g. the same Comeet / Greenhouse host).
"""
import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import yaml

from jobfinder import claude, db
from jobfinder.known_answers import norm

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "company"
MODEL = "claude-opus-5-5"          # config.yaml agent_model overrides it
SENT_TIMEOUT = 15                  # seconds to wait for the site to confirm after a submit click

NOISE_FRAMES = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare|doubleclick|googletagmanager|google-analytics|"
                          r"facebook\.com|youtube|vimeo|hotjar|demdex", re.I)
SUBMITISH = re.compile(r"submit|send|apply|finish|complete|הגש|שלח|סיים", re.I)
MARKETING = re.compile(r"newsletter|marketing|promotion|job alert|talent (community|pool|network)|\bfollow\b|"
                       r"keep me (updated|informed)|updates? (from|about)|\bsms\b|whatsapp|ניוזלטר|דיוור", re.I)
# "the application was received" - checked after a submit click (visible texts NEW since before the click)
SITE_SENT = re.compile(r"thank(s| you)\b.{0,80}\b(appl|submi|interest|candidac|cv|resume)|application (was |has been )?"
                       r"(received|submitted|sent)|we('ve| have) received your|successfully (submitted|sent|applied)|"
                       r"קיבלנו את|תודה על|נשלח(ה|ו)? בהצלחה|פנייתך התקבלה|מועמדותך התקבלה", re.I)
THANKS_ONLY = re.compile(r"^\s*(thank(s| you)( very much)?|תודה( רבה)?)\s*[!.]?\s*$", re.I)
THANKS_URL = re.compile(r"thank|success|confirm|submitted|received", re.I)


# ---------------------------------------------------------------- site checks (also used by apply.py)

def available(cfg: dict) -> bool:
    return int(cfg.get("agent_tries", 4) or 0) > 0 and bool(os.getenv("ANTHROPIC_API_KEY"))


def _frames(site) -> list:
    return [fr for fr in site.frames if fr is site.main_frame or not NOISE_FRAMES.search(fr.url or "")]


def site_sent_marks(site) -> int:
    """How many visible "thank you for applying / application received" texts the site shows right now."""
    n = 0
    for fr in _frames(site):
        try:
            for loc in (fr.get_by_text(SITE_SENT), fr.get_by_text(THANKS_ONLY)):
                n += sum(1 for i in range(min(loc.count(), 20)) if loc.nth(i).is_visible())
        except Exception:      # frames come and go
            continue
    return n


def captcha(site) -> str | None:
    """'challenge' = a CAPTCHA puzzle is open, 'checkbox' = an "I'm not a robot" box to tick, None = neither.
    The script never solves them - it tells you."""
    for fr in site.frames:
        url = fr.url or ""
        if re.search(r"recaptcha/(api2|enterprise)/bframe|hcaptcha\.com.*frame=challenge", url):
            kind = "challenge"
        elif (re.search(r"recaptcha/(api2|enterprise)/anchor", url) and "size=invisible" not in url) \
                or re.search(r"hcaptcha\.com.*frame=checkbox|challenges\.cloudflare\.com", url):
            kind = "checkbox"
        else:
            continue
        try:
            el = fr.frame_element()
            box = el.bounding_box()
            if el.is_visible() and box and box["width"] > 50 and box["height"] > 50:
                return kind
        except Exception:
            continue
    return None


def wait_site_sent(site, before: int, url_before: str, ask, timeout: float | None = None) -> bool:
    """True once the site confirms (a NEW "thank you" text, or a thank-you URL). A CAPTCHA puzzle -> asks you."""
    timeout = SENT_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if site_sent_marks(site) > before:
                return True
            u = urlparse(site.url)
            if site.url != url_before and THANKS_URL.search(f"{u.path}?{u.query}"):
                return True
        except Exception:      # the page is navigating
            pass
        if captcha(site) == "challenge":
            a = ask("   A CAPTCHA puzzle is open - solve it in the browser (the script never does), "
                    "then [c]ontinue / [s]kip:", "cs")
            if a != "c":
                return False
            deadline = time.time() + timeout
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- notes: company/<Company>/notes.md

def notes_file(company: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", company or "").strip(" .-")[:80] or "unknown"
    return NOTES_DIR / safe / "notes.md"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def read_notes(company: str, host: str) -> str:
    """This company's notes + notes of up to 2 other companies on the same site host (same hiring system)."""
    parts, own = [], notes_file(company)
    if own.exists():
        parts.append(f'<notes company="{company}">\n{own.read_text(encoding="utf-8")[-6000:]}\n</notes>')
    if host and NOTES_DIR.is_dir():
        for other in sorted(NOTES_DIR.glob("*/notes.md")):
            text = other.read_text(encoding="utf-8")
            if other != own and f"(host: {host})" in text and len(parts) < 3:
                parts.append(f'<notes company="{other.parent.name}" same_site="{host}">\n{text[-3000:]}\n</notes>')
    return "\n\n".join(parts)


# ---------------------------------------------------------------- what Claude sees

# Every frame: the visible form elements and buttons, each tagged with a stable id (data-as-ag).
OBSERVE_JS = r"""
(prefix) => {
  window.__asAgP = window.__asAgP || prefix;
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelEl = el => (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
  const byIds = ids => clean((ids || '').split(/\s+/).filter(Boolean)
                               .map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
  const near = el => {
    for (let n = el, up = 0; n && up < 3; n = n.parentElement, up++)
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) { const t = clean(s.innerText); if (t) return t; }
    return '';
  };
  const labelOf = el => {
    const l = labelEl(el), t = l && clean(l.innerText);
    return (t || byIds(el.getAttribute('aria-labelledby')) || clean(el.getAttribute('aria-label')) ||
            clean(el.placeholder) || clean(el.title) || near(el) || clean(el.name)).slice(0, 150);
  };
  const groupLabel = (fs, first) => {
    if (fs) {
      const lg = fs.querySelector('legend');
      const t = (lg && clean(lg.innerText)) || clean(fs.getAttribute('aria-label')) ||
                byIds(fs.getAttribute('aria-labelledby')) || near(fs);
      if (t) return t.slice(0, 150);
    }
    return near(first.closest('div') || first).slice(0, 150);
  };
  const required = el => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelOf(el));
  const errorOf = el => ((el.getAttribute('aria-invalid') === 'true' ? 'INVALID ' : '') +
                         (byIds(el.getAttribute('aria-describedby')) || byIds(el.getAttribute('aria-errormessage'))))
                        .trim().slice(0, 150);
  const tag = el => el.dataset.asAg || (el.dataset.asAg = window.__asAgP + (window.__asAgN = (window.__asAgN || 0) + 1));
  const ACTION = /apply|submit|send|next|continue|upload|attach|accept|decline|reject|agree|confirm|הגש|שלח|המשך|אישור|צרף|העלא/i;
  const out = [], radios = new Set();
  const nodes = document.querySelectorAll('input, select, textarea, button, a[href], [role=button], [role=combobox], ' +
                                          '[role=option], [role=checkbox], [role=switch]');
  for (const el of nodes) {
    if (out.length >= 150) break;
    const tn = el.tagName.toLowerCase(), role = el.getAttribute('role');
    const type = (el.getAttribute('type') || (tn === 'input' ? 'text' : '')).toLowerCase();
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (tn === 'input' && ['hidden', 'password', 'image', 'reset'].includes(type)) continue;
    const lab = labelEl(el);
    if (!(shown(el) || shown(lab) || (['radio', 'checkbox', 'file'].includes(type) && shown(el.parentElement)))) continue;
    if (tn === 'input' && type === 'file') {
      out.push({id: tag(el), kind: 'file', label: labelOf(el), required: required(el),
                value: el.files && el.files.length ? el.files[0].name : '', error: errorOf(el)});
    } else if (tn === 'input' && type === 'radio') {
      const key = el.name || el.id;
      if (radios.has(key)) continue;
      radios.add(key);
      const group = el.name ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
      const checked = group.find(r => r.checked);
      out.push({id: tag(el), kind: 'radio', label: groupLabel(el.closest('fieldset, [role=radiogroup]'), el),
                required: group.some(required), options: group.map(r => labelOf(r) || r.value),
                value: checked ? (labelOf(checked) || checked.value) : '', error: errorOf(el)});
    } else if ((tn === 'input' && type === 'checkbox') || role === 'checkbox' || role === 'switch') {
      const on = tn === 'input' ? el.checked : el.getAttribute('aria-checked') === 'true';
      out.push({id: tag(el), kind: 'checkbox', label: labelOf(el), required: required(el),
                value: on ? 'ticked' : 'not ticked', error: errorOf(el)});
    } else if (tn === 'select') {
      const cur = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      const empty = !cur || cur.value === '' || /^[-\s]*(select|choose|please|בחר)/i.test(cur.text);
      out.push({id: tag(el), kind: 'select', label: labelOf(el), required: required(el), value: empty ? '' : clean(cur.text),
                options: [...el.options].map(o => clean(o.text)).filter(Boolean), error: errorOf(el)});
    } else if (tn === 'textarea' || (tn === 'input' && !['submit', 'button'].includes(type))) {
      out.push({id: tag(el), kind: tn === 'textarea' ? 'textarea' : 'text', type, label: labelOf(el),
                required: required(el), value: clean(el.value).slice(0, 200), error: errorOf(el)});
    } else if (role === 'combobox') {
      out.push({id: tag(el), kind: 'combobox', label: labelOf(el), required: required(el),
                value: clean(el.innerText || el.value).slice(0, 100)});
    } else if (role === 'option') {
      out.push({id: tag(el), kind: 'option', label: clean(el.innerText).slice(0, 100)});
    } else {                                     // button, input[type=submit|button], link, role=button
      const text = clean(el.innerText || el.value || el.getAttribute('aria-label') || el.title).slice(0, 80);
      if (!text || (tn === 'a' && !ACTION.test(text))) continue;
      out.push({id: tag(el), kind: tn === 'a' ? 'link' : 'button', type: tn === 'a' ? '' : (type || 'button'), label: text});
    }
  }
  const msgs = [...document.querySelectorAll('[role=alert], [role=status], [aria-live], [class*=error], [class*=success], ' +
                                             '[class*=thank]')]
    .filter(shown).map(e => clean(e.innerText)).filter(t => t && t.length < 300);
  const heads = [...document.querySelectorAll('h1, h2, h3')].filter(shown).map(e => clean(e.innerText)).filter(Boolean);
  return {url: location.href, items: out, messages: [...new Set(msgs)].slice(0, 10), headings: heads.slice(0, 8)};
}
"""

CHOOSE_RADIO_JS = r"""
(el, option) => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const labelOf = r => (r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`)) || r.closest('label');
  const text = r => clean((labelOf(r) && labelOf(r).innerText) || r.getAttribute('aria-label') || r.value);
  const group = el.name ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
  const want = clean(option);
  const hit = group.find(r => text(r) === want) || group.find(r => text(r).startsWith(want)) ||
              group.find(r => text(r).includes(want));
  if (!hit) return null;
  (labelOf(hit) || hit).click();
  return hit.checked ? text(hit) : null;
}
"""


def _fmt(it: dict) -> str:
    kind = it["kind"]
    if it.get("type") and it["type"] not in ("text", "button") and kind in ("text", "button"):
        kind += f"({it['type']})"
    line = f'[{it["id"]}] {kind} "{it["label"]}"' + (" REQUIRED" if it.get("required") else "")
    if it["kind"] not in ("button", "link", "option"):
        line += f" = {json.dumps(it['value'], ensure_ascii=False)}" if it.get("value") else " = (empty)"
    opts = it.get("options")
    if opts:
        if len(opts) > 40:
            opts = opts[:25] + [f"... +{len(opts) - 25} more (give the exact text)"]
        line += "  options: " + " | ".join(opts)
    if it.get("error"):
        line += f"  ! {it['error']}"
    return line


# ---------------------------------------------------------------- tools

def _tool(name: str, description: str, props: dict) -> dict:
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


_S = {"type": "string"}
_ID = {"type": "string", "description": "the element's id from the list, e.g. f0-12"}
_SRC = {"type": "string",
        "description": 'where the value comes from: "profile.<key>", "saved: <question>" or "cv: <words in the CV>"'}
TOOLS = [
    _tool("fill_text", "Type a value into a text field or text area (replaces what's there).",
          {"element_id": _ID, "value": _S, "source": _SRC}),
    _tool("select_option", "Pick an option of a drop-down (select) by its exact text.",
          {"element_id": _ID, "option": _S, "source": _SRC}),
    _tool("choose_radio", "Pick one option of a radio-button group by its text.",
          {"element_id": _ID, "option": _S, "source": _SRC}),
    _tool("set_checkbox", "Tick or untick a checkbox.",
          {"element_id": _ID, "checked": {"type": "boolean"}, "reason": _S}),
    _tool("upload_cv", "Attach the candidate's CV file to a file field.", {"element_id": _ID}),
    _tool("click", "Click a button, link or option. Clicking the form's submit button sends the application - "
                   "the candidate approves that click first.", {"element_id": _ID, "reason": _S}),
    _tool("application_sent", "The page clearly says the application was received. Quote the page's words.",
          {"evidence": _S}),
    _tool("give_up", "Stop and hand the form back to the candidate: a required question you can't answer from the "
                     "facts, a CAPTCHA, a login, or nothing more you can do.",
          {"reason": _S, "questions_for_candidate": {"type": "array", "items": _S}}),
]

OUTCOMES = {"sent": "SUBMITTED", "gave_up": "HANDED BACK TO YOU", "out_of_tries": "OUT OF TRIES",
            "stopped": "AGENT STOPPED", "error": "API ERROR", "quit": "STOPPED BY YOU", "dry_run": "DRY RUN"}


class ActionFailed(Exception):
    """An action didn't work - Claude is told why (an error tool result)."""


class FormAgent:
    def __init__(self, site, job: dict, cfg: dict, profile: dict, cache: dict, con, log: list, ui, dry_run: bool):
        self.site, self.job, self.cfg, self.profile, self.cache, self.con = site, job, cfg, profile, cache, con
        self.log, self.ui, self.dry_run = log, ui, dry_run
        self.tries = int(cfg.get("agent_tries", 4) or 4)
        self.start_url, self.host = site.url, _host(site.url)
        self.ids: dict[str, tuple] = {}          # element id -> (frame, item) from the latest look at the page
        self.frame_no: dict = {}                 # frame -> its id prefix number (stable while the frame lives)
        self.actions: list[dict] = []            # for the notes: what worked / what didn't
        self.said: list[tuple[int, str]] = []    # Claude's one-liners per try
        self.gave_up: dict | None = None
        self.before = list(log)                  # what the deterministic part already filled

    # ---- looking at the page
    def look(self) -> list[dict]:
        self.ids.clear()
        lines, msgs, heads = [], [], []
        for fr in _frames(self.site):
            try:
                seen = fr.evaluate(OBSERVE_JS, f"f{self.frame_no.setdefault(fr, len(self.frame_no))}-")
            except Exception:
                continue
            if fr is not self.site.main_frame and seen["items"]:
                lines.append(f"(inside an iframe: {seen['url'][:80]})")
            for it in seen["items"]:
                self.ids[it["id"]] = (fr, it)
                lines.append(_fmt(it))
            msgs += seen["messages"]
            heads += seen["headings"]
        cap = captcha(self.site)
        text = (f"URL: {self.site.url}\n"
                + (f"Headings: {' | '.join(heads[:8])}\n" if heads else "")
                + (f"Messages on the page: {' | '.join(msgs[:10])}\n" if msgs else "")
                + (f"CAPTCHA on the page: {cap} - the candidate handles CAPTCHAs (give_up if it blocks you)\n" if cap else "")
                + "Elements ([id] kind \"label\" REQUIRED = value, options, ! error):\n" + "\n".join(lines or ["(none)"]))
        content = [{"type": "text", "text": text}]
        shot = self._screenshot()
        if shot:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": shot}})
        return content

    def _screenshot(self) -> str | None:
        try:
            roots = self.ui.form_roots(self.site)
            root = roots[0][0] if roots else None
            if root is not None and root.evaluate("e => e.tagName") != "BODY":
                png = root.screenshot(type="jpeg", quality=70, timeout=5000)
            else:
                png = self.site.screenshot(type="jpeg", quality=70, timeout=5000)
            return base64.standard_b64encode(png).decode()
        except Exception:
            return None

    # ---- Claude
    def system(self) -> str:
        notes = read_notes(self.job["company"], self.host)
        saved = "\n".join(f"- {k}: {v}" for k, v in sorted(self.cache.items())) or "(none yet)"
        profile = yaml.safe_dump(self.profile, allow_unicode=True, sort_keys=False)
        notes_part = ("<notes_from_earlier_runs>\nWritten after earlier runs on this site. Trust what worked; don't "
                      f"repeat what failed.\n{notes}\n</notes_from_earlier_runs>") if notes else \
            "No notes from earlier runs on this site yet."
        return f"""You finish one job application form in a real web browser for a candidate. A script already filled
what it knew. Each try you get the page's elements (id, kind, label, REQUIRED, value, options) and a screenshot,
and you act only through the tools - one batch of tool calls per try. You have {self.tries} tries in total.

Goal: every REQUIRED field filled truthfully, then click the form's submit button (Send / Submit / Apply).
The candidate approves that click. If the page then clearly says the application was received, call
application_sent with the page's words.

Rules - never break them:
1. Use only facts from <profile>, <saved_answers> and <cv>. Never invent experience, skills, dates, numbers, links
   or answers. In `source` name where the value comes from: "profile.<key>", "saved: <question>" or
   "cv: <the words in the CV>".
2. A REQUIRED question you can't answer from those facts: don't guess - call give_up and list the question.
3. Leave optional fields empty unless the answer is plainly in the facts (e.g. a GitHub link).
4. Expected salary: leave it empty; if it's required, give_up.
5. A CAPTCHA, a login, creating an account or a password: give_up - the candidate does those. Never try to solve a
   CAPTCHA.
6. Tick a checkbox only if it's required to apply (privacy policy / terms for processing this application). Never
   tick newsletter, marketing, job alerts, talent community or "follow" boxes.
7. A cookie banner: ignore it unless it blocks the form; then prefer Decline / Reject.
8. The candidate found this job on LinkedIn (e.g. "How did you hear about us?" -> LinkedIn, if that option exists).
9. A file field that already shows a .pdf name has the CV; otherwise use upload_cv.
10. Before your tool calls, write one short line: what you see and what you'll do.

<profile>
{profile}</profile>

<saved_answers>
{saved}
</saved_answers>

<cv>
{claude.cv_text()[:15000]}
</cv>

{notes_part}"""

    def call(self, system: str, messages: list):
        try:
            resp = claude.client().messages.create(
                model=self.cfg.get("agent_model") or MODEL,
                max_tokens=8000,
                output_config={"effort": self.cfg.get("agent_effort", "medium")},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.AuthenticationError:
            print("   agent: Anthropic auth failed - check ANTHROPIC_API_KEY in .env")
            return None
        except anthropic.APIStatusError as e:
            print(f"   agent: API error {e.status_code}: {e.message}")
            return None
        except anthropic.APIConnectionError:
            print("   agent: can't reach the Anthropic API")
            return None
        if resp.stop_reason in ("refusal", "max_tokens"):
            print(f"   agent: stopped ({resp.stop_reason})")
            return None
        return resp

    # ---- the loop
    def run(self) -> str:
        """-> 'submitted' or 'user' (hand the form back to you). Writes the notes either way."""
        outcome, used = "stopped", 0
        try:
            system = self.system()
            messages = [{"role": "user", "content": [{"type": "text", "text":
                         f"Job: {self.job['title']} at {self.job['company']}. Try 1 of {self.tries}. The page:"}]
                         + self.look()}]
            for n in range(1, self.tries + 1):
                used = n
                print(f"   agent: try {n}/{self.tries} - looking at the page...")
                resp = self.call(system, messages)
                if resp is None:
                    outcome = "error"
                    break
                messages.append({"role": "assistant", "content": resp.content})
                for b in resp.content:
                    if b.type == "text" and b.text.strip():
                        self.said.append((n, b.text.strip()[:300]))
                        print(f"   agent: {b.text.strip()[:300]}")
                uses = [b for b in resp.content if b.type == "tool_use"]
                if not uses:
                    break
                results, state = self.act(uses, n)
                if state in ("sent", "gave_up", "dry_run"):
                    outcome = state
                    break
                if n == self.tries:
                    outcome = "out_of_tries"
                    break
                messages.append({"role": "user", "content": results + [
                    {"type": "text", "text": f"Try {n + 1} of {self.tries}. The page now:"}] + self.look()})
        except Exception as e:
            outcome = "quit" if type(e).__name__ == "Quit" else "error"
            if outcome == "error":
                print(f"   agent: error: {str(e).splitlines()[0][:200]}")
            if outcome == "quit":
                raise
        finally:
            self.write_notes(outcome, used)
        if self.gave_up:
            print(f"   agent handed it back: {self.gave_up['reason']}")
            for q in self.gave_up.get("questions_for_candidate", []):
                print(f"     • {q}")
        return "submitted" if outcome == "sent" else "user"

    def act(self, uses: list, n: int) -> tuple[list, str | None]:
        results, state = [], None
        for u in uses:
            if state is not None:              # the page changed (sent / navigated): don't act on old ids
                results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True,
                                "content": "skipped - the page changed after the previous action; look again"})
                continue
            what, note = self.describe(u.name, u.input), self.describe(u.name, u.input, for_notes=True)
            try:
                msg, state = self.do(u.name, u.input, what)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": msg})
                self.actions.append({"try": n, "what": note, "result": msg,
                                     "ok": not msg.startswith(("declined", "no confirmation"))})
            except ActionFailed as e:
                results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True, "content": str(e)})
                self.actions.append({"try": n, "what": note, "ok": False, "result": str(e)})
            except Exception as e:             # Playwright: timeout, detached element...
                if type(e).__name__ == "Quit":
                    raise
                err = f"failed: {str(e).splitlines()[0][:200]}"
                results.append({"type": "tool_result", "tool_use_id": u.id, "is_error": True, "content": err})
                self.actions.append({"try": n, "what": note, "ok": False, "result": err})
        return results, state

    def describe(self, name: str, inp: dict, for_notes: bool = False) -> str:
        """The action in words: with the value for your y/n; in the notes profile values stay out ("(profile.phone)")."""
        it = self.ids.get(inp.get("element_id", ""), (None, {}))[1]
        label = (it.get("label") or inp.get("element_id", "?"))[:60]
        src = inp.get("source", "")
        value = lambda v: f"({src})" if for_notes and src.startswith("profile.") else \
            f"{json.dumps(v[:60], ensure_ascii=False)} ({src})"
        return {
            "fill_text": lambda: f'fill "{label}" = {value(inp.get("value", ""))}',
            "select_option": lambda: f'select "{label}" = {value(inp.get("option", ""))}',
            "choose_radio": lambda: f'choose "{label}" = {value(inp.get("option", ""))}',
            "set_checkbox": lambda: f'{"tick" if inp.get("checked") else "untick"} "{label}" ({inp.get("reason", "")})',
            "upload_cv": lambda: f'upload the CV to "{label}"',
            "click": lambda: f'click "{label}" ({inp.get("reason", "")})',
            "application_sent": lambda: f'says it was sent: "{inp.get("evidence", "")[:80]}"',
            "give_up": lambda: f'hand back: {inp.get("reason", "")}',
        }.get(name, lambda: name)()

    def _remember(self, label: str, value: str, source: str) -> None:
        self.log.append({"label": label, "value": value, "source": f"agent: {source}"})
        if self.ui.step and label:                 # you approved it -> a known answer next time (no agent needed)
            self.cache[norm(label)] = value
            db.save_answer(self.con, norm(label), label, value)

    def do(self, name: str, inp: dict, what: str) -> tuple[str, str | None]:
        """-> (result for Claude, state): None = carry on; 'sent' / 'gave_up' / 'dry_run' end the run;
        'changed' = the page changed, skip the rest of this batch."""
        if name == "give_up":
            self.gave_up = inp
            return "ok - handed back to the candidate", "gave_up"
        if name == "application_sent":
            ok = self.ui.confirm_send(f'Agent says the site confirmed it: "{inp["evidence"][:100]}" - is it sent?')
            return ("the candidate confirmed it's sent", "sent") if ok else ("declined: the candidate says it's NOT sent", None)

        eid = inp.get("element_id", "")
        if eid not in self.ids:
            raise ActionFailed(f"no element with id {eid!r} - use an id from the latest list")
        frame, it = self.ids[eid]
        loc = frame.locator(f'[data-as-ag="{eid}"]').first
        label, kind = it.get("label", ""), it["kind"]

        if name == "click":
            if it.get("type") == "submit" or SUBMITISH.search(label):
                return self._submit_click(loc, label)
            if not self.ui.confirm(f"Agent: {what}"):
                return "declined by the candidate", None
            url, pages = self.site.url, len(self.site.context.pages)
            loc.click(timeout=5000)
            time.sleep(1.5)
            if len(self.site.context.pages) > pages:
                self.site = self.site.context.pages[-1]
                self.site.wait_for_load_state("domcontentloaded", timeout=15000)
                return f"a new tab opened ({self.site.url[:100]}) - you're on it now", "changed"
            return ("clicked - the page changed", "changed") if self.site.url != url else ("clicked", None)

        if name == "set_checkbox":
            if kind != "checkbox":
                raise ActionFailed(f"{eid} is a {kind}, not a checkbox")
            if inp["checked"] and MARKETING.search(label) and not it.get("required"):
                raise ActionFailed("not allowed: newsletter / marketing / follow boxes stay unticked")
            if not self.ui.confirm(f"Agent: {what}"):
                return "declined by the candidate", None
            try:
                loc.set_checked(inp["checked"], timeout=3000)
            except Exception:
                if loc.is_checked() != inp["checked"]:
                    loc.evaluate("el => el.click()")
            if loc.is_checked() != inp["checked"]:
                raise ActionFailed("the box didn't change")
            return f"ok - {'ticked' if inp['checked'] else 'unticked'}", None

        if name == "upload_cv":
            if kind != "file":
                raise ActionFailed(f"{eid} is a {kind}, not a file field")
            if not self.ui.confirm(f"Agent: {what}"):
                return "declined by the candidate", None
            loc.set_input_files(self.cfg["cv_path"])
            return f"ok - attached {Path(self.cfg['cv_path']).name}", None

        if not self.ui.confirm(f"Agent: {what}"):
            return "declined by the candidate", None
        if name == "fill_text":
            if kind == "combobox":
                raise ActionFailed(f"{eid} is a combobox: click it, then click the option that appears")
            if kind not in ("text", "textarea"):
                raise ActionFailed(f"{eid} is a {kind} - use the tool for a {kind}")
            loc.fill(inp["value"], timeout=5000)
            self._remember(label, inp["value"], inp["source"])
            return f"ok - {label[:60]!r} = {loc.input_value()[:80]!r}", None
        if name == "select_option":
            if kind != "select":
                raise ActionFailed(f"{eid} is a {kind}, not a drop-down")
            want = inp["option"].strip().lower()
            pick = next((o for o in it["options"] if o.lower() == want), None)
            close = [o for o in it["options"] if want in o.lower()]
            pick = pick or (close[0] if len(close) == 1 else None)
            if pick is None:
                raise ActionFailed(f"no option {inp['option']!r}" + (f"; close: {close[:6]}" if close else ""))
            loc.select_option(label=pick, timeout=5000)
            self._remember(label, pick, inp["source"])
            return f"ok - selected {pick!r}", None
        if name == "choose_radio":
            if kind != "radio":
                raise ActionFailed(f"{eid} is a {kind}, not a radio group")
            got = loc.evaluate(CHOOSE_RADIO_JS, inp["option"])
            if not got:
                raise ActionFailed(f"no option {inp['option']!r}; options: {it.get('options', [])[:10]}")
            self._remember(label, inp["option"], inp["source"])
            return f"ok - chose {got!r}", None
        raise ActionFailed(f"unknown tool {name}")

    def _submit_click(self, loc, label: str) -> tuple[str, str | None]:
        if self.dry_run:
            return "dry run - the script doesn't send applications; stop here", "dry_run"
        if not self.ui.confirm_send(f"Agent wants to click '{label[:40]}' - this may SEND your application"):
            return "declined: the candidate didn't approve this click", None
        if captcha(self.site) == "checkbox":
            if self.ui.ask("   Tick the \"I'm not a robot\" box in the browser first (the script never does), "
                           "then [c]ontinue / [s]kip:", "cs") != "c":
                return "the candidate stopped at the CAPTCHA", "gave_up"
        before, url = site_sent_marks(self.site), self.site.url
        loc.click(timeout=5000)
        if wait_site_sent(self.site, before, url, self.ui.ask):
            print("   ✔ sent - the site confirmed it")
            return "sent - the site confirmed the application", "sent"
        return "no confirmation: clicked, but the site doesn't say it got it (look at the page for errors)", "changed"

    # ---- notes
    def write_notes(self, outcome: str, used: int) -> None:
        f = notes_file(self.job["company"])
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = [] if f.exists() else [
            f"# {self.job['company']} - application notes", "",
            "Written by apply.py's agent after every run on this company's site, read by it before the next one.",
            "Edit freely - delete a tip that's wrong.", ""]
        lines += [f"## {datetime.now():%Y-%m-%d %H:%M} - {self.job['title']} - {OUTCOMES.get(outcome, outcome)} "
                  f"({used} of {self.tries} tries)",
                  f"- site: {self.start_url} (host: {self.host})"]
        filled = [e["label"][:40] for e in self.before if e.get("value")]
        if filled:
            lines.append("- the script filled before the agent: " + ", ".join(filled))
        ok = [a for a in self.actions if a["ok"]]
        bad = [a for a in self.actions if not a["ok"]]
        lines.append("- worked:" + ("" if ok else " (nothing)"))
        lines += [f"  - try {a['try']}: {a['what']} -> {a['result']}" for a in ok]
        if bad:
            lines.append("- didn't work:")
            lines += [f"  - try {a['try']}: {a['what']} -> {a['result']}" for a in bad]
        if self.said:
            lines.append("- the agent said:")
            lines += [f"  - try {n}: {text}" for n, text in self.said]
        if self.gave_up:
            lines.append(f"- handed back to you: {self.gave_up['reason']}")
            lines += [f"  - {q}" for q in self.gave_up.get("questions_for_candidate", [])]
        with f.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
        print(f"   agent notes: {f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}")


def run(site, job: dict, cfg: dict, profile: dict, cache: dict, con, log: list, ui, dry_run: bool = False) -> str:
    """-> 'submitted' or 'user'. ui: confirm (y/n in --step), confirm_send (always asks), ask, form_roots, step."""
    return FormAgent(site, job, cfg, profile, cache, con, log, ui, dry_run).run()
