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
