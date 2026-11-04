"""Semi-auto apply: LinkedIn Easy Apply + company sites.

Opens a real Chrome (own profile in data/chrome-profile - log in once) and goes through jobs with
status='queued' (score >= apply_threshold), highest score first.
  Easy Apply    - fills each step, stops before "Submit application" for your y/s/q.
  Company site  - clicks Apply on LinkedIn, opens the company's form, uploads the CV and fills what it
                  knows. YOU check it, solve any CAPTCHA and click the site's Submit, then press d.
Both: fills ONLY known answers (profile.yaml + answers you gave before, MongoDB `answers`); a NEW question
-> it stops, you answer it in the browser, and your answer is saved for next time.
The description and the answers used are saved to the DB (see the board).

  .venv/bin/python apply.py              # normal run
  .venv/bin/python apply.py --dry-run    # fill forms, never submit (discards at the end)
  .venv/bin/python apply.py --job 4486487512
  .venv/bin/python apply.py --login      # just open the browser to log in
  --debug   save a log + a screenshot and the page HTML of every step to logs/apply/<time>/
  --step    ask y/n before EVERY action (open job, click, fill each field, upload CV, Next, Submit);
            implies --debug
"""
import argparse
import os
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from jobfinder import db
from jobfinder.cleanup import auto_clean
from jobfinder.known_answers import lookup, norm

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

PROFILE_DIR = ROOT / "data" / "chrome-profile"

# LinkedIn's new job page (2026) has random class names (d7ilww...) - match the aria-label instead.
# Last two = the old layout.
APPLY_BTN = ("button[aria-label*='Easy Apply' i], a[aria-label*='Easy Apply' i], "
             "a[aria-label*='company website' i], button[aria-label*='company website' i], "
             "button.jobs-apply-button, a.jobs-apply-button")
# The WHOLE element must read like "Applied 3 days ago": a loose /Applied .* ago/ matched
# "Applied Materials ... 2 days ago" and "...or Applied AI" in a description.
APPLIED_TEXT = re.compile(r"^Applied (just now|now|(\d+|an?) (second|minute|hour|day|week|month|year)s? ago)$", re.I)
# after clicking Submit we wait for LinkedIn to say so (never assume it went through)
SENT_TEXT = re.compile(r"application (was )?sent|application submitted|הבקשה (שלך )?נשלחה|מועמדות(ך)? נשלחה", re.I)
SENT_TIMEOUT = 15
CLOSED_TEXT = re.compile(r"No longer accepting applications", re.I)
CONTINUE_BTN = "div[role=dialog] button:has-text('Continue'), div[role=dialog] a:has-text('Continue')"
VISIBLE_FIELDS = "input:not([type=hidden]):not([type=file]):visible, select:visible, textarea:visible"
SITE_APPLY = re.compile(r"^\s*(apply|apply now|apply for this (job|position|role)|הגש(ת)? מועמדות)\s*$", re.I)
# most specific first. New layout: a native <dialog open> ("Apply to Omnisys"), no role attribute, no <form>.
# find_modal also looks inside iframes.
MODALS = ["div.jobs-easy-apply-modal", "dialog[open]:has-text('Apply to')", "[data-test-modal][role='dialog']",
          "[role='dialog']:has(form)", "[role='dialog']:has-text('Apply to')",
          "dialog[open]:has(select, textarea, input:not([type=hidden]))"]
SUBMIT_BTN = ("button[aria-label='Submit application'], button:has-text('Submit application'), "
              "button[data-live-test-easy-apply-submit-button]")
REVIEW_BTN = ("button[aria-label='Review your application'], button:has-text('Review'), "
              "button[data-live-test-easy-apply-review-button]")
NEXT_BTN = ("button[aria-label='Continue to next step'], button:has-text('Next'), "
            "button[data-easy-apply-next-button], button[data-live-test-easy-apply-next-button]")
DISMISS_BTN = "button[aria-label='Dismiss'], button.artdeco-modal__dismiss"
DISCARD_BTN = "button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')"
FIELD_ERROR = ".artdeco-inline-feedback--error, [aria-invalid='true']"
# old layout ids/classes; new layout: first text box on the page is "About the job" (second = "About the company")
JOB_DESCRIPTION = ("#job-details, div.jobs-description__content, div.jobs-description-content__text, "
                   "[data-testid='expandable-text-box']")

