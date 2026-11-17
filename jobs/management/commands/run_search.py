import logging

from django.core.management.base import BaseCommand, CommandError

from jobs.models import SearchQuery
from jobs.services.search import SearchRunner, plan_run

logger = logging.getLogger("jobs.search")


class Command(BaseCommand):
    help = (
        "Stage 1 (Search): collect job cards for search queries and store them in PostgreSQL. "
        "Safe to re-run: unfinished queries continue from the saved pagination cursors."
    )

    def add_arguments(self, parser):
        parser.add_argument("--query-id", type=int, action="append",
                            help="ID of SearchQuery (can be repeated). Default: all active queries")
        parser.add_argument("--restart", action="store_true",
                            help="Forget saved progress (slices) and search from scratch")
        parser.add_argument("--include-done", action="store_true",
                            help="Also refresh queries that are already finished (to pick up new jobs)")

    def handle(self, *args, **opts):
        explicit = bool(opts["query_id"])
        if explicit:
            qs = SearchQuery.objects.filter(pk__in=opts["query_id"])
            if not qs.exists():
                raise CommandError("No such query")
        else:
            qs = SearchQuery.objects.filter(is_active=True)

        for q in qs:
            run, restart, reason = plan_run(
                q, restart=opts["restart"], explicit=explicit, include_done=opts["include_done"]
            )
            if not run:
                self.stdout.write(f"--- {q}: skipped ({reason})")
                continue
            self.stdout.write(f"==> {q} ({'full restart' if restart else 'resume'})")
            try:
                found = SearchRunner(q).run(restart=restart)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Interrupted. Progress saved; run again to continue."))
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("query %s crashed", q.pk)
                self.stdout.write(self.style.ERROR(f"    failed: {exc}"))
                continue
            q.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(
                f"    {found} unique jobs, status: {q.get_status_display()}"))
