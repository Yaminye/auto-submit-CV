"""Proves the auto clean deletes only logs from earlier days - in a throwaway folder (your real logs/ and
data/ are never touched).

    .venv/bin/python tests/check_cleanup.py
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobfinder.cleanup import clean_old_logs  # noqa: E402

tmp = Path(tempfile.mkdtemp())
logs, outside = tmp / "logs", tmp / "outside"
(logs / "apply").mkdir(parents=True)
outside.mkdir()
(outside / "keep-me.txt").write_text("not a log")


def ago(days):
    return (datetime.now() - timedelta(days=days)).timestamp()


def run_folder(days, changed_days=None):
    """An apply.py debug run started `days` ago (last written `changed_days` ago)."""
    d = logs / "apply" / f"{(datetime.now() - timedelta(days=days)):%Y%m%d-%H%M%S}"
    d.mkdir()
    for name in ("run.log", "01-job-page.png", "01-job-page.html"):
        (d / name).write_text("x" * 1000)
        os.utime(d / name, (ago(changed_days if changed_days is not None else days),) * 2)
    os.utime(d, (ago(changed_days if changed_days is not None else days),) * 2)
    return d


def file(name, days):
    f = logs / name
    f.write_text("x" * 500)
    os.utime(f, (ago(days),) * 2)
    return f


old_run, yesterday_run, today_run = run_folder(3), run_folder(1), run_folder(0)
still_writing = run_folder(2, changed_days=0)          # started 2 days ago, still being written today
appending = run_folder(4)                              # all 4 days old, but its run.log was appended today
os.utime(appending / "run.log", (ago(0),) * 2)
os.utime(appending, (ago(4),) * 2)
old_log, new_log, hidden = file("first_scan.log", 2), file("scan_today.log", 0), file(".keep", 9)
link = logs / "apply" / f"{(datetime.now() - timedelta(days=5)):%Y%m%d}-000000"
link.symlink_to(outside, target_is_directory=True)
os.utime(link, (ago(5),) * 2, follow_symlinks=False)

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + str(detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


print("1) keep_logs_days: 0 = off")
check("nothing deleted", clean_old_logs(0, logs) == (0, 0) and old_run.exists() and old_log.exists())

print("2) keep_logs_days: 2 (today + yesterday)")
gone, freed = clean_old_logs(2, logs)
check("deleted: the 3-day-old run, the 2-day-old .log, the 5-day-old link", gone == 3
      and not old_run.exists() and not old_log.exists() and not link.is_symlink(), (gone, freed))
check("kept: yesterday's and today's runs, the runs still being written, today's .log, hidden files",
      all(p.exists() for p in (yesterday_run, today_run, still_writing, appending, new_log, hidden)))
check("a link is removed, never what it points to", (outside / "keep-me.txt").exists())
check("reports the space freed", freed >= 3500, freed)

print("3) keep_logs_days: 1 (only today's - the default)")
gone, _ = clean_old_logs(1, logs)
check("deleted yesterday's run", gone == 1 and not yesterday_run.exists())
check("kept today's run + the runs still being written + today's .log",
      today_run.exists() and still_writing.exists() and appending.exists() and new_log.exists())
check("never deletes the logs/apply folder itself", (logs / "apply").is_dir())

print("4) no logs/ folder yet")
check("does nothing", clean_old_logs(1, tmp / "nope") == (0, 0))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'} (throwaway folder deleted, real logs/ untouched)")
sys.exit(1 if failures else 0)
