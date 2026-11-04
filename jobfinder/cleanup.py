"""Auto clean, at the start of every apply.py / scan.py run: delete what logs/ collected on earlier days -
apply.py's debug runs (log + screenshots + page copies) and old .log files. Keeps the last
`keep_logs_days` days (config.yaml; 1 = only today's, 0 = never clean).

Only ever deletes inside logs/. Never touches data/ (your LinkedIn login), the database (board, saved
answers, the "already seen / already scored" history), config.yaml or profile.yaml.
"""
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def _day(p: Path) -> date:
    """The newer of: the date in the name (apply.py's run folders: 20261104-222748) and the last change to it
    or anything inside it (a run still writing after midnight is kept)."""
    t = p.lstat().st_mtime
    if p.is_dir() and not p.is_symlink():
        t = max([t] + [f.lstat().st_mtime for f in p.rglob("*")])
    changed = datetime.fromtimestamp(t).date()
    try:
        return max(datetime.strptime(p.name[:8], "%Y%m%d").date(), changed)
    except ValueError:
        return changed


def _size(p: Path) -> int:
    if p.is_symlink() or not p.is_dir():
        return p.lstat().st_size
    return sum(f.lstat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def clean_old_logs(keep_days: int = 1, logs: Path = LOGS) -> tuple[int, int]:
    """Delete logs/ items from before the last `keep_days` days. Items = files right in logs/ and the entries
    of each folder in it (logs/apply/<run>). Returns (items deleted, bytes freed)."""
    if keep_days < 1 or not logs.is_dir() or logs.is_symlink():
        return 0, 0
    cutoff = date.today() - timedelta(days=keep_days - 1)
    items = []
    for p in logs.iterdir():
        if p.is_dir() and not p.is_symlink():
            items += list(p.iterdir())
        else:
            items.append(p)
    gone = freed = 0
    for p in items:
        if p.name.startswith(".") or _day(p) >= cutoff:
            continue
        size = _size(p)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()            # a symlink: only the link goes, never what it points to
        gone, freed = gone + 1, freed + size
    return gone, freed


def auto_clean(cfg: dict) -> None:
    """Prints one line when it deleted something."""
    keep = int(cfg.get("keep_logs_days", 1) or 0)
    try:
        gone, freed = clean_old_logs(keep)
    except OSError as e:
        print(f"(auto clean skipped: {e})")
        return
    if gone:
        print(f"auto clean: deleted {gone} old log item(s) from before the last {keep} day(s), "
              f"{freed / 1e6:.1f} MB freed")