# Fields of the current Easy Apply step. all=false -> only empty ones (tagged with data-as-id).
# all=true -> every field with its current value (to read back what you typed).
COLLECT_JS = r"""
(root, all) => {
  const out = [];
  // ids unique for the whole page: fields of earlier steps can stay in the DOM (hidden) with old ids
  const nextId = () => 'f' + (window.__asSeq = (window.__asSeq || 0) + 1);
  const undouble = s => {  // "QuestionQuestion" (visible + visually-hidden copy) -> "Question"
    const h = s.length / 2;
    return (Number.isInteger(h) && s.slice(0, h) === s.slice(h)) ? s.slice(0, h).trim() : s;
  };
  const clean = t => undouble([...new Set((t || '').split('\n').map(s => s.trim()).filter(Boolean))].join(' ')
                      .replace(/\s+/g, ' ').trim());
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const labelOf = el => {
    if (el.id) { const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`); const t = l && clean(l.innerText); if (t) return t; }
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    return clean(el.getAttribute('aria-label') || el.placeholder || '');
  };
  const near = el => {  // text just before el ("Resume" heading) - for groups without a <legend>
    for (let n = el, up = 0; n && up < 3; n = n.parentElement, up++)
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
        const t = clean(s.innerText); if (t) return t.slice(0, 150);
      }
    return '';
  };
  const groupLabel = fs => {
    const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return clean(lg.innerText);
    if (fs.getAttribute('aria-label')) return clean(fs.getAttribute('aria-label'));
    const by = (fs.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const t = clean(by.map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
    return t || near(fs);
  };
  // new layout: a select's first option can be a real value (Email: one option, preselected)
  const placeholder = o => !o || o.value === '' || !o.text.trim() || /^[-\s]*(select|choose|בחר)\b/i.test(o.text.trim());
  const req = el => el.required || el.getAttribute('aria-required') === 'true';
  const tag = el => { const id = nextId(); el.setAttribute('data-as-id', id); return id; };

  root.querySelectorAll('input[type=text], input[type=number], input[type=tel], input[type=email], input[type=url], input:not([type]), textarea')
    .forEach(el => {
      if (!visible(el)) return;
      const value = (el.value || '').trim();
      if (value && !all) return;
      out.push({id: tag(el), kind: el.tagName === 'TEXTAREA' ? 'textarea' : 'text', input_type: el.type || 'text',
                label: labelOf(el), required: req(el), value});
    });
  root.querySelectorAll('select').forEach(el => {
    if (!visible(el)) return;
    const cur = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
    const value = placeholder(cur) ? '' : cur.text.trim();
    if (value && !all) return;
    const opts = [...el.options].filter(o => !placeholder(o)).map(o => o.text.trim());
    out.push({id: tag(el), kind: 'select', label: labelOf(el), options: opts, required: req(el), value});
  });
  root.querySelectorAll('fieldset').forEach(fs => {
    const radios = [...fs.querySelectorAll('input[type=radio]')];
    if (!radios.length) return;
    // new layout: the CV list has empty <label>s, the file name is in aria-label
    const text = r => { const l = r.id && fs.querySelector(`label[for="${CSS.escape(r.id)}"]`);
                        return (l && clean(l.innerText)) || clean(r.getAttribute('aria-label') || r.value); };
    const checked = radios.find(r => r.checked);
    const value = checked ? text(checked) : '';
    if (value && !all) return;
    out.push({id: tag(fs), kind: 'radio', label: groupLabel(fs), options: radios.map(text), required: true, value});
  });
  return out;
}
"""

# Easy Apply's list of CVs you uploaded before -> [{id, name, date: [y, m, d] | null, checked}]
# new layout: a card per CV ("PDF  Yehonatan Yamin - CV.pdf  11/3/2026"), the file name in the radio's aria-label
RESUMES_JS = r"""
(root) => {
  const out = [];
  for (const r of root.querySelectorAll('input[type=radio]')) {
    let card = r;    // the radio's card: grow while the parent still holds only this one radio
    while (card.parentElement && card.parentElement !== root &&
           card.parentElement.querySelectorAll('input[type=radio]').length === 1) card = card.parentElement;
    const name = (r.getAttribute('aria-label') || '').trim();
    const text = `${name} ${card.innerText || ''}`.replace(/\s+/g, ' ').trim();
    if (!/\.(pdf|docx?)\b/i.test(text)) continue;                 // not a CV (a Yes/No question...)
    const m = text.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/);   // English UI: M/D/YYYY
    if (!r.getAttribute('data-as-cv')) r.setAttribute('data-as-cv', 'cv' + (window.__asSeq = (window.__asSeq || 0) + 1));
    out.push({id: r.getAttribute('data-as-cv'), name: name || text.slice(0, 80),
              date: m ? [+m[3], +m[1], +m[2]] : null, checked: r.checked});
  }
  return out;
}
"""

