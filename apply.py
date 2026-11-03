"""Semi-auto LinkedIn Easy Apply.

Opens a real Chrome (own profile in data/chrome-profile - log in once), goes through jobs with
status='queued' (score >= apply_threshold), fills each Easy Apply form with Claude's help and STOPS
before "Submit application" so you approve every one in the terminal.

  .venv/bin/python apply.py              # normal run
  .venv/bin/python apply.py --dry-run    # fill forms, never submit (discards at the end)
  .venv/bin/python apply.py --job 4486487512
  .venv/bin/python apply.py --login      # just open the browser to log in
"""
import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from jobfinder import claude, db  # noqa: E402  (after load_dotenv so the SDK sees the key)

PROFILE_DIR = ROOT / "data" / "chrome-profile"
ANSWER_CACHE = ROOT / "data" / "answers.json"

EASY_APPLY_BTN = "button.jobs-apply-button:has-text('Easy Apply'), button[aria-label*='Easy Apply']"
MODAL = "div.jobs-easy-apply-modal, div[data-test-modal][role='dialog'], div[role='dialog']:has(form)"
SUBMIT_BTN = "button[aria-label='Submit application'], button:has-text('Submit application')"
REVIEW_BTN = "button[aria-label='Review your application'], button:has-text('Review')"
NEXT_BTN = "button[aria-label='Continue to next step'], button:has-text('Next')"
DISMISS_BTN = "button[aria-label='Dismiss']"
DISCARD_BTN = "button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')"
FIELD_ERROR = ".artdeco-inline-feedback--error"

# Collect empty fields of the current Easy Apply step and tag them with data-as-id.
COLLECT_JS = r"""
(root) => {
  const out = []; let i = 0;
  const undouble = s => {  // "QuestionQuestion" (visible + visually-hidden copy) -> "Question"
    const h = s.length / 2;
    return (Number.isInteger(h) && s.slice(0, h) === s.slice(h)) ? s.slice(0, h).trim() : s;
  };
  const clean = t => undouble([...new Set((t || '').split('\n').map(s => s.trim()).filter(Boolean))].join(' ')
                      .replace(/\s+/g, ' ').trim());
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const labelOf = el => {
    if (el.id) { const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    return clean(el.getAttribute('aria-label') || el.placeholder || '');
  };
  const req = el => el.required || el.getAttribute('aria-required') === 'true';
  root.querySelectorAll('input[type=text], input[type=number], input[type=tel], input[type=email], input[type=url], input:not([type]), textarea')
    .forEach(el => {
      if (!visible(el) || (el.value && el.value.trim())) return;
      const id = 'f' + (i++); el.setAttribute('data-as-id', id);
      out.push({id, kind: el.tagName === 'TEXTAREA' ? 'textarea' : 'text', input_type: el.type || 'text',
                label: labelOf(el), required: req(el)});
    });
  root.querySelectorAll('select').forEach(el => {
    if (!visible(el)) return;
    const cur = el.options[el.selectedIndex];
    if (cur && el.selectedIndex > 0 && !/select an option/i.test(cur.text)) return;
    const opts = [...el.options].map(o => o.text.trim()).filter(t => t && !/^select an option$/i.test(t));
    const id = 'f' + (i++); el.setAttribute('data-as-id', id);
    out.push({id, kind: 'select', label: labelOf(el), options: opts, required: req(el)});
  });
  root.querySelectorAll('fieldset').forEach(fs => {
    const radios = [...fs.querySelectorAll('input[type=radio]')];
    if (!radios.length || radios.some(r => r.checked)) return;
    const opts = radios.map(r => {
      const l = r.id && fs.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      return clean(l ? l.innerText : r.value);
    });
    const id = 'f' + (i++); fs.setAttribute('data-as-id', id);
    out.push({id, kind: 'radio', label: clean(fs.querySelector('legend')?.innerText), options: opts, required: true});
  });
  return out;
}
"""

