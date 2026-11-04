"""Daily runs inside Docker (replaces launchd/cron):
  schedule.scan   (default 17:00)  scan.py  - search, filter, score -> new cards on the board
  schedule.digest (default 18:00)  send.py  - only if SMTP/Telegram is set in .env

If the Mac was asleep at that time, the run happens as soon as Docker is running again the same day.
config.yaml is re-read every minute, so a new time applies without a restart.
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from jobfinder import db

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo(os.getenv("SCHEDULE_TZ", "Asia/Jerusalem"))


def plan(cfg: dict) -> list[tuple[str, str, list[str]]]:
    sch = cfg.get("schedule") or {}
    runs = [("scan", str(sch.get("scan", "17:00")), [sys.executable, "scan.py"])]
    if os.getenv("SMTP_APP_PASSWORD") or os.getenv("TELEGRAM_BOT_TOKEN"):
        runs.append(("digest", str(sch.get("digest", "18:00")), [sys.executable, "send.py"]))
    return runs


def main():
    d = db.connect()
    print(f"scheduler started ({TZ.key})", flush=True)
    while True:
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        now = datetime.now(TZ)
        state = db.get_meta(d, "scheduler")
        for name, hhmm, cmd in plan(cfg):
            h, m = (int(x) for x in hhmm.split(":"))
            due = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= due and state.get(name) != now.date().isoformat():
                print(f"{now:%Y-%m-%d %H:%M} running {name}", flush=True)
                db.set_meta(d, "scheduler", {name: now.date().isoformat()})   # mark first: never runs twice a day
                rc = subprocess.call(cmd, cwd=ROOT)
                print(f"{datetime.now(TZ):%H:%M} {name} finished (exit {rc})", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
