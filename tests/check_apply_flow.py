"""Runs the real apply.py flow (apply_one, step mode) on fake job pages in LinkedIn's NEW layout:
Easy Apply form drawn in a hidden "interop" iframe, company link through linkedin.com/safety/go.
Your y/n answers are scripted. Headless browser, every request answered locally (nothing reaches
LinkedIn or any site), throwaway database (your real data is never touched).

    .venv/bin/python tests/check_apply_flow.py      (MongoDB must be running)
"""
import html
import os
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["MONGO_DB"] = TEST_DB = f"autosubmit_test_{uuid.uuid4().hex[:8]}"

from playwright.sync_api import sync_playwright  # noqa: E402

import apply  # noqa: E402
from jobfinder import db  # noqa: E402

d = db.connect()
assert d.name == TEST_DB, "refusing to run on a real database"

PROFILE = {"first_name": "Test", "last_name": "User", "email": "test@example.com", "phone": "0500000000",
           "linkedin_url": "https://www.linkedin.com/in/test", "skill_years": {"Python": 3}}
CV = Path(tempfile.mkdtemp()) / "mock-cv.pdf"
CV.write_bytes(b"%PDF-1.4 mock")
CFG = {"cv_path": str(CV), "easy_apply_only": False}


def modal(extra=""):
    """Multi-step Easy Apply form, like LinkedIn's: phone -> resume -> questions -> review/submit."""
    return f"""<body>
<div id="m" class="jobs-easy-apply-modal" role="dialog" style="display:none"><h2>Apply to MockCo</h2>
 <button aria-label="Dismiss" onclick="m.style.display='none'; if (!document.body.dataset.submitted) d.style.display='block'">x</button>
 <form>
  <div id="s1"><label for="ph">Mobile phone number</label><input id="ph" type="text" required>
   <button type="button" aria-label="Continue to next step" onclick="s1.style.display='none'; s2.style.display='block'">Next</button></div>
  <div id="s2" style="display:none"><p>Upload resume</p><input type="file" id="resume" name="resume">
   <button type="button" aria-label="Continue to next step" onclick="s2.style.display='none'; s3.style.display='block'">Next</button></div>
  <div id="s3" style="display:none">
   <label for="yp">How many years of work experience do you have with Python?</label><input id="yp" type="number" required>
   {extra}
   <button type="button" aria-label="Review your application" onclick="s3.style.display='none'; s4.style.display='block'">Review</button></div>
  <div id="s4" style="display:none"><p>Review your application</p>
   <button type="button" aria-label="Submit application" onclick="document.body.dataset.submitted='yes'; s4.style.display='none'; sent.style.display='block'">Submit application</button></div>
  <div id="sent" style="display:none"><p>Your application was sent to MockCo</p></div>
 </form>
</div>
<div id="d" role="dialog" style="display:none"><p>Discard application?</p>
 <button data-control-name="discard_application_confirm_btn" onclick="d.style.display='none'; document.body.dataset.discarded='yes'">Discard</button></div>
</body>"""


def job_page(button, iframe_html="", extra=""):
    """New layout: random class names, aria-label buttons; the form in a hidden full-screen iframe or in `extra`."""
    iframe = (f'<iframe id="interop" data-testid="interop-iframe" srcdoc="{html.escape(iframe_html)}" style="opacity:0;'
              ' z-index:-1; width:100vw; height:100vh; position:absolute; top:0; border:0"></iframe>'
              if iframe_html else "")
    return f"""<html><body>
<div class="d7ia2a"><p>MockCo</p><h1>Mock Engineer</h1><p>Tel Aviv · 1 day ago · 12 applicants</p>{button}</div>
<div><div><h2>About the job</h2></div><div><span data-testid="expandable-text-box">Mock job: Python, Docker.</span></div></div>
{iframe}{extra}
<script>function openApply() {{ const f = document.getElementById('interop');
  f.style.opacity = 1; f.style.zIndex = 10; f.contentDocument.getElementById('m').style.display = 'block'; }}</script>
</body></html>"""


EASY = '<button class="d7ilww" type="button" aria-label="Easy Apply to this job" onclick="openApply()"><span>Easy Apply</span></button>'
EASY_DIALOG = EASY.replace("openApply()", "dlg.showModal()")


