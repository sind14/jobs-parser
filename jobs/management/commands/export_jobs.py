from pathlib import Path

from django.core.management.base import BaseCommand

from jobs.export import write_csv, write_xlsx
from jobs.models import Job


class Command(BaseCommand):
    help = "Export collected jobs (with company data) to CSV or Excel"

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=["csv", "xlsx"], default="csv")
        parser.add_argument("--output", "-o", help="Output file path")
        parser.add_argument("--query-id", type=int, help="Only jobs of this search query")
        parser.add_argument("--only-done", action="store_true", help="Only jobs with collected details")

    def handle(self, *args, **o):
        qs = Job.objects.all()
        if o["query_id"]:
            qs = qs.filter(search_queries=o["query_id"])
        if o["only_done"]:
            qs = qs.filter(details_status="done")
        out = Path(o["output"] or f"export/linkedin_jobs.{o['format']}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if o["format"] == "xlsx":
            n = write_xlsx(qs, out)
        else:
            with out.open("w", encoding="utf-8-sig", newline="") as fp:
                n = write_csv(qs, fp)
        self.stdout.write(self.style.SUCCESS(f"Exported {n} jobs -> {out}"))