# checkboxes with their label -> [{id, label, checked}] (to find "Follow <company> to stay up to date...")
CHECKBOXES_JS = r"""
(root) => [...root.querySelectorAll('input[type=checkbox]')].map(el => {
  const l = el.id && root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
  const label = ((l && l.innerText.trim()) || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  if (!el.getAttribute('data-as-cb')) el.setAttribute('data-as-cb', 'cb' + (window.__asSeq = (window.__asSeq || 0) + 1));
  return {id: el.getAttribute('data-as-cb'), label, checked: el.checked};
})
"""
FOLLOW_TEXT = re.compile(r"^follow\b.*|stay up to date with (their|the company'?s?) page", re.I)

CLICK_RADIO_JS = r"""
(fs, value) => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  for (const r of fs.querySelectorAll('input[type=radio]')) {
    const l = r.id && fs.querySelector(`label[for="${CSS.escape(r.id)}"]`);
    const txt = norm((l && norm(l.innerText)) || r.getAttribute('aria-label') || r.value);
    if (txt === norm(value) || txt.startsWith(norm(value))) { (l || r).click(); return true; }
  }
  return false;
}
"""


LOG_DIR = ROOT / "logs" / "apply"
_session = {"dir": None, "n": 0, "log": None}


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for st in self.streams:
            st.write(text)
            st.flush()
        return len(text)

    def flush(self):
        for st in self.streams:
            st.flush()


def start_debug() -> Path:
    d = LOG_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    _session.update(dir=d, log=open(d / "run.log", "a", encoding="utf-8"))
    sys.stdout = _Tee(sys.stdout, _session["log"])
    return d


def snap(page, tag: str) -> None:
    """--debug: screenshot + HTML of the page (and its iframes) for this step."""
    if not _session["dir"]:
        return
    _session["n"] += 1
    base = _session["dir"] / f"{_session['n']:02d}-{re.sub(r'[^a-z0-9]+', '-', tag.lower()).strip('-')[:60]}"
    try:
        page.screenshot(path=f"{base}.png")
        Path(f"{base}.html").write_text(page.content(), encoding="utf-8")
        for i, fr in enumerate(page.frames[1:6], 1):
            if fr.url and not fr.url.startswith("about:"):
                Path(f"{base}.frame{i}.html").write_text(fr.content(), encoding="utf-8")
    except Exception as e:
        print(f"   (snapshot failed: {e})")


STEP = {"on": False}


class Quit(Exception):
    """You pressed q - stop the whole run."""


def confirm(action: str) -> bool:
    """--step: ask before doing `action`. y = do it, n = don't, q = stop the run. Always True otherwise."""
    if not STEP["on"]:
        return True
    a = ask(f"   ? {action}  [y/n/q]", "ynq")
    if a == "q":
        raise Quit()
    return a == "y"


def visible_button(root, selector: str):
    btns = root.locator(selector)
    for i in range(btns.count()):
        b = btns.nth(i)
        if b.is_visible() and b.is_enabled():
            return b
    return None


def pause(a=0.8, b=2.2):
    time.sleep(random.uniform(a, b))


def ask(prompt: str, choices: str) -> str:
    while True:
        a = input(f"{prompt} ").strip().lower()
        if _session["log"]:
            _session["log"].write(f"   > {a}\n")      # your answer, into the debug log only
        if a and a[0] in choices:
            return a[0]


# ---------------------------------------------------------------- browser steps

def ensure_logged_in(page: Page) -> None:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    pause(2, 3)
    if any(k in page.url for k in ("login", "authwall", "checkpoint", "signup")):
        input("\n>>> Log in to LinkedIn in the Chrome window, then press Enter here... ")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")


def find_apply(page: Page, easy_default: bool, wait: float = 8) -> tuple:
    """-> (button, 'easy' | 'site'), or (None, 'applied' | 'closed' | 'none'). Waits a bit for slow pages."""
    deadline = time.time() + wait
    while True:
        btn = visible_button(page, APPLY_BTN)
        if btn is not None:
            label = f"{btn.get_attribute('aria-label') or ''} {btn.inner_text() or ''}".lower()
            if "easy apply" in label or "בקלות" in label:
                return btn, "easy"
            if "company website" in label or btn.locator("[data-test-icon*='external']").count():
                return btn, "site"
            return btn, ("easy" if easy_default else "site")      # unknown wording -> what the scan saw
        if page.get_by_text(APPLIED_TEXT).count():                  # only when there's no button
            return None, "applied"
        if page.get_by_text(CLOSED_TEXT).count():
            return None, "closed"
        if time.time() >= deadline:
            return None, "none"
        time.sleep(0.5)


