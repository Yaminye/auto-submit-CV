"""Runs ~18:00: send all unsent matches (email and/or Telegram)."""
import sys
from pathlib import Path

from dotenv import load_dotenv

from jobfinder import db, notify

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def main():
    con = db.connect()
    jobs = [dict(r) for r in db.unsent(con)]
    if "--preview" in sys.argv:
        out = ROOT / "data" / "preview.html"
        out.write_text(notify.build_html(jobs), encoding="utf-8")
        print(f"{len(jobs)} jobs -> {out}")
        return
    sent = notify.send_email(jobs) | notify.send_telegram(jobs)
    if not sent:
        sys.exit("No channel configured - fill .env (SMTP_* or TELEGRAM_*)")
    db.mark_sent(con, [j["job_id"] for j in jobs])
    print(f"sent {len(jobs)} jobs")


if __name__ == "__main__":
    main()
