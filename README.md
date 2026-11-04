# auto-submit-CV

Daily LinkedIn job finder for Israel: scans at 17:00, scores every job against your CV with Claude,
puts it on a local job board, and applies (semi-auto) to the good matches via Easy Apply.

- Uses LinkedIn's public (guest) job search for finding jobs, so no login and no account risk
- Only jobs posted in the last 48h; categories Backend, Full Stack, AI Engineer, Data Science, DevOps
- Easy Apply **and** company-site jobs (`easy_apply_only: false`). Set it to `true` for Easy Apply only: the search
  then asks LinkedIn for Easy Apply jobs and every job page is checked too
- Drops jobs asking for > 4 years (minimum of the range), management titles, and Data Science jobs where an
  MSc/PhD is required. Senior titles are kept (`exclude_senior_titles: false`) - the years filter decides
- Hides jobs in excluded locations (`location_exclude`: Jerusalem)
- Never saves or scores the same job twice: the LinkedIn id is the Mongo `_id`, reposts (same title + company)
  are kept out as "duplicate", and a run must atomically claim a job before scoring it, so even two runs at
  the same time can't score it twice. Proof: `.venv/bin/python tests/check_no_duplicates.py`

## How it runs

| Where | What |
|---|---|
| Docker `mongo` | MongoDB - all the data (volume `auto-submit_mongo-data`) |
| Docker `dashboard` | the board at http://127.0.0.1:8765 |
| Docker `scheduler` | **off for now** - daily 17:00 scan + scoring. Turn on: `docker compose --profile schedule up -d` |
| Your Mac | `apply.py` - opens a real Chrome for Easy Apply, talks to Mongo on `localhost:27017` |

Docker Desktop must be running. Scans run by hand for now (see Commands); with the scheduler on, a scan missed
while the Mac was asleep runs as soon as Docker is up again that day.

## Where the data lives

| What | Where |
|---|---|
| Jobs: descriptions, scores, stages, answers sent | MongoDB `autosubmit.jobs` (one doc per job, `_id` = LinkedIn job id) |
| Form answers you gave | MongoDB `autosubmit.answers` - also editable in the board's **Answers** tab |
| Last scan / scheduler state | MongoDB `autosubmit.meta` |
| Settings | `config.yaml` (search, filters, thresholds, schedule) |
| Your details for forms | `profile.yaml` (git-ignored) |
| Secrets, CV path | `.env` (git-ignored) |
| LinkedIn login session | `data/chrome-profile/` (git-ignored) |

Browse the raw data with MongoDB Compass: `mongodb://localhost:27017` (bound to localhost only, no password).

## Setup
```bash
cp .env.example .env                 # ANTHROPIC_API_KEY, CV_PATH (+ optional Gmail/Telegram)
cp profile.example.yaml profile.yaml # your form answers - check skill_years!
docker compose up -d --build         # Mongo + board

# for apply.py on the Mac
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python apply.py --login    # once: log in to LinkedIn in the opened Chrome
```

## Daily use
1. Scan: `.venv/bin/python scan.py` (or 17:00 automatically once the scheduler is on) - new cards get a NEW badge
2. Open http://127.0.0.1:8765 - check **Review**, drag anything you want into **To apply**
3. `.venv/bin/python apply.py` - goes through **To apply**, stops on new questions and before every Submit
4. Move cards along as you hear back: Assignment → Interview 1 → Interview 2 → Offer / Rejected

## Commands
```bash
docker compose ps                                  # what's running
docker compose restart                             # after editing code
docker compose up -d --build                       # after changing requirements.txt
.venv/bin/python scan.py                           # scan + score now (on the Mac, talks to Mongo on localhost)
.venv/bin/python score.py --rescore              # the only way to score jobs again - asks first
.venv/bin/python recheck.py [--dropped]            # re-run filters after editing config.yaml
.venv/bin/python send.py --preview                 # digest -> data/preview.html
docker compose --profile schedule up -d            # turn the daily 17:00 scan on
docker compose logs -f scheduler                   # its output
docker compose down                                # stop (data stays in the volume)
```

## Scoring and auto-apply
Claude scores every kept job against your CV (0-100): **≥ 70** → To apply, **50-69** → Review, **< 50** → Not a fit.

```bash
.venv/bin/python apply.py --dry-run    # fills forms, never submits
.venv/bin/python apply.py              # fills known answers, stops on new questions + before Submit
.venv/bin/python apply.py --job 4486487512
```
- **Easy Apply**: fills every step and stops before "Submit application" - you press `y`.
- **Company site** (Comeet, Greenhouse, Lever, ...): clicks Apply on LinkedIn, opens the company's form (also inside
  iframes), uploads the CV and fills what it knows. **You** check it, solve any CAPTCHA and click the site's Submit,
  then press `d` (or `l` = later, keeps it in the queue). Sites that need an account (Workday) you do by hand.

Form filling never guesses: it fills only from `profile.yaml` and answers you gave before. A new required
question → it stops, you answer in the browser, press `c`, and your answer is saved. Max 20 applications/day.

## Board
Columns: Review (50-69) → To apply (≥ 70, apply.py takes these) → Applied → Assignment → Interview 1 →
Interview 2 → Offer → Rejected, plus a collapsed "Not a fit" (< 50 / skipped).
- Drag a card to another column (or use the stage buttons in the job panel). Undo in the toast.
- Click a card: full description, Claude's score + reason, and the exact answers sent.
- "company site" badge = no Easy Apply: apply on the company site, then drag it to Applied.
- **List**: search everything, including jobs dropped by the filters and why. **Answers**: edit saved answers.