def find_modal(page: Page, timeout: float = 12) -> tuple:
    """-> (modal, frame) of the Easy Apply form, or (None, None). Looks in the page and in every iframe."""
    deadline = time.time() + timeout
    while True:
        for sel in MODALS:
            for fr in page.frames:
                try:
                    m = fr.locator(sel)
                    for i in range(m.count()):
                        if m.nth(i).is_visible():
                            return m.nth(i), fr
                except Exception:   # frames come and go
                    continue
        if time.time() >= deadline:
            return None, None
        time.sleep(0.5)


def sent_marks(page: Page) -> int:
    """How many "application sent" / "Applied now" texts the page (and its iframes) SHOW right now
    (visible only: a hidden "sent" message that appears later must count as new)."""
    n = 0
    for fr in page.frames:
        try:
            for loc in (fr.get_by_text(SENT_TEXT), fr.get_by_text(APPLIED_TEXT)):
                n += sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        except Exception:   # frames come and go
            continue
    return n


def wait_sent(page: Page, before: int) -> bool:
    """True once LinkedIn shows a NEW "sent" text (more than `before`, counted before clicking Submit)."""
    deadline = time.time() + SENT_TIMEOUT
    while time.time() < deadline:
        if sent_marks(page) > before:
            return True
        time.sleep(0.5)
    return False


def save_description(con, page: Page, job: dict) -> None:
    """Snapshot the description from the logged-in job page (postings get removed later)."""
    try:
        el = page.locator(JOB_DESCRIPTION).first
        text = el.inner_text(timeout=5000).strip()
    except PWTimeout:
        return
    if len(text) > len(job.get("description") or ""):
        db.update_job(con, job["job_id"], {"description": text})


def upload_resume_if_needed(modal, cv_path: str) -> None:
    file_inputs = modal.locator("input[type=file]")
    if file_inputs.count() == 0:
        return
    text = modal.inner_text().lower()
    if Path(cv_path).stem.lower() in text:   # our CV is already listed/selected
        return
    for i in range(file_inputs.count()):
        inp = file_inputs.nth(i)
        meta = ((inp.get_attribute("id") or "") + (inp.get_attribute("name") or "")).lower()
        if "cover" in meta:
            continue
        if "resume" in meta or "resume" in text or "cv" in text:
            if not confirm(f"Upload CV '{Path(cv_path).name}'"):
                return
            inp.set_input_files(cv_path)
            print("   uploaded CV")
            pause(2, 3)
            return


def _cv_day(entry: dict) -> date | None:
    if not entry.get("date"):
        return None
    y, mo, d = entry["date"]
    if mo > 12:                                   # D/M/YYYY (other UI language)
        mo, d = d, mo
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def easy_apply_cv(modal, page: Page, cv_path: str, state: dict) -> None:
    """Easy Apply lists the CVs you uploaded before, each with its upload date. Upload your CV file only if it
    changed after LinkedIn's newest copy of it (same file name) or there's no copy; otherwise make sure that
    newest copy is the selected one. state: per application, so it's done once."""
    if state.get("cv_done"):
        return
    entries = modal.evaluate(RESUMES_JS)
    upload_btn = modal.get_by_role("button", name=re.compile(r"upload (resume|cv)", re.I))
    file_inputs = modal.locator("input[type=file]")
    if not entries and not upload_btn.count() and not (
            file_inputs.count() and re.search(r"\bresume\b|\bcv\b|קורות חיים", modal.inner_text(), re.I)):
        return                                    # no CV section on this step
    state["cv_done"] = True
    fname = Path(cv_path).name.lower()
    file_day = datetime.fromtimestamp(Path(cv_path).stat().st_mtime).date()
    mine = [(day, e) for e in entries if fname in e["name"].lower() and (day := _cv_day(e))]
    newest_day, newest = max(mine, key=lambda x: x[0], default=(None, None))   # ties: first = top of the list

    if newest is not None and newest_day >= file_day:
        if newest["checked"]:
            print(f"   CV: LinkedIn's copy from {newest_day} is selected (your file: {file_day}) - no upload")
        elif confirm(f"Select your CV '{newest['name'][:40]}' from {newest_day} (newest copy; your file: {file_day})"):
            modal.locator(f'[data-as-cv="{newest["id"]}"]').evaluate("el => el.click()")
            print(f"   CV: selected LinkedIn's copy from {newest_day}")
        return

    why = f"LinkedIn's newest copy is from {newest_day}" if newest is not None else "no copy on LinkedIn yet"
    if not confirm(f"Upload your CV '{Path(cv_path).name}' (file changed {file_day}; {why})"):
        return
    try:
        inputs = [file_inputs.nth(i) for i in range(file_inputs.count())]
        inp = next((x for x in inputs
                    if "cover" not in f"{x.get_attribute('id')} {x.get_attribute('name')}".lower()), None)
        if inp is not None:                       # old layout: a file input in the form
            inp.set_input_files(cv_path)
        else:                                     # new layout: a button that opens the file picker
            with page.expect_file_chooser(timeout=8000) as chooser:
                upload_btn.first.click()
            chooser.value.set_files(cv_path)
    except PWTimeout:
        print("   couldn't upload the CV - upload it in the browser")
        return
    if not entries:                               # a plain file field, no list of stored CVs to pick from
        print(f"   CV: uploaded your file (changed {file_day})")
        return
    for _ in range(12):                           # it appears on top with today's date: make sure it's selected
        time.sleep(1)
        fresh = [e for e in modal.evaluate(RESUMES_JS) if fname in e["name"].lower() and _cv_day(e) == date.today()]
        if fresh:
            if not fresh[0]["checked"]:
                modal.locator(f'[data-as-cv="{fresh[0]["id"]}"]').evaluate("el => el.click()")
            print(f"   CV: uploaded your file (changed {file_day}) and selected it")
            return
    print("   uploaded the CV, but can't see it in the list - check which CV is selected in the browser")


