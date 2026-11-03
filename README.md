# auto-submit-CV

Daily LinkedIn job finder for Israel: scans at 17:00, scores every job against your CV with Claude,
puts it on a local job board, and applies (semi-auto) to the good matches via Easy Apply.

- Uses LinkedIn's public (guest) job search, so no login is needed and your account isn't at risk
- Only jobs posted in the last 48h
- Categories: Backend, Full Stack, AI Engineer, Data Science
- Drops jobs asking for > 4 years (minimum of the range), management titles, and Data Science jobs where an
  MSc/PhD is required. Senior titles are kept (`exclude_senior_titles: false`) - the years filter decides
- Never sends the same job twice (SQLite in `data/`)

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill Gmail app password and/or Telegram bot
./launchd/install.sh        # 17:00 scan, always-on board, 18:00 email only if configured
```

## Manual
```bash
.venv/bin/python scan.py             # search + filter
.venv/bin/python send.py --preview   # writes data/preview.html, sends nothing
.venv/bin/python send.py             # send digest
.venv/bin/python recheck.py          # re-run filters on waiting jobs after editing config.yaml
.venv/bin/python recheck.py --dropped  # re-check jobs dropped by the title filter
```

Tune keywords and filters in `config.yaml`.

## Auto-apply (semi-auto, LinkedIn Easy Apply)
After each scan, Claude scores every job against your CV (0-100):
- **≥ 70** → `queued` for apply
- **50-69** → `review` (shown in the digest, not applied - use it to tune the threshold)
- **< 50** → `low`

```bash
cp profile.example.yaml profile.yaml       # your answers (git-ignored) - check skill_years!
# .env: ANTHROPIC_API_KEY=..., CV_PATH=/abs/path/cv.pdf
.venv/bin/playwright install chromium      # only if Chrome isn't installed
.venv/bin/python apply.py --login          # once: log in to LinkedIn in the opened Chrome
.venv/bin/python apply.py --dry-run        # fills forms, never submits
.venv/bin/python apply.py                  # fills known answers, stops on new questions + before Submit
.venv/bin/python score.py [--rescore]      # (re)score jobs by hand
```
Form filling never guesses: it fills only answers from `profile.yaml` and answers you gave before
(`data/answers.json`, edit freely). A new required question -> it stops, you answer in the browser,
press `c`, and your answer is saved. Max 15 applications/day (`config.yaml`).

## Board
```bash
.venv/bin/python dashboard.py              # http://127.0.0.1:8765  (or always-on via install.sh)
```
Columns: Review (50-69) → To apply (≥ 70, apply.py takes these) → Applied → Assignment → Interview 1 →
Interview 2 → Offer → Rejected, plus a collapsed "Not a fit" (< 50 / skipped).
- Drag a card to another column (or use the stage buttons in the job panel). Undo in the toast.
- Click a card: full description, Claude's score + reason, and the exact answers sent.
- "company site" badge = no Easy Apply: apply on the company site, then drag it to Applied.
- List view: search everything, including jobs dropped by the filters and why.