CLICK_RADIO_JS = r"""
(fs, value) => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  for (const r of fs.querySelectorAll('input[type=radio]')) {
    const l = r.id && fs.querySelector(`label[for="${CSS.escape(r.id)}"]`);
    const txt = norm(l ? l.innerText : r.value);
    if (txt === norm(value) || txt.split(' ')[0] === norm(value)) { (l || r).click(); return true; }
  }
  return false;
}
"""


def pause(a=0.8, b=2.2):
    time.sleep(random.uniform(a, b))


def norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip().lower()


def load_cache() -> dict:
    try:
        return json.loads(ANSWER_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    ANSWER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))


def ask(prompt: str, choices: str) -> str:
    while True:
        a = input(f"{prompt} [{choices}] ").strip().lower()
        if a and a[0] in choices:
            return a[0]


# ---------------------------------------------------------------- browser steps

def ensure_logged_in(page: Page) -> None:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    pause(2, 3)
    if any(k in page.url for k in ("login", "authwall", "checkpoint", "signup")):
        input("\n>>> Log in to LinkedIn in the Chrome window, then press Enter here... ")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")


def upload_resume_if_needed(modal, cv_path: str) -> None:
    file_inputs = modal.locator("input[type=file]")
    if file_inputs.count() == 0:
        return
    text = modal.inner_text().lower()
    if Path(cv_path).stem.lower() in text:   # our CV is already listed/selected
        return
    for i in range(file_inputs.count()):
        inp = file_inputs.nth(i)
        meta = (inp.get_attribute("id") or "") + (inp.get_attribute("name") or "")
        if "cover" in meta.lower():
            continue
        if "resume" in meta.lower() or "upload resume" in text or "resume" in text:
            inp.set_input_files(cv_path)
            print("   uploaded CV")
            pause(2, 3)
            return


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
            match = next((o for o in f["options"] if o.strip().lower() == value.strip().lower()), None)
            if not match:
                return False
            loc.select_option(label=match)
        elif f["kind"] == "radio":
            return bool(loc.evaluate(CLICK_RADIO_JS, value))
        return True
    except PWTimeout:
        return False


def fill_step(modal, page: Page, job: dict, cache: dict, log: list) -> None:
    fields = modal.evaluate(COLLECT_JS)
    if not fields:
        return
    todo = []
    for f in fields:
        cached = cache.get(norm_label(f["label"]))
        if cached and (f["kind"] in ("text", "textarea") or cached in f.get("options", [])):
            if fill_field(modal, page, f, cached):
                log.append((f["label"], cached, "cache"))
                continue
        todo.append(f)
    if not todo:
        return
    answers = claude.answer_fields(todo, job)
    for f in todo:
        value, confident = answers.get(f["id"], ("", False))
        if not value:
            log.append((f["label"], "", "NEEDS YOU"))
            continue
        ok = fill_field(modal, page, f, value)
        log.append((f["label"], value, "ok" if ok and confident else "CHECK"))
        if ok and confident and f["kind"] != "textarea":   # don't cache job-specific free text
            cache[norm_label(f["label"])] = value
    pause()


def click_first_visible(modal, selector: str) -> bool:
    btn = modal.locator(selector)
    for i in range(btn.count()):
        b = btn.nth(i)
        if b.is_visible() and b.is_enabled():
            b.click()
            return True
    return False


def discard(page: Page) -> None:
    try:
        page.locator(DISMISS_BTN).first.click(timeout=3000)
        pause(0.8, 1.5)
        page.locator(DISCARD_BTN).first.click(timeout=3000)
    except PWTimeout:
        pass


def print_log(log: list) -> None:
    for label, value, how in log:
        flag = "  <-- check" if how in ("CHECK", "NEEDS YOU") else ""
        print(f"     {label[:70]:70} = {value[:50]}{flag}")


