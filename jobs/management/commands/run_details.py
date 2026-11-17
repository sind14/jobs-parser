from django.core.management.base import BaseCommand

from jobs.services.details import DetailsRunner


class Command(BaseCommand):
    help = (
        "Stage 2 (Details): fetch full job postings and company pages for records saved by "
        "the Search stage. Processes only new / failed records, safe to run in parallel."
    )

    def add_arguments(self, parser):
        parser.add_argument("--workers", type=int, help="Parallel threads (default LI_DETAILS_WORKERS)")
        parser.add_argument("--limit", type=int, help="Max records to process in this run (per type)")
        parser.add_argument("--query-id", type=int, help="Only jobs/companies of this search query")
        parser.add_argument("--jobs-only", action="store_true")
        parser.add_argument("--companies-only", action="store_true")
        parser.add_argument("--batch-size", type=int, default=50)
        parser.add_argument("--follow", action="store_true",
                            help="When the queue is empty, wait for new jobs while Search is running")

    def handle(self, *args, **o):
        try:
            runner = DetailsRunner(
                workers=o["workers"], limit=o["limit"], query_id=o["query_id"],
                jobs=not o["companies_only"], companies=not o["jobs_only"],
                batch_size=o["batch_size"], follow=o["follow"],
            )
            stats = runner.run()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING(
                "Interrupted. Records in 'processing' will be released automatically on the next run."))
            return
        for model in runner.blocked:
            self.stdout.write(self.style.WARNING(
                f"{model.__name__}: LinkedIn is blocking these pages from this IP — stopped to avoid "
                "useless requests. Unprocessed records stay in the queue; re-run later."))
        self.stdout.write(self.style.SUCCESS(f"Done: {stats}"))
