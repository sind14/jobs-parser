from django.core.management.base import BaseCommand

from jobs.linkedin.parsers import parse_company_page, parse_job_posting
from jobs.models import Company, Job, ProcessingStatus as S, RawPage
from jobs.services.details import apply_company_data, apply_job_data


class Command(BaseCommand):
    help = (
        "Re-run the parsers on stored raw HTML (no requests to LinkedIn). "
        "Use after a parser fix or a LinkedIn markup change."
    )

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=["job", "company", "all"], default="all")
        parser.add_argument("--key", action="append",
                            help="Only this job_id / company slug (can be repeated)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse and report field coverage, but don't save anything")

    def handle(self, *args, **o):
        kinds = ["job", "company"] if o["kind"] == "all" else [o["kind"]]
        for kind in kinds:
            pages = RawPage.objects.filter(kind=kind)
            if o["key"]:
                pages = pages.filter(key__in=o["key"])
            total = pages.count()
            if not total:
                self.stdout.write(f"{kind}: no stored pages")
                continue
            ok = bad = missing = 0
            filled = {}
            for page in pages.iterator(chunk_size=200):
                if kind == "job":
                    obj = Job.objects.filter(job_id=int(page.key)).select_related("company").first()
                    data = parse_job_posting(page.html)
                    applied = obj is not None and apply_job_data(obj, data)
                else:
                    obj = Company.objects.filter(slug=page.key).first()
                    data = parse_company_page(page.html)
                    applied = obj is not None and apply_company_data(obj, data)
                for field, value in data.items():
                    if value not in (None, ""):
                        filled[field] = filled.get(field, 0) + 1
                if obj is None:
                    missing += 1
                    continue
                if not applied:
                    bad += 1
                    continue
                ok += 1
                if not o["dry_run"]:
                    # a successful re-parse also repairs records that failed on a parser bug
                    if obj.details_status == S.FAILED:
                        obj.details_status, obj.details_error = S.DONE, ""
                    obj.save()

            self.stdout.write(self.style.SUCCESS(
                f"{kind}: {total} pages, {ok} re-parsed{' (dry run, nothing saved)' if o['dry_run'] else ''}, "
                f"{bad} empty/unknown layout, {missing} without a DB record"))
            self.stdout.write("  field coverage: " + ", ".join(
                f"{k}={v * 100 // total}%" for k, v in sorted(filled.items())))
