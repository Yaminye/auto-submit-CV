"""Proves apply.py reads LinkedIn job pages right: which button it finds, Easy Apply vs company site,
"already applied" / "closed", the description, and the Easy Apply form even inside an iframe.
Offline: small copies of real LinkedIn markup in a headless browser with the network blocked
(no LinkedIn, no login, no database).

    .venv/bin/python tests/check_apply_page.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

import apply  # noqa: E402

# New layout (2026): random class names - copied from real pages (logs/apply/20261104-222748)
TOP = ('<div class="d7ia2a"><p class="d7ilje">{company}</p><h1>{title}</h1>'
       '<p>Rehovot, Center District, Israel · 2 days ago · 46 people clicked apply</p>{button}</div>')
EASY = ('<button class="d7ilww d7ilwv" type="button" componentkey="1" aria-label="Easy Apply to this job">'
        '<span><svg></svg><span class="d7ilje">Easy Apply</span></span></button>')
SITE = ('<a class="d7ii5k" href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fexample.com" '
        'target="_blank" aria-label="Apply on company website"><span>Apply</span></a>')
DESC = ('<div><div><h2>About the job</h2></div><div><span data-testid="expandable-text-box">{desc}</span></div></div>'
        '<div><h2>About the company</h2><span data-testid="expandable-text-box">We make things.</span></div>')
MORE = ('<h2>More jobs</h2><a href="/jobs/view/1/"><p>Data Engineer</p><p>Applied Materials · Rehovot</p>'
        '<p>3 days ago</p><p class="d7ilje">Easy Apply</p></a>')


def job_page(company, title, button, desc="Python and Docker."):
    return TOP.format(company=company, title=title, button=button) + DESC.format(desc=desc) + MORE


PAGES = [  # (what, html, expected)
    ("Easy Apply button (new layout)", job_page("Omnisys", "Generative AI Engineer", EASY), "easy"),
    ("company-site button (new layout)", job_page("ISCAR", "AI Engineer", SITE), "site"),
    ("company 'Applied Materials' + '2 days ago' is NOT 'already applied'",
     job_page("Applied Materials - Israel", "Devops Engineer", SITE), "site"),
    ("'...or Applied AI' in the description is NOT 'already applied'",
     job_page("SQLink Group", "AI Software Engineer", EASY, "ML Engineering, or Applied AI - mandatory"), "easy"),
    ("...not even when the button is missing",
     job_page("Applied Materials - Israel", "Devops Engineer", "", "Applied AI, posted 2 days ago"), "none"),
    ("really applied", job_page("X", "Y", "<p>Applied 3 days ago</p><a href='#'>See application</a>"), "applied"),
    ("closed", job_page("X", "Y", "<p>No longer accepting applications</p>"), "closed"),
    ("old layout: Easy Apply", '<button class="jobs-apply-button"><span>Easy Apply</span></button>', "easy"),
    ("old layout: company site",
     '<button class="jobs-apply-button"><span>Apply</span><svg data-test-icon="link-external-small"></svg></button>',
     "site"),
]

FORM = ('<div role="dialog" class="jobs-easy-apply-modal"><h2>Apply to Omnisys</h2><form>'
        '<label for="ph">Mobile phone number</label><input id="ph" type="tel" required>'
        '<button aria-label="Continue to next step">Next</button></form></div>')

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + str(detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context()
    ctx.route("**/*", lambda route: route.abort())       # nothing leaves this machine
    page = ctx.new_page()

    print("1) the job page")
    for what, html, expected in PAGES:
        page.set_content(html)
        btn, kind = apply.find_apply(page, easy_default=False, wait=0)
        check(what, kind == expected, f"got {kind!r}, expected {expected!r}")

    page.set_content(job_page("Omnisys", "Generative AI Engineer", EASY, "We build agents."))
    text = page.locator(apply.JOB_DESCRIPTION).first.inner_text()
    check("description = 'About the job' (not 'About the company')", text == "We build agents.", repr(text))

    print("2) the Easy Apply form")
    page.set_content(f"<p>job page</p>{FORM}")
    modal, frame = apply.find_modal(page, timeout=2)
    check("form in the page", modal is not None and frame is page.main_frame)

    page.set_content(f'<p>job page</p><iframe srcdoc="{FORM.replace(chr(34), "&quot;")}"></iframe>')
    page.wait_for_timeout(300)
    modal, frame = apply.find_modal(page, timeout=2)
    check("form inside an iframe (new layout)", modal is not None and frame is not page.main_frame)
    if modal is not None:
        fields = modal.evaluate(apply.COLLECT_JS, False)
        check("reads the iframe form's fields", [f["label"] for f in fields] == ["Mobile phone number"], fields)
        ok = fields and apply.fill_field(modal, frame, fields[0], "0500000000")
        check("fills a field inside the iframe", ok and frame.locator("#ph").input_value() == "0500000000")

    page.set_content("<p>job page, no form</p>")
    modal, frame = apply.find_modal(page, timeout=1)
    check("no form -> nothing found", modal is None)
    browser.close()

print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
