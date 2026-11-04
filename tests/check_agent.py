"""Proves the company-site flow: deterministic first (the form's own Send button), the agent only when that's
stuck (at most agent_tries tries, notes written + read), you approve every click that sends, a CAPTCHA is
always yours. Claude is FAKED (scripted tool calls - no API call, no cost), the sites are mock pages answered
locally (nothing reaches the internet), throwaway database and notes folder (your real data is never touched).

    .venv/bin/python tests/check_agent.py      (MongoDB must be running)
"""
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["MONGO_DB"] = TEST_DB = f"autosubmit_test_{uuid.uuid4().hex[:8]}"
os.environ["ANTHROPIC_API_KEY"] = "fake-key-claude-is-faked"    # never your real key (.env doesn't override it)

from playwright.sync_api import sync_playwright  # noqa: E402

import apply  # noqa: E402
from jobfinder import agent, claude, db  # noqa: E402

d = db.connect()
assert d.name == TEST_DB, "refusing to run on a real database"
TMP = Path(tempfile.mkdtemp())
agent.NOTES_DIR = NOTES = TMP / "company"
agent.SENT_TIMEOUT = 2
claude.cv_text = lambda: "Test User - B.Sc. Software Engineering. Python, Docker, CI/CD."
PROFILE = {"first_name": "Test", "last_name": "User", "email": "test@example.com", "phone": "0500000000"}
CV = TMP / "mock-cv.pdf"
CV.write_bytes(b"%PDF-1.4 mock")
CFG = {"cv_path": str(CV), "agent_tries": 4, "agent_model": "fake-model"}
JOB = {"job_id": "M1", "title": "Backend Engineer", "company": "MockCo"}
SITE = "https://careers.mockco.example"


def form_page(extra="", captcha_on_send=False):
    """A company form like Raft's: name / email / phone / CV + `extra`, a Send button, a cookie banner."""
    return f"""<html><body><h1>MockCo careers - Backend Engineer</h1>
<div id="cookies"><p>We use cookies</p><button type="button" id="decline">Decline</button></div>
<form id="app" onsubmit="return send(event)">
 <label for="fn">First Name *</label><input id="fn" required>
 <label for="ln">Last Name *</label><input id="ln" required>
 <label for="em">Email *</label><input id="em" type="email" required>
 <label for="ph">Phone</label><input id="ph" type="tel">
 <label for="cv">Resume/CV *</label><input id="cv" name="resume" type="file" required>
 {extra}
 <button type="submit">Send</button>
</form>
<div id="thanks" style="display:none"><h2>Thank you for applying!</h2></div>
<script>
function send(e) {{ e.preventDefault(); {"showCaptcha();" if captcha_on_send else "done();"} return false; }}
function done() {{
  document.body.dataset.sent = 'yes';
  const m = document.getElementById('mkt'); document.body.dataset.mkt = m ? m.checked : 'none';
  app.style.display = 'none'; thanks.style.display = 'block'; }}
function showCaptcha() {{                 // like reCAPTCHA's puzzle popping up after Send
  const f = document.createElement('iframe'); f.id = 'cap';
  f.src = 'https://www.google.com/recaptcha/api2/bframe?k=mock'; f.style.cssText = 'width:400px;height:500px';
  document.body.appendChild(f); }}
</script></body></html>"""


# a required question the script has no answer for + a required privacy box + a newsletter box
HEAR = """<label for="hear">How did you hear about us? *</label>
 <select id="hear" required><option value="">Select...</option><option>LinkedIn</option><option>Friend</option></select>
 <input id="priv" type="checkbox" required><label for="priv">I agree to the privacy policy *</label>
 <input id="mkt" type="checkbox"><label for="mkt">Send me the MockCo newsletter</label>"""
PAGES = {f"{SITE}/full": form_page(), f"{SITE}/hear": form_page(HEAR), f"{SITE}/captcha": form_page(captcha_on_send=True)}


def serve(route):
    url = route.request.url
    if url.startswith("https://www.google.com/recaptcha/api2/bframe"):
        return route.fulfill(content_type="text/html", body="<p>Select all images with traffic lights</p>")
    if url in PAGES:
        return route.fulfill(content_type="text/html", body=PAGES[url])
    return route.abort()


class FakeClaude:
    """Plays Claude: each turn = function(request) -> (one-liner, [(tool, input)]). Records every request."""

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []
        self.messages = self

    def create(self, **req):
        self.requests.append({"model": req["model"], "system": req["system"][0]["text"],
                              "last": req["messages"][-1], "tools": [t["name"] for t in req["tools"]]})
        if not self.turns:
            raise AssertionError("Claude was called more times than scripted")
        text, calls = self.turns.pop(0)(req)
        content = [SimpleNamespace(type="text", text=text)] + [
            SimpleNamespace(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:10]}", name=n, input=i) for n, i in calls]
        return SimpleNamespace(content=content, stop_reason="tool_use" if calls else "end_turn")


