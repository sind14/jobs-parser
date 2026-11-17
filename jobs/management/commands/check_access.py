"""
Safe check whether LinkedIn currently serves this IP.

Exactly ONE request per endpoint type (search page, job posting, company page),
no retries, a pause between them, nothing is written to the database. Use it
after a ban instead of restarting the parser: a real run would immediately
send dozens of requests and could prolong the block.
"""
import time

import requests
from django.core.management.base import BaseCommand
from django.db.models import Count

from jobs.linkedin.client import (
    JOB_URL, SEARCH_URL, LinkedInClient, company_page_url, is_bot_check_page,
)
from jobs.linkedin.parsers import parse_company_page, parse_job_posting, parse_search_page
from jobs.linkedin.regions import canonical_location
from jobs.models import Company, Job, ProcessingStatus, SearchQuery

PAUSE_BETWEEN_PROBES = 3  # seconds


def classify(resp, has_content):
    """Map a single response to (verdict, detail). Verdicts: OK, BLOCKED, UNCLEAR, ERROR."""
    code = resp.status_code
    if code == 200:
        if "/authwall" in resp.url or "/login" in resp.url:
            return "BLOCKED", "redirected to the sign-in wall"
        if is_bot_check_page(resp.text):
            return "BLOCKED", "anti-bot page (HTTP 200)"
        if has_content(resp.text):
            return "OK", "real content received"
        return "UNCLEAR", "HTTP 200 but no data parsed (soft block or new page layout)"
    if code in (404, 410):
        return "OK", f"HTTP {code}: this item was removed, but the endpoint answers normally"
    if code == 999:
        return "BLOCKED", "HTTP 999 (LinkedIn bot protection)"
    if code == 429:
        return "BLOCKED", "HTTP 429 (too many requests)"
    return "ERROR", f"unexpected HTTP {code}"


class Command(BaseCommand):
    help = ("Check whether LinkedIn serves this IP again: one request per endpoint "
            "(search, job, company), no retries, nothing is saved.")

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=["search", "job", "company"], action="append",
                            help="Probe only these endpoints (can be repeated)")

    def handle(self, *args, **o):
        client = LinkedInClient()
        probes = self._probes()
        wanted = o["only"] or list(probes)
        results = {}
        for i, name in enumerate(wanted):
            target = probes[name]
            if target is None:
                self.stdout.write(f"{name:8} SKIPPED  nothing in the database to probe")
                continue
            if i:
                time.sleep(PAUSE_BETWEEN_PROBES)
            url, params, has_content, label = target
            started = time.monotonic()
            try:
                resp = client.session.get(url, params=params, timeout=client.conf["TIMEOUT"])
                resp.encoding = "utf-8"
                verdict, detail = classify(resp, has_content)
            except requests.RequestException as exc:
                verdict, detail = "ERROR", f"network error: {exc}"
            results[name] = verdict
            style = {"OK": self.style.SUCCESS, "BLOCKED": self.style.ERROR}.get(verdict, self.style.WARNING)
            self.stdout.write(style(
                f"{name:8} {verdict:8} {detail} ({label}, {time.monotonic() - started:.1f}s)"))
        self._advice(results)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _probes():
        q = SearchQuery.objects.filter(is_active=True).order_by("pk").first()
        keywords = q.keywords if q else "Python Developer"
        location = canonical_location(q.location) if q else "United States"

        job_id = Job.objects.order_by("-first_seen_at").values_list("job_id", flat=True).first()
        # the company Details would fetch next (highest priority); fall back to any company
        slug = (Company.objects.filter(details_status=ProcessingStatus.NEW)
                .annotate(n=Count("jobs")).order_by("-n", "pk")
                .values_list("slug", flat=True).first()
                or Company.objects.values_list("slug", flat=True).first())
        return {
            "search": (SEARCH_URL, {"keywords": keywords, "location": location, "start": 0},
                       lambda html: bool(parse_search_page(html)), f"'{keywords}' / {location}"),
            "job": (JOB_URL.format(job_id=job_id), None,
                    lambda html: bool(parse_job_posting(html).get("title")), f"job {job_id}")
            if job_id else None,
            "company": (company_page_url(slug), None,
                        lambda html: bool(parse_company_page(html).get("name")), f"company {slug}")
            if slug else None,
        }

    def _advice(self, results):
        if not results:
            return
        blocked = [n for n, v in results.items() if v != "OK"]
        if not blocked:
            self.stdout.write(self.style.SUCCESS(
                "\nAccess looks restored. Continue with: python manage.py run_details --companies-only"))
            return
        ok = [n for n, v in results.items() if v == "OK"]
        self.stdout.write(self.style.WARNING(
            f"\nNot served yet: {', '.join(blocked)}"
            + (f" (working: {', '.join(ok)})" if ok else "")
            + ".\nWait a few hours before the next check: every check is a request too, and "
              "probing often can prolong the block."))