def apply_one(page: Page, job: dict, cfg: dict, cache: dict, dry_run: bool) -> str:
    """Returns: applied / skipped / external / failed / quit"""
    print(f"\n=== [{job['score']}] {job['title']} @ {job['company']}\n    {job['url']}\n    {job['match_reason']}")
    page.goto(job["url"], wait_until="domcontentloaded")
    pause(2.5, 4.5)

    btn = page.locator(EASY_APPLY_BTN).first
    if btn.count() == 0 or not btn.is_visible():
        if page.locator("text=/Applied .* ago/").count():
            print("   already applied")
            return "applied"
        print("   no Easy Apply (external site) - skipping")
        return "external"
    btn.click()
    try:
        page.locator(MODAL).first.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return "failed"
    modal = page.locator(MODAL).first
    log: list = []

    for _ in range(15):
        pause(1.2, 2.0)
        upload_resume_if_needed(modal, cfg["cv_path"])
        fill_step(modal, page, job, cache, log)

        if modal.locator(SUBMIT_BTN).count() and modal.locator(SUBMIT_BTN).first.is_visible():
            print_log(log)
            if dry_run:
                print("   [dry-run] reached Submit - discarding")
                discard(page)
                return "skipped"
            a = ask("   Submit? (edit fields in the browser first if needed) [y]es/[s]kip/[q]uit", "ysq")
            if a == "q":
                discard(page)
                return "quit"
            if a == "s":
                discard(page)
                return "skipped"
            click_first_visible(modal, SUBMIT_BTN)
            pause(2.5, 4)
            try:  # close the "application sent" dialog
                page.locator(DISMISS_BTN).first.click(timeout=4000)
            except PWTimeout:
                pass
            print("   ✔ submitted")
            return "applied"

        if not (click_first_visible(modal, REVIEW_BTN) or click_first_visible(modal, NEXT_BTN)):
            print_log(log)
            a = ask("   Stuck (no Next/Review). Fix it in the browser, then [c]ontinue / [s]kip / [q]uit", "csq")
            if a != "c":
                discard(page)
                return "quit" if a == "q" else "skipped"
            continue

        pause(1.0, 1.8)
        if modal.locator(FIELD_ERROR).count():
            print_log(log)
            a = ask("   Some fields are invalid (red in the browser). Fix them, then [c]ontinue / [s]kip / [q]uit", "csq")
            if a != "c":
                discard(page)
                return "quit" if a == "q" else "skipped"
            click_first_visible(modal, REVIEW_BTN) or click_first_visible(modal, NEXT_BTN)

    print("   too many steps - giving up")
    discard(page)
    return "failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    ap.add_argument("--job", help="apply to one job id (any status)")
    ap.add_argument("--login", action="store_true", help="only open the browser to log in")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["cv_path"] = os.getenv("CV_PATH", "")
    if not Path(cfg["cv_path"]).exists():
        sys.exit("CV_PATH missing or not found - set it in .env")

    con = db.connect()
    if args.job:
        jobs = [dict(r) for r in con.execute("SELECT * FROM jobs WHERE job_id=?", (args.job,))]
    else:
        left = cfg["max_applications_per_day"] - db.applied_today(con)
        if left <= 0 and not args.dry_run:
            sys.exit(f"daily cap reached ({cfg['max_applications_per_day']})")
        jobs = [dict(r) for r in db.queued(con, limit=max(left, 1))]
    if not jobs and not args.login:
        sys.exit("nothing queued - run scan.py / score.py first")

    cache = load_cache()
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

        print(f"{len(jobs)} job(s) to go{' (dry-run)' if args.dry_run else ''}")
        for job in jobs:
            try:
                result = apply_one(page, job, cfg, cache, args.dry_run)
            except Exception as e:  # keep going on any page weirdness
                print(f"   error: {e}")
                result = "failed"
            save_cache(cache)
            if result == "quit":
                break
            if not args.dry_run:
                db.set_status(con, job["job_id"], result)
            pause(4, 9)   # human-ish gap between applications
        ctx.close()


if __name__ == "__main__":
    main()
