# auto-submit-CV

Daily LinkedIn job finder for Israel. It scans at 17:00 and sends a digest at 18:00.
Auto-submit is planned for a later stage.

- Uses LinkedIn's public (guest) job search, so no login is needed and your account isn't at risk
- Only jobs posted in the last 48h
- Categories: Backend, Full Stack, AI Engineer, Data Science
- Drops senior/lead titles, jobs asking for >4 years, and Data Science jobs where an MSc/PhD is required
- Never sends the same job twice (SQLite in `data/`)

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill Gmail app password and/or Telegram bot
./launchd/install.sh        # daily 17:00 scan + 18:00 send (Mac must be awake)
```

## Manual
```bash
.venv/bin/python scan.py             # search + filter
.venv/bin/python send.py --preview   # writes data/preview.html, sends nothing
.venv/bin/python send.py             # send digest
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

## Dashboard
```bash
.venv/bin/python dashboard.py              # http://127.0.0.1:8765
```
Every job in the DB: queue / review / applied / low / dropped-by-filters (with the reason),
full description, Claude's score + reason, and the exact answers sent for each application.
Move jobs between queue / review / skipped from the job panel.
