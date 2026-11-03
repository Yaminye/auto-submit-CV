"""Re-run filters on kept-but-unsent jobs (use after changing filters/config)."""
from pathlib import Path

import yaml

from jobfinder import db, filters, linkedin

ROOT = Path(__file__).resolve().parent


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    con = db.connect()
    rows = [dict(r) for r in db.unsent(con)]
    dropped = 0
    for job in rows:
        # cheap title check first, no network
        if not filters.title_ok(job["title"], cfg["title_exclude"], cfg.get("title_include")):
            con.execute("UPDATE jobs SET keep=0, reason='title' WHERE job_id=?", (job["job_id"],))
            con.commit()
            dropped += 1
            print(f"  - {job['title']} @ {job['company']}: title")
            continue
        job.update(linkedin.details(job["job_id"]))
        keep, reason = filters.evaluate(job, cfg)
        con.execute("UPDATE jobs SET keep=?, reason=?, years=? WHERE job_id=?",
                    (int(keep), reason, job.get("years"), job["job_id"]))
        con.commit()
        if not keep:
            dropped += 1
            print(f"  - {job['title']} @ {job['company']}: {reason}")
        linkedin._pause(1.0, 2.0)
    print(f"rechecked {len(rows)}, dropped {dropped}")


if __name__ == "__main__":
    main()