def page_text(req):
    return "\n".join(b["text"] for b in req["messages"][-1]["content"] if isinstance(b, dict) and b.get("type") == "text")


def el(req, kind, label):
    """The id the agent was shown for this element (ids are made at run time)."""
    m = re.search(rf'\[(\S+)\] {kind}(?:\([^)]*\))? "{re.escape(label)}', page_text(req))
    return m.group(1) if m else "no-such-id"


class Script:
    """Your answers to the prompts: [(text in prompt, answer or function)], each used once, in order.
    Other y/n/q questions -> 'y'. Anything else -> the test fails instead of hanging."""

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


failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + str(detail)[:300] if detail and not ok else ''}")
    if not ok:
        failures.append(name)


apply.pause = lambda *a, **k: None
apply.STEP["on"] = True
notes = NOTES / "MockCo" / "notes.md"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context()
        ctx.route("**/*", serve)
        holder = {}

        def run(path, script, fake, dry_run=False):
            site = ctx.new_page()
            site.goto(f"{SITE}/{path}")
            holder["site"] = site
            claude.client = lambda: fake
            apply.ask = script
            result, log = apply.company_apply(site, d, JOB, CFG, PROFILE, db.load_answers(d), dry_run)
            return site, result, log

        def sent(site, key="sent"):
            return site.evaluate(f"document.body.dataset.{key}")

        print("1) everything known -> the script clicks Send itself (after your y), no Claude")
        fake, script = FakeClaude(), Script(("this SENDS", "y"))
        site, result, _ = run("full", script, fake)
        check("result = applied, the site got it", result == "applied" and sent(site) == "yes", result)
        check("asked you before the click that sends", any("Click 'Send' - this SENDS" in pr for pr in script.prompts))
        check("Claude never called", fake.requests == [])
        site.close()

        print("2) a required question the script can't answer -> the agent: try 1 fails, try 2 sends")

        def try1(req):
            return "How did you hear is empty; the privacy box too. Picking LinkedIn and sending.", [
                ("select_option", {"element_id": el(req, "select", "How did you hear about us?"), "option": "LinkedIn",
                                   "source": "the candidate found the job on LinkedIn"}),
                ("set_checkbox", {"element_id": el(req, "checkbox", "Send me the MockCo newsletter"), "checked": True,
                                  "reason": "testing the guard"}),
                ("click", {"element_id": "f9-999", "reason": "a wrong id"}),
                ("click", {"element_id": el(req, "button", "Send"), "reason": "submit"})]

        def try2(req):
            return "The privacy box is required and still empty; ticking it and sending.", [
                ("set_checkbox", {"element_id": el(req, "checkbox", "I agree to the privacy policy"), "checked": True,
                                  "reason": "required to apply"}),
                ("click", {"element_id": el(req, "button", "Send"), "reason": "submit"})]

        fake, script = FakeClaude(try1, try2), Script()
        site, result, log = run("hear", script, fake)
        check("result = applied after 2 tries", result == "applied" and len(fake.requests) == 2, len(fake.requests))
        check("you approved each send click", sum("Agent wants to click 'Send'" in pr for pr in script.prompts) == 2)
        first = fake.requests[0]
        check("Claude got the rules, your profile and CV, the model from config",
              "Never invent" in first["system"] and "B.Sc. Software Engineering" in first["system"]
              and "test@example.com" in first["system"] and first["model"] == "fake-model")
        check("Claude sees the page: element list + screenshot",
              "How did you hear about us?" in page_text({"messages": [first["last"]]})
              and any(b.get("type") == "image" for b in first["last"]["content"]))
        res2 = fake.requests[1]["last"]["content"]
        check("tool results come first in the next message", res2[0]["type"] == "tool_result")
        errors = [b["content"] for b in res2 if b.get("type") == "tool_result" and b.get("is_error")]
        check("blocked in code: the newsletter box; a wrong id is an error Claude sees",
              any("not allowed" in e for e in errors) and any("no element with id" in e for e in errors), errors)
        check("the site got it, the newsletter box stayed unticked", sent(site) == "yes" and sent(site, "mkt") == "false")
        check("the agent's answer is saved for next time (you approved it)",
              db.load_answers(d).get("how did you hear about us?") == "LinkedIn", db.load_answers(d))
        text = notes.read_text() if notes.exists() else ""
        check("notes written: outcome, site, what worked, what didn't",
              "SUBMITTED (2 of 4 tries)" in text and "(host: careers.mockco.example)" in text
              and 'select "How did you hear about us? *" = "LinkedIn"' in text and "not allowed: newsletter" in text
              and re.search(r"didn't work:\n(  - .*\n)*  - try 1: click \"Send\" .*-> no confirmation", text), text[-900:])
        site.close()

        print("3) next run: the notes are read first; deterministic part now knows the answer; give_up -> you")
        other = NOTES / "OtherCo" / "notes.md"
        other.parent.mkdir(parents=True)
        other.write_text("## 2026-11-01 - Dev - SUBMITTED\n- site: x (host: careers.mockco.example)\n"
                         "- OtherCo tip: the Send button is at the bottom\n")

        def give_up(req):
            return "A required question I can't answer from the facts.", [
                ("give_up", {"reason": "the form asks for an expected salary", "questions_for_candidate":
                             ["What is your expected salary?"]})]

        fake, script = FakeClaude(give_up), Script(("this SENDS", "n"), ("[d]one", "l"))
        site, result, _ = run("hear", script, fake)
        system = fake.requests[0]["system"] if fake.requests else ""
        check("the script filled 'How did you hear' itself this time (saved answer), tried Send, then the agent",
              any("Fill 'How did you hear about us? *' with 'LinkedIn' (saved)" in pr for pr in script.prompts)
              and len(fake.requests) == 1, script.prompts)
        check("Claude got this company's notes + another company's on the same site",
              '<notes company="MockCo">' in system and "SUBMITTED (2 of 4 tries)" in system
              and 'same_site="careers.mockco.example"' in system and "OtherCo tip" in system)
        check("handed back to you: result = queued (your 'l')", result == "queued", result)
        check("notes: handed back + the question", "HANDED BACK TO YOU" in notes.read_text()
              and "What is your expected salary?" in notes.read_text())
        site.close()

        print("4) the agent gets at most 4 tries, then it's yours")

        def useless(req):
            return "Closing the cookie banner.", [("click", {"element_id": el(req, "button", "Decline"),
                                                             "reason": "cookie banner"})]

        fake, script = FakeClaude(*[useless] * 6), Script(("this SENDS", "n"), ("[d]one", "s"))
        site, result, _ = run("hear", script, fake)
        check("exactly 4 tries", len(fake.requests) == 4, len(fake.requests))
        check("then your prompt: result = skipped (your 's')", result == "skipped", result)
        check("notes: out of tries", "OUT OF TRIES (4 of 4 tries)" in notes.read_text())
        site.close()

        print("5) CAPTCHA after Send: never solved by the script - you do it, then it sees the confirmation")

        def you_solve():
            holder["site"].evaluate("cap.remove(); done()")
            return "c"

        fake, script = FakeClaude(), Script(("this SENDS", "y"), ("CAPTCHA puzzle is open", you_solve))
        site, result, _ = run("captcha", script, fake)
        check("asked you to solve it, then applied", result == "applied"
              and any("CAPTCHA puzzle is open" in pr for pr in script.prompts), script.prompts)
        check("no Claude", fake.requests == [])
        site.close()

        print("6) --dry-run: the agent fills, but never clicks Send")
        d.answers.delete_many({})                 # forget 'How did you hear' -> a new question again

        def fill_and_send(req):
            return "Filling and sending.", [
                ("select_option", {"element_id": el(req, "select", "How did you hear about us?"), "option": "LinkedIn",
                                   "source": "the candidate found the job on LinkedIn"}),
                ("click", {"element_id": el(req, "button", "Send"), "reason": "submit"})]

        fake, script = FakeClaude(fill_and_send), Script()
        site, result, _ = run("hear", script, fake, dry_run=True)
        check("nothing sent, result = skipped", result == "skipped" and sent(site) is None, result)
        check("no send prompt at all, notes say dry run",
              not any("this SENDS" in pr or "Agent wants to click" in pr for pr in script.prompts)
              and "DRY RUN" in notes.read_text(), script.prompts)
        site.close()

        print("7) q during the agent stops the run - and the notes are still written")
        d.answers.delete_many({})

        def one_fill(req):
            return "Picking LinkedIn.", [("select_option", {
                "element_id": el(req, "select", "How did you hear about us?"), "option": "LinkedIn",
                "source": "the candidate found the job on LinkedIn"})]

        fake, script = FakeClaude(one_fill), Script(("Agent: select", "q"))
        try:
            run("hear", script, fake)
            check("q stopped the run", False, "no Quit raised")
        except apply.Quit:
            check("q stopped the run", True)
        check("notes: stopped by you", "STOPPED BY YOU" in notes.read_text())
        browser.close()
finally:
    d.client.drop_database(TEST_DB)
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'} "
      f"(fake Claude, test database dropped, notes in a throwaway folder)")
sys.exit(1 if failures else 0)