def untick_follow(modal) -> None:
    """Untick "Follow <company> to stay up to date with their page" (config follow_companies: false)."""
    for cb in modal.evaluate(CHECKBOXES_JS):
        if not (cb["checked"] and FOLLOW_TEXT.search(cb["label"])):
            continue
        if not confirm(f"Untick '{cb['label'][:70]}'"):
            continue
        box = modal.locator(f'[data-as-cb="{cb["id"]}"]')
        try:
            box.uncheck(timeout=3000)
        except Exception:                         # hidden/custom checkbox -> plain JS click below
            pass
        if box.is_checked():
            box.evaluate("el => el.click()")
        print("   unticked 'Follow'" if not box.is_checked() else "   couldn't untick 'Follow' - untick it in the browser")


def fill_field(modal, page: Page, f: dict, value: str) -> bool:
    loc = modal.locator(f'[data-as-id="{f["id"]}"]')
    try:
        if f["kind"] in ("text", "textarea"):
            loc.fill(value)
            pause(0.6, 1.2)
            # typeahead (e.g. city): pick the first suggestion if a listbox opened
            opt = page.locator("[role=listbox] [role=option]").first
            if opt.count() and opt.is_visible():
                opt.click()
        elif f["kind"] == "select":
            loc.select_option(label=value)
        elif f["kind"] == "radio":
            return bool(loc.evaluate(CLICK_RADIO_JS, value))
        return True
    except PWTimeout:
        return False


def fill_known(root, page_like, profile: dict, cache: dict, log: list) -> list[dict]:
    """Fill every empty field we have an answer for; return the ones we don't (new questions)."""
    new = []
    for f in root.evaluate(COLLECT_JS, False):
        hit = lookup(f, profile, cache)
        if hit and confirm(f"Fill '{f['label'][:60]}' with '{hit[0][:50]}' ({hit[1]})") \
                and fill_field(root, page_like, f, hit[0]):
            log.append({"label": f["label"], "value": hit[0], "source": hit[1]})
        else:
            new.append(f)
    pause()
    return new


def ask_new(pairs: list, con, cache: dict, log: list, prompt: str) -> str:
    """pairs = [(root, field)]. Stops only for required new questions; saves what you type.
    Returns 'ok' / 'skip' / 'quit'."""
    if not any(f["required"] for _, f in pairs):
        for _, f in pairs:
            log.append({"label": f["label"], "value": "", "source": "left empty (optional)"})
        return "ok"

    print("   NEW questions - answer them in the browser:")
    for _, f in pairs:
        opts = f"  options: {', '.join(f['options'])}" if f.get("options") else ""
        print(f"     • {f['label']}{' (required)' if f['required'] else ''}{opts}")
    a = ask(prompt, "csq")
    if a != "c":
        return "skip" if a == "s" else "quit"

    # read back what you typed and remember it
    now = {}
    for root in {id(r): r for r, _ in pairs}.values():
        now.update({norm(x["label"]): x["value"] for x in root.evaluate(COLLECT_JS, True)})
    for _, f in pairs:
        value = now.get(norm(f["label"]), "")
        log.append({"label": f["label"], "value": value, "source": "you"})
        if value:
            cache[norm(f["label"])] = value
            db.save_answer(con, norm(f["label"]), f["label"], value)
    return "ok"


