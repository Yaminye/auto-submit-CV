import os
import smtplib
from collections import defaultdict
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests


def _group(jobs):
    g = defaultdict(list)
    for j in jobs:
        g[j["category"]].append(j)
    return g


def build_html(jobs) -> str:
    if not jobs:
        return "<p>No new matching jobs today.</p>"
    parts = [f"<h2>{len(jobs)} new jobs &mdash; {date.today():%d/%m/%Y}</h2>"]
    for cat, items in _group(jobs).items():
        parts.append(f"<h3>{escape(cat)} ({len(items)})</h3><table cellpadding='6' "
                     "style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
                     "<tr style='background:#eee'><th align='left'>Title</th><th align='left'>Company</th>"
                     "<th align='left'>Location</th><th>Posted</th><th>Yrs</th></tr>")
        for j in items:
            yrs = j["years"] if j["years"] is not None else "-"
            parts.append(
                f"<tr style='border-bottom:1px solid #ddd'>"
                f"<td><a href='{escape(j['url'])}'>{escape(j['title'])}</a></td>"
                f"<td>{escape(j['company'])}</td><td>{escape(j['location'])}</td>"
                f"<td>{escape(j['posted'])}</td><td align='center'>{yrs}</td></tr>"
            )
        parts.append("</table>")
    return "".join(parts)


def send_email(jobs) -> bool:
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_APP_PASSWORD")
    to = os.getenv("MAIL_TO") or user
    if not (user and pwd):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Job digest {date.today():%d/%m} - {len(jobs)} new"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(build_html(jobs), "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd.replace(" ", ""))
        s.send_message(msg)
    return True


def send_telegram(jobs) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    lines = [f"<b>{len(jobs)} new jobs</b>"] if jobs else ["No new matching jobs today."]
    for cat, items in _group(jobs).items():
        lines.append(f"\n<b>{escape(cat)}</b>")
        lines += [f"• <a href='{j['url']}'>{escape(j['title'])}</a> — {escape(j['company'])}" for j in items]
    # Telegram limit ~4096 chars per message
    chunk, chunks = "", []
    for line in lines:
        if len(chunk) + len(line) > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    chunks.append(chunk)
    for c in chunks:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    return True