def one_page_dialog(on_submit, checked="r1"):
    """The real new form ("Apply to Omnisys", 2026-11-04): native <dialog>, all pre-filled from the LinkedIn
    profile, a card per stored CV (file name in aria-label + upload date), "Follow" ticked, Submit right away.
    "Upload resume" opens a file picker (no file input in the dialog)."""
    def cv(rid, name, day):
        return (f'<div><div><span>PDF</span><p>{name}</p><p>{day}</p></div><input id="{rid}" type="radio" name="cv" '
                f'aria-label="{name}"{" checked" if rid == checked else ""}><label for="{rid}"></label></div>')
    return f"""<dialog id="dlg" data-testid="dialog" aria-labelledby="dialog-header">
 <header id="dialog-header"><h2>Apply to MockCo</h2></header>
 <button type="button" aria-label="Dismiss" onclick="dlg.close()">x</button>
 <div id="dbody"><h3>Contact info</h3>
  <label for="em"><span>Email*</span></label>
  <select id="em" required><option value="test@example.com">test@example.com</option></select>
  <label for="cc"><span>Phone country code*</span></label>
  <select id="cc" required><option value="ad">Andorra (+376)</option><option value="il" selected>Israel (+972)</option></select>
  <label for="ph"><span>Phone*</span></label><input id="ph" type="tel" required value="+972500000000">
  <div><h3>Resume</h3><p>Select or upload a resume in DOC, DOCX, or PDF format</p></div>
  <fieldset role="radiogroup" id="cvs">{cv("r1", "mock-cv.pdf", "11/3/2026")}{cv("r2", "mock-cv.pdf", "10/30/2026")}{cv("r3", "Cover Letter.pdf", "11/5/2026")}</fieldset>
  <button type="button" onclick="up.click()">Upload resume</button>
  <input id="fol" type="checkbox" checked aria-label="Follow MockCo to stay up to date with their page"><label for="fol"></label>
  <button type="button" onclick="{on_submit}">Submit application</button></div>
</dialog>
<input type="file" id="up" style="display:none" onchange="addCv()">
<script>function addCv() {{     // LinkedIn puts the upload on top, dated today, selected
  const d = new Date(), day = (d.getMonth() + 1) + '/' + d.getDate() + '/' + d.getFullYear();
  document.querySelectorAll('#cvs input').forEach(r => r.checked = false);
  const div = document.createElement('div');
  div.innerHTML = '<div><span>PDF</span><p>mock-cv.pdf</p><p>' + day + '</p></div>' +
                  '<input id="rn" type="radio" name="cv" aria-label="mock-cv.pdf" checked><label for="rn"></label>';
  cvs.prepend(div); document.body.dataset.uploaded = up.files[0].name; }}</script>"""


# Submit: remember what was sent (which CV, Follow ticked?), then show LinkedIn's confirmation
SENT = ("document.body.dataset.submitted='yes'; document.body.dataset.follow=fol.checked; "
        "document.body.dataset.cv=document.querySelector('#cvs input:checked').id; "
        "dbody.innerHTML='<p>Your application was sent to MockCo</p>'")
NOTHING = "document.body.dataset.clicked='yes'"       # LinkedIn shows no confirmation
OCT30 = datetime(2026, 10, 30, 9, 7).timestamp()       # like your real CV file
SITE = ('<a class="d7ii5k" target="_blank" aria-label="Apply on company website" '
        'href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers.mockco.example%2Fjob"><span>Apply</span></a>')
COMPANY = """<html><body><h1>MockCo careers - Backend Engineer</h1>
<form id="newsletter"><label for="nl">Email</label><input id="nl" type="email"><button>Subscribe</button></form>
<a href="#" onclick="document.getElementById('wrap').style.display='block'; return false;">Apply for this job</a>
<div id="wrap" style="display:none"><iframe style="width:800px;height:900px" srcdoc='
<form id="application">
  <label for="fn">First Name *</label><input id="fn" type="text" aria-required="true">
  <label for="ln">Last Name *</label><input id="ln" type="text" aria-required="true">
  <label for="em">Email *</label><input id="em" type="email" aria-required="true">
  <label for="ph">Phone</label><input id="ph" type="tel">
  <label for="resume">Resume/CV *</label><input id="resume" name="resume" type="file">
  <label for="hear">How did you hear about us? *</label>
  <select id="hear" aria-required="true"><option>Select...</option><option>LinkedIn</option><option>Friend</option></select>
  <button type="submit">Submit Application</button>
</form>'></iframe></div></body></html>"""