def fill_step(modal, frame, con, profile: dict, cache: dict, log: list) -> str:
    """One Easy Apply step: fill known answers, stop on new required questions. frame = the modal's page/iframe."""
    new = fill_known(modal, frame, profile, cache, log)
    return ask_new([(modal, f) for f in new], con, cache, log, "   Done? [c]ontinue / [s]kip job / [q]uit:")


# ---------------------------------------------------------------- company sites

def open_company_site(page: Page, btn) -> Page | None:
    """Click LinkedIn's (non Easy Apply) Apply button; return the company's page (new tab or same tab)."""
    try:
        with page.context.expect_page(timeout=8000) as info:
            btn.click()
        site = info.value
    except PWTimeout:
        site = page
        cont = page.locator(CONTINUE_BTN).first        # LinkedIn sometimes asks first
        if cont.count() and cont.is_visible():
            try:
                with page.context.expect_page(timeout=8000) as info:
                    cont.click()
                site = info.value
            except PWTimeout:
                pass
    try:   # the link goes through linkedin.com/safety/go/?url=... first, then redirects to the company
        site.wait_for_url(lambda u: "linkedin.com" not in u, timeout=15000)
        site.wait_for_load_state("domcontentloaded", timeout=20000)
    except PWTimeout:
        pass
    return None if "linkedin.com" in site.url else site


def form_roots(site: Page) -> list:
    """[(form root, frame)] for every frame with an application-looking form (forms are often in an iframe)."""
    roots = []
    for fr in site.frames:
        try:
            best, best_n = fr.locator("body"), 0
            forms = fr.locator("form")
            for i in range(min(forms.count(), 10)):      # the biggest form, not the newsletter box
                n = forms.nth(i).locator(VISIBLE_FIELDS).count()
                if n > best_n:
                    best, best_n = forms.nth(i), n
            if best_n == 0:
                best_n = fr.locator(VISIBLE_FIELDS).count()
            if best_n >= 2:
                roots.append((best, fr))
        except Exception:   # frames come and go (ads, captcha); ignore the ones we can't read
            continue
    return roots


def company_apply(site: Page, con, cfg: dict, profile: dict, cache: dict, dry_run: bool) -> tuple[str, list]:
    log: list = []
    print(f"   company site: {site.url[:90]}")
    pause(2.5, 4)
    snap(site, "company-site-landing")
    roots = form_roots(site)
    if not roots:   # the form is often behind the site's own "Apply" button
        btn = site.get_by_role("link", name=SITE_APPLY).or_(site.get_by_role("button", name=SITE_APPLY)).first
        if btn.count() and confirm(f"Click the site's '{(btn.inner_text() or 'Apply').strip()[:30]}' button"):
            try:
                btn.click(timeout=5000)
                pause(2.5, 4)
            except PWTimeout:
                pass
        roots = form_roots(site)
    if not roots:
        snap(site, "company-no-form-found")
        a = ask("   Can't find the application form. Open it in the browser, then [c]ontinue / [s]kip / [q]uit:", "csq")
        if a != "c":
            return ("quit" if a == "q" else "skipped"), log
        roots = form_roots(site)

    pairs = []
    for root, frame in roots:
        upload_resume_if_needed(root, cfg["cv_path"])
        pairs += [(root, f) for f in fill_known(root, frame, profile, cache, log)]
    step = ask_new(pairs, con, cache, log,
                   "   Fill them in the browser (don't submit yet), then [c]ontinue / [s]kip / [q]uit:")
    if step != "ok":
        return ("quit" if step == "quit" else "skipped"), log

    snap(site, "company-form-filled")
    print_log(log)
    if dry_run:
        print("   [dry-run] company form filled - nothing submitted")
        return "skipped", log
    a = ask("   Check the form in the browser, solve any CAPTCHA and click the site's Submit yourself.\n"
            "   Then: [d]one, I applied / [l]ater (keep in queue) / [s]kip / [q]uit:", "dlsq")
    return {"d": "applied", "l": "queued", "s": "skipped", "q": "quit"}[a], log


def click_first_visible(modal, selector: str) -> bool:
    btn = modal.locator(selector)
    for i in range(btn.count()):
        b = btn.nth(i)
        if b.is_visible() and b.is_enabled():
            b.click()
            return True
    return False


