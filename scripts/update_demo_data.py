"""
Refresh the data shipped with the repository after a new parser run:

  * docker/initdb/01-demo-data.sql.gz — DB snapshot that `docker compose up` loads on first start
  * export/linkedin_jobs.xlsx          — Excel export (jobs + company data)

Run from the project root on the host (the `db` service must be running):

    python scripts/update_demo_data.py

The snapshot contains no users, sessions or raw HTML pages (raw pages are a local
debugging aid and would quickly exceed GitHub's file size limit).
"""
import gzip
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "docker" / "initdb" / "01-demo-data.sql.gz"
EXPORT = ROOT / "export" / "linkedin_jobs.xlsx"
EXCLUDE_DATA = [
    "auth_user", "auth_user_groups", "auth_user_user_permissions",
    "django_session", "django_admin_log", "jobs_rawpage",
]
GITHUB_WARN_MB = 50


def main():
    user = os.environ.get("POSTGRES_USER", "linkedin")
    db = os.environ.get("POSTGRES_DB", "linkedin_jobs")
    cmd = ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", user, "-d", db,
           "--no-owner", "--no-privileges"] + [f"--exclude-table-data={t}" for t in EXCLUDE_DATA]
    print("Dumping the database ...", flush=True)
    dump = subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True).stdout
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(SNAPSHOT, "wb", compresslevel=9) as fp:
        fp.write(dump)

    print("Exporting Excel ...", flush=True)
    subprocess.run([sys.executable, "manage.py", "export_jobs", "--format", "xlsx", "-o", str(EXPORT)],
                   cwd=ROOT, check=True)

    for path in (SNAPSHOT, EXPORT):
        mb = path.stat().st_size / 1024 / 1024
        warn = f"  <- over {GITHUB_WARN_MB} MB, GitHub will warn" if mb > GITHUB_WARN_MB else ""
        print(f"{path.relative_to(ROOT)}: {mb:.1f} MB{warn}")
    print('\nNext: git add -A && git commit -m "Update collected data" && git push')


if __name__ == "__main__":
    main()