PAGES = {
    "https://www.linkedin.com/jobs/view/E1/": job_page(EASY, modal()),
    "https://www.linkedin.com/jobs/view/E2/": job_page(EASY, modal(
        '<label for="color">What is your favorite color?</label><input id="color" type="text" required>')),
    "https://www.linkedin.com/jobs/view/D1/": job_page(EASY_DIALOG, extra=one_page_dialog(SENT)),
    "https://www.linkedin.com/jobs/view/D2/": job_page(EASY_DIALOG, extra=one_page_dialog(NOTHING)),
    "https://www.linkedin.com/jobs/view/D3/": job_page(EASY_DIALOG, extra=one_page_dialog(SENT, checked="r2")),
    "https://www.linkedin.com/jobs/view/C1/": job_page(SITE),
    "https://www.linkedin.com/jobs/view/N1/": job_page("<p>Applied Materials · posted 2 days ago</p>"),
    "https://careers.mockco.example/job": COMPANY,
}


def serve(route):
    url = route.request.url
    if url.startswith("https://www.linkedin.com/safety/go/"):    # LinkedIn's "leaving LinkedIn" redirect
        # (a script redirect, not a 302: Playwright doesn't intercept a 302's target, so it'd go unserved)
        return route.fulfill(status=200, content_type="text/html",
                             body='<script>location.replace("https://careers.mockco.example/job")</script>')
    if url in PAGES:
        return route.fulfill(status=200, content_type="text/html", body=PAGES[url])
    return route.abort()


class Script:
    """Answers apply.py's prompts: rules [(text in prompt, answer or function)], each used once, in order.
    Any other y/n/q question -> 'y'. Anything else -> test fails (instead of hanging)."""

    def __init__(self, *rules):
        self.rules, self.prompts = list(rules), []

    def __call__(self, prompt, choices):
        self.prompts.append(prompt.strip())
        for i, (needle, answer) in enumerate(self.rules):
            if needle in prompt:
                self.rules.pop(i)
                return answer() if callable(answer) else answer
        if "[y/n/q]" in prompt:
            return "y"
        raise AssertionError(f"unexpected prompt: {prompt!r}")


def job(job_id):
    j = {"_id": job_id, "job_id": job_id, "title": "Mock Engineer", "company": "MockCo", "score": 90,
         "url": f"https://www.linkedin.com/jobs/view/{job_id}/", "match_reason": "mock", "keep": True,
         "status": "queued", "easy_apply": job_id[0] in "ED", "description": ""}
    d.jobs.replace_one({"_id": job_id}, j, upsert=True)
    return j


failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + str(detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


apply.pause = lambda *a, **k: None    # no human-like waits in a test
apply.STEP["on"] = True               # --step: y/n before every action

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context()
        ctx.route("**/*", serve)

        def run(job_id, script):
            page = ctx.new_page()
            apply.ask = script
            apply_result = apply.apply_one(page, d, job(job_id), CFG, PROFILE, db.load_answers(d), False)
            log = apply_result[1]
            form = next((f for f in page.frames if f is not page.main_frame), None)
            return page, form, apply_result, {e["label"]: e["value"] for e in log}

        print("1) Easy Apply (form in the iframe), y to everything")
        page, form, (result, _), answers = run("E1", Script(("Submit?", "y")))
        check("result = applied", result == "applied", result)
        check("Submit clicked inside the iframe", form.evaluate("document.body.dataset.submitted") == "yes")
        check("phone + Python years filled from profile",
              answers.get("Mobile phone number") == "0500000000"
              and answers.get("How many years of work experience do you have with Python?") == "3", answers)
        check("CV uploaded", form.locator("#resume").evaluate("e => e.files.length") == 1)
        check("description saved from the job page", db.get_job(d, "E1")["description"] == "Mock job: Python, Docker.")
        page.close()

        print("2) Easy Apply, s at Submit")
        page, form, (result, _), _ = run("E1", Script(("Submit?", "s")))
        check("result = skipped", result == "skipped", result)
        check("nothing submitted", form.evaluate("document.body.dataset.submitted") is None)
        check("form closed + discarded (inside the iframe)", form.evaluate("document.body.dataset.discarded") == "yes")
        page.close()

        print("3) Easy Apply with a NEW required question")
        holder = {}

        def you_type_blue():
            holder["form"].fill("#color", "Blue")
            return "c"

        script = Script(("Done?", you_type_blue), ("Submit?", "y"))
        page = ctx.new_page()
        page.on("frameattached", lambda fr: holder.update(form=fr))
        apply.ask = script
        result, log = apply.apply_one(page, d, job("E2"), CFG, PROFILE, db.load_answers(d), False)
        check("stopped and asked about it", any("Done?" in pr for pr in script.prompts))
        check("result = applied", result == "applied", result)
        saved = db.load_answers(d)
        check("your answer saved for next time", saved.get("what is your favorite color?") == "Blue", saved)
        page.close()

        print("4) company site (link through linkedin.com/safety/go)")

        def you_pick_linkedin():
            site = next(pg for pg in ctx.pages if "mockco.example" in pg.url)
            next(f for f in site.frames if f is not site.main_frame).select_option("#hear", label="LinkedIn")
            return "c"

        script = Script(("Fill them in the browser", you_pick_linkedin), ("[d]one", "d"))
        page, _, (result, _), answers = run("C1", script)
        check("result = applied (after your 'd')", result == "applied", result)
        check("company site opened (redirect followed)",
              any("Click the site's 'Apply for this job'" in pr for pr in script.prompts), script.prompts)
        check("name/email filled, CV upload asked",
              answers.get("First Name *") == "Test" and answers.get("Email *") == "test@example.com"
              and any("Upload CV" in pr for pr in script.prompts), answers)
        check("company tab closed", not any("mockco.example" in pg.url for pg in ctx.pages))
        page.close()

        print("5) job page without an Apply button")
        page, _, (result, _), _ = run("N1", Script())
        check("left in the queue, not 'failed' / not 'applied'", result == "queued", result)
        page.close()

        def sent(page, key):
            return page.evaluate(f"document.body.dataset.{key}")

        print("6) new one-page form (<dialog>): pre-filled, your file (Oct 30) older than the newest copy (Nov 3)")
        os.utime(CV, (OCT30, OCT30))
        script = Script(("Submit?", "y"))
        page, _, (result, log), _ = run("D1", script)
        check("result = applied, LinkedIn confirmed (no 'didn't confirm' question)",
              result == "applied" and not any("didn't confirm" in pr for pr in script.prompts), script.prompts)
        check("touched nothing pre-filled: no Fill / new-question prompts, empty answer log",
              not any(pr.startswith(("? Fill", "Done?")) for pr in script.prompts) and log == [], (script.prompts, log))
        check("no upload, sent the newest copy (Nov 3; not the newer-dated cover letter)",
              sent(page, "uploaded") is None and sent(page, "cv") == "r1"
              and not any("Upload your CV" in pr for pr in script.prompts), script.prompts)
        check("asked to untick 'Follow', and it was unticked when sent",
              any(pr.startswith("? Untick 'Follow MockCo") for pr in script.prompts) and sent(page, "follow") == "false")
        page.close()

        print("6b) an older copy is selected -> selects the newest copy")
        script = Script(("Submit?", "y"))
        page, _, (result, _), _ = run("D3", script)
        check("asked, then sent the Nov 3 copy instead of the Oct 30 one",
              any(pr.startswith("? Select your CV") for pr in script.prompts) and sent(page, "cv") == "r1"
              and result == "applied", (script.prompts, sent(page, "cv")))
        page.close()

        print("6c) you changed your CV file after the newest copy -> uploads it and sends it")
        os.utime(CV, None)                          # file changed now (after Nov 3)
        script = Script(("Submit?", "y"))
        page, _, (result, _), _ = run("D1", script)
        check("asked to upload, uploaded through LinkedIn's file picker",
              any(pr.startswith("? Upload your CV 'mock-cv.pdf'") for pr in script.prompts)
              and sent(page, "uploaded") == "mock-cv.pdf", script.prompts)
        check("the new upload (today) is the CV that was sent", sent(page, "cv") == "rn" and result == "applied",
              sent(page, "cv"))
        page.close()

        print("6d) 'n' to untick Follow -> stays ticked")
        os.utime(CV, (OCT30, OCT30))
        page, _, (result, _), _ = run("D1", Script(("Untick", "n"), ("Submit?", "y")))
        check("Follow left as LinkedIn had it", sent(page, "follow") == "true" and result == "applied")
        page.close()

        print("7) Submit clicked but LinkedIn shows no confirmation -> asks you; n = stays in the queue")
        apply.SENT_TIMEOUT = 1
        script = Script(("Submit?", "y"), ("didn't confirm", "n"))
        page, _, (result, _), _ = run("D2", script)
        check("asked you instead of assuming", any("didn't confirm" in pr for pr in script.prompts), script.prompts)
        check("result = queued (not 'applied')", result == "queued", result)
        check("Submit was clicked", page.evaluate("document.body.dataset.clicked") == "yes")
        page.close()
        browser.close()
finally:
    d.client.drop_database(TEST_DB)

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'} (test database dropped, real data untouched)")
sys.exit(1 if failures else 0)