def discard(frame) -> None:
    """frame = the page or iframe the Easy Apply form is in."""
    if not confirm("Close the form and discard this unfinished application"):
        print("   left the form open")
        return
    try:
        frame.locator(DISMISS_BTN).first.click(timeout=3000)
        pause(0.8, 1.5)
        frame.locator(DISCARD_BTN).first.click(timeout=3000)
    except PWTimeout:
        pass


def print_log(log: list) -> None:
    if not log:
        print("   Answers: none needed - every field was already filled (by LinkedIn / the site)")
        return
    print("   Answers:")
    for e in log:
        print(f"     {e['label'][:65]:65} = {e['value'][:40]:40} ({e['source']})")


def apply_one(page: Page, con, job: dict, cfg: dict, profile: dict, cache: dict, dry_run: bool) -> tuple[str, list]:
    """Returns (result, answers log). result: applied / queued (later) / skipped / external / failed / quit"""
    print(f"\n=== [{job['score']}] {job['title']} @ {job['company']}\n    {job['url']}\n    {job['match_reason']}")
    if not confirm("Open this job on LinkedIn"):
        return "queued", []
    page.goto(job["url"], wait_until="domcontentloaded")
    pause(2.5, 4.5)
    save_description(con, page, job)
    snap(page, f"{job['job_id']}-job-page")

    btn, kind = find_apply(page, easy_default=bool(job.get("easy_apply")))
    if btn is None:
        if kind == "applied":
            print("   already applied (LinkedIn shows 'Applied ... ago')")
            return "applied", []
        if kind == "closed":
            print("   job closed (no longer accepting applications) - skipping")
            return "skipped", []
        # don't mark it failed: a LinkedIn page change would otherwise "fail" the whole queue in one run
        print("   no Apply button found - left in the queue (see the screenshot)")
        snap(page, "no-apply-button")
        return "queued", []

    easy = kind == "easy"
    label = (btn.get_attribute("aria-label") or btn.inner_text() or "").strip()
    print(f"   {'Easy Apply' if easy else 'company site'} (button: {label[:40]!r})")
    if not easy:
        if cfg.get("easy_apply_only"):
            print("   company site - skipped (easy_apply_only: true)")
            return "external", []
        if not confirm("Click 'Apply' (opens the company site)"):
            return "queued", []
        site = open_company_site(page, btn)
        if site is None:
            snap(page, "company-site-not-opened")
            a = ask("   Couldn't open the company site. Open its application page in the browser, "
                    "then [c]ontinue / [s]kip / [q]uit:", "csq")
            if a != "c":
                return ("quit" if a == "q" else "skipped"), []
            site = next((pg for pg in reversed(page.context.pages) if "linkedin.com" not in pg.url), None)
            if site is None:
                return "failed", []
        result = company_apply(site, con, cfg, profile, cache, dry_run)   # q (Quit) leaves the tab open
        if site is not page and confirm("Close the company site tab"):
            pause(1.5, 2.5)
            site.close()
        return result

    if not confirm("Click 'Easy Apply'"):
        return "queued", []
    btn.click()
    modal, frame = find_modal(page)       # frame: the page itself, or the iframe the form is drawn in
    while modal is None:
        snap(page, "easy-apply-modal-not-found")
        a = ask("   Can't find the Easy Apply form. [r]etry / [l]eave it in the queue / [q]uit:", "rlq")
        if a != "r":
            return ("quit" if a == "q" else "queued"), []
        modal, frame = find_modal(page, timeout=5)
    if frame is not page.main_frame:
        print("   (the form is inside an iframe)")
    log: list = []
    cv_state: dict = {}

    for n in range(15):
        pause(1.2, 2.0)
        easy_apply_cv(modal, page, cfg["cv_path"], cv_state)
        step = fill_step(modal, frame, con, profile, cache, log)
        snap(page, f"easy-apply-step-{n + 1}")
        if step != "ok":
            discard(frame)
            return ("quit" if step == "quit" else "skipped"), log

        if modal.locator(SUBMIT_BTN).count() and modal.locator(SUBMIT_BTN).first.is_visible():
            if not cfg.get("follow_companies", False):
                untick_follow(modal)
                snap(page, "ready-to-submit")
            print_log(log)
            if dry_run:
                print("   [dry-run] reached Submit - discarding")
                discard(frame)
                return "skipped", log
            a = ask("   Submit? (check the form in the browser) [y]es / [s]kip / [q]uit:", "ysq")
            if a != "y":
                discard(frame)
                return ("quit" if a == "q" else "skipped"), log
            before = sent_marks(page)
            click_first_visible(modal, SUBMIT_BTN)
            if not wait_sent(page, before):
                snap(page, "submit-not-confirmed")
                a = ask("   Clicked Submit, but LinkedIn didn't confirm it. Look in the browser - was it sent?\n"
                        "   [y]es / [n]o (leave it in the queue):", "yn")
                if a == "n":
                    print("   not sent - left in the queue (the form is still open in the browser)")
                    return "queued", log
            else:
                print("   ✔ submitted - LinkedIn confirmed")
            snap(page, "submitted")
            try:  # close the "application sent" dialog
                frame.locator(DISMISS_BTN).first.click(timeout=4000)
            except PWTimeout:
                pass
            return "applied", log

        nxt = visible_button(modal, REVIEW_BTN) or visible_button(modal, NEXT_BTN)
        if nxt is None:
            snap(page, "stuck-no-next-button")
            a = ask("   Stuck (no Next/Review). Fix it in the browser, then [c]ontinue / [s]kip / [q]uit:", "csq")
            if a != "c":
                discard(frame)
                return ("quit" if a == "q" else "skipped"), log
            continue
        if not confirm(f"Click '{(nxt.inner_text() or 'Next').strip()[:30]}'"):
            a = ask("   OK - do it yourself in the browser, then [c]ontinue / [s]kip job / [q]uit:", "csq")
            if a != "c":
                discard(frame)
                return ("quit" if a == "q" else "skipped"), log
            continue
        nxt.click()

        pause(1.0, 1.8)
        if modal.locator(FIELD_ERROR).count():
            snap(page, "invalid-fields")
            a = ask("   Some fields are invalid (red in the browser). Fix them, then [c]ontinue / [s]kip / [q]uit:", "csq")
            if a != "c":
                discard(frame)
                return ("quit" if a == "q" else "skipped"), log
            nxt = visible_button(modal, REVIEW_BTN) or visible_button(modal, NEXT_BTN)
            if nxt is not None and confirm(f"Click '{(nxt.inner_text() or 'Next').strip()[:30]}'"):
                nxt.click()

    print("   too many steps - giving up")
    snap(page, "too-many-steps")
    discard(frame)
    return "failed", log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    ap.add_argument("--job", help="apply to one job id (any status)")
    ap.add_argument("--login", action="store_true", help="only open the browser to log in")
    ap.add_argument("--debug", action="store_true", help="log + screenshot + HTML of every step (logs/apply/)")
    ap.add_argument("--step", action="store_true", help="ask y/n before every action (implies --debug)")
    args = ap.parse_args()
    STEP["on"] = args.step
    if args.debug or args.step:
        print(f"debug log + screenshots: {start_debug()}")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    auto_clean(cfg)                    # logs/ from earlier days (keep_logs_days)
    cfg["cv_path"] = os.getenv("CV_PATH", "")
    if not Path(cfg["cv_path"]).exists():
        sys.exit("CV_PATH missing or not found - set it in .env")
    profile = yaml.safe_load((ROOT / "profile.yaml").read_text())

    con = db.connect()
    if args.job:
        jobs = [j for j in [db.get_job(con, args.job)] if j]
    else:
        left = cfg["max_applications_per_day"] - db.applied_today(con)
        if left <= 0 and not args.dry_run:
            sys.exit(f"daily cap reached ({cfg['max_applications_per_day']})")
        jobs = db.queued(con, limit=max(left, 1))
    if not jobs and not args.login:
        sys.exit("nothing queued - run scan.py / score.py first")

    cache = db.load_answers(con)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False, no_viewport=True,
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        if args.login:
            input("Logged in. Press Enter to close... ")
            ctx.close()
            return

        print(f"{len(jobs)} job(s) to go{' (dry-run)' if args.dry_run else ''}"
              f"{' - step mode: y = do it, n = skip that action, q = stop' if args.step else ''}")
        for i, job in enumerate(jobs, 1):
            print(f"\n--- job {i}/{len(jobs)}")
            try:
                result, log = apply_one(page, con, job, cfg, profile, cache, args.dry_run)
            except Quit:
                print("   stopped by you")
                break
            except Exception as e:  # keep going on any page weirdness
                print(f"   error: {e}")
                snap(page, "error")
                result, log = "failed", []
            if log:
                db.save_answers(con, job["job_id"], log, dry_run=args.dry_run)
            if result == "quit":
                break
            if not args.dry_run and result != "queued":   # "queued" = untouched / later
                db.set_status(con, job["job_id"], result)
            pause(4, 9)   # human-ish gap between applications
        ctx.close()


if __name__ == "__main__":
    main()
