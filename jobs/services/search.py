"""
Stage 1 — Search.

Walks the guest search endpoint page by page and stores job cards
(job_id, title, company, location, date...) in PostgreSQL.

LinkedIn returns at most ~1000 results for one search, so a query is
processed as a tree of *slices*:

    base slice (country)  ->  if saturated, split by region of the country
                              (location="California, United States", ...)
    region slice          ->  if still saturated, split by date posted
                              (f_TPR = last 24h / week / month)

NB: the guest endpoint silently ignores f_E / f_JT / f_WT facets (checked:
identical result sets), so only `location` and `f_TPR` are usable for
splitting. Region slices partition the country; f_TPR windows overlap but
surface the freshest jobs beyond the cap.

Every slice keeps its pagination cursor in the DB, so the stage can be
stopped at any moment and resumed later without re-downloading pages.
Duplicates are impossible: job_id is UNIQUE and inserts use
bulk_create(ignore_conflicts=True).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from jobs.linkedin.client import (
    Blocked, LinkedInClient, LinkedInError, PaginationEnd, company_page_url,
)
from jobs.linkedin.parsers import company_slug_from_url, parse_search_page
from jobs.linkedin.regions import canonical_location, regions_for
from jobs.models import Company, Job, SearchQuery, SearchSlice

logger = logging.getLogger("jobs.search")

PAGE_SIZE = 10
TIME_WINDOWS = ["r86400", "r604800", "r2592000"]  # 24h / week / month


# A query in RUNNING state whose run shows no progress for this long is
# considered dead (crashed / killed process) and may be resumed.
STALE_AFTER = timedelta(minutes=30)


def filters_key(filters: dict) -> str:
    return "&".join(f"{k}={filters[k]}" for k in sorted(filters))


def alive_queries():
    """Queries that are really being processed right now (RUNNING + recent progress)."""
    border = timezone.now() - STALE_AFTER
    return SearchQuery.objects.filter(status=SearchQuery.Status.RUNNING).filter(
        Q(updated_at__gte=border) | Q(slices__updated_at__gte=border)
    ).distinct()


def plan_run(q, restart=False, explicit=False, include_done=False):
    """
    Decide what `run_search` should do with a query.
    Returns (run: bool, restart: bool, reason: str).

    * unfinished work (pending / interrupted / failed slices) -> resume from the cursors;
    * job limit reached -> skip until the limit is raised;
    * fully finished -> full refresh only when asked explicitly (--query-id / --include-done);
    * already running in another process -> skip (two runs would fetch the same pages).
    """
    if alive_queries().filter(pk=q.pk).exists():
        return False, False, "already running in another process"
    if restart:
        return True, True, ""
    if q.max_jobs and q.jobs_found >= q.max_jobs:
        return False, False, f"job limit {q.max_jobs} reached — raise it to continue"
    unfinished = q.slices.exclude(
        status__in=[SearchSlice.Status.DONE, SearchSlice.Status.SPLIT]).exists()
    if unfinished or not q.slices.exists():
        return True, False, ""
    if explicit or include_done:
        return True, True, ""
    return False, False, "finished — use --query-id or --include-done to refresh"


class SearchRunner:
    def __init__(self, query: SearchQuery, client: LinkedInClient | None = None, stdout=None):
        self.query = query
        self.client = client or LinkedInClient()
        self.conf = settings.LINKEDIN
        self.out = stdout

    def log(self, msg, *args):
        logger.info("[q%s] " + msg, self.query.pk, *args)

    # ------------------------------------------------------------ public API
    def run(self, restart=False):
        q = self.query
        if restart:
            q.slices.all().delete()
        self._ensure_base_slice()

        q.status = SearchQuery.Status.RUNNING
        q.started_at = timezone.now()
        q.finished_at = None
        q.last_error = ""
        q.save(update_fields=["status", "started_at", "finished_at", "last_error", "updated_at"])
        self._req_snapshot = self.client.requests_made

        # slices left in RUNNING state after a crash are resumed from their cursor;
        # slices that failed in a previous run get a fresh set of attempts
        SearchSlice.objects.filter(query=q, status=SearchSlice.Status.RUNNING).update(
            status=SearchSlice.Status.PENDING
        )
        SearchSlice.objects.filter(query=q, status=SearchSlice.Status.FAILED).update(
            status=SearchSlice.Status.PENDING, attempts=0
        )
        failed = 0
        blocked = False
        try:
            while True:
                if q.max_jobs and self._jobs_count() >= q.max_jobs:
                    self.log("max_jobs=%s reached", q.max_jobs)
                    break
                sl = (
                    SearchSlice.objects.filter(query=q, status=SearchSlice.Status.PENDING)
                    .order_by("depth", "id")
                    .first()
                )
                if sl is None:
                    break
                try:
                    self._process_slice(sl)
                except Blocked as exc:
                    # IP is banned for now: keep the slice (and its cursor) for the next run
                    sl.status = SearchSlice.Status.PENDING
                    sl.last_error = str(exc)
                    sl.save()
                    logger.error("[q%s] stopped: %s", q.pk, exc)
                    blocked = True
                    break
                except LinkedInError as exc:
                    failed += 1
                    sl.attempts += 1
                    sl.last_error = str(exc)
                    sl.status = (
                        SearchSlice.Status.FAILED if sl.attempts >= 3 else SearchSlice.Status.PENDING
                    )
                    sl.save()
                    logger.error("[q%s] slice %s failed: %s", q.pk, sl, exc)
                finally:
                    self._update_query_stats()
        except KeyboardInterrupt:
            q.status = SearchQuery.Status.NEW
            q.last_error = "Перервано користувачем — запустіть повторно, щоб продовжити"
            q.save()
            raise
        except Exception as exc:  # noqa: BLE001 — store any unexpected error
            q.status = SearchQuery.Status.FAILED
            q.last_error = repr(exc)
            q.save()
            raise

        q.refresh_from_db()
        left_failed = q.slices.filter(status=SearchSlice.Status.FAILED).count()
        pending_left = q.slices.filter(status=SearchSlice.Status.PENDING).exists()
        if blocked:
            q.status = SearchQuery.Status.FAILED
            q.last_error = ("LinkedIn тимчасово блокує запити з цієї IP — пошук зупинено. "
                            "Запустіть пізніше: він продовжиться з місця зупинки.")
        elif left_failed:
            q.status = SearchQuery.Status.FAILED
            q.last_error = f"Зрізів з помилкою: {left_failed} — запустіть повторно"
        elif pending_left:
            # stopped by max_jobs: not finished, the rest of the slices wait in the queue
            q.status = SearchQuery.Status.LIMIT
        else:
            q.status = SearchQuery.Status.DONE
        q.finished_at = timezone.now()
        q.save()
        self._update_query_stats()
        self.log("finished: %s unique jobs", q.jobs_found)
        return q.jobs_found

    # ------------------------------------------------------------ internals
    def _jobs_count(self):
        return Job.objects.filter(search_queries=self.query).count()

    def _update_query_stats(self):
        q = self.query
        delta = self.client.requests_made - self._req_snapshot
        self._req_snapshot = self.client.requests_made
        q.jobs_found = self._jobs_count()
        q.requests_made = (q.requests_made or 0) + delta
        SearchQuery.objects.filter(pk=q.pk).update(
            jobs_found=q.jobs_found, requests_made=q.requests_made
        )

    def _ensure_base_slice(self):
        base = {}
        if self.query.time_posted:
            base["f_TPR"] = self.query.time_posted
        SearchSlice.objects.get_or_create(
            query=self.query, filters_key=filters_key(base), defaults={"filters": base, "depth": 0}
        )

    def _process_slice(self, sl: SearchSlice):
        q = self.query
        sl.status = SearchSlice.Status.RUNNING
        sl.save(update_fields=["status", "updated_at"])
        self.log("slice [%s] start=%s", sl.filters_key or "base", sl.next_start)

        while sl.next_start <= self.conf["MAX_START"]:
            try:
                html = self.client.search(
                    q.keywords, canonical_location(q.location), start=sl.next_start, geo_id=q.geo_id, extra=sl.filters
                )
            except PaginationEnd:
                break
            cards = parse_search_page(html)
            if not cards:
                break
            new = self._save_cards(cards)
            sl.results_seen += len(cards)
            sl.new_jobs += new
            sl.next_start += PAGE_SIZE
            sl.save(update_fields=["results_seen", "new_jobs", "next_start", "updated_at"])
            if q.max_jobs and self._jobs_count() >= q.max_jobs:
                sl.status = SearchSlice.Status.PENDING  # resumable later
                sl.save(update_fields=["status", "updated_at"])
                return

        saturated = sl.results_seen >= self.conf["SLICE_SPLIT_THRESHOLD"]
        children = self._split(sl) if saturated else []
        sl.status = SearchSlice.Status.SPLIT if children else SearchSlice.Status.DONE
        sl.save(update_fields=["status", "updated_at"])
        self.log(
            "slice [%s] done: %s cards, %s new%s",
            sl.filters_key or "base", sl.results_seen, sl.new_jobs,
            f", split into {len(children)}" if children else "",
        )

    def _split(self, sl: SearchSlice):
        """Create child slices: first by region, then by date-posted window."""
        children = []
        if "location" not in sl.filters:
            children = [dict(sl.filters, location=r)
                        for r in regions_for(self.query.location, self.query.regions)]
        if not children and "f_TPR" not in sl.filters:
            children = [dict(sl.filters, f_TPR=w) for w in TIME_WINDOWS]
        created = []
        for f in children:
            obj, is_new = SearchSlice.objects.get_or_create(
                query=sl.query, filters_key=filters_key(f),
                defaults={"filters": f, "depth": sl.depth + 1},
            )
            if is_new:
                created.append(obj)
        return created

    @transaction.atomic
    def _save_cards(self, cards):
        """Upsert companies and jobs; returns number of newly inserted jobs."""
        # --- companies (unique by slug)
        slugs = {}
        for c in cards:
            slug = company_slug_from_url(c["company_url"])
            c["company_slug"] = slug
            if slug and slug not in slugs:
                slugs[slug] = Company(
                    slug=slug,
                    linkedin_url=company_page_url(slug),
                    name=c["company_name"],
                    logo_url=c["company_logo"][:1000],
                )
        if slugs:
            Company.objects.bulk_create(slugs.values(), ignore_conflicts=True)
        company_ids = dict(Company.objects.filter(slug__in=slugs).values_list("slug", "id"))

        # --- jobs (unique by job_id)
        ids = [c["job_id"] for c in cards]
        existing = set(Job.objects.filter(job_id__in=ids).values_list("job_id", flat=True))
        new_jobs = [
            Job(
                job_id=c["job_id"],
                url=c["url"][:500],
                title=c["title"][:500],
                company_id=company_ids.get(c["company_slug"]),
                company_name=c["company_name"][:500],
                location=c["location"][:255],
                posted_date=c["posted_date"],
                benefits=c["benefits"][:255],
                salary=c["salary"][:255],
            )
            for c in cards
            if c["job_id"] not in existing
        ]
        Job.objects.bulk_create(new_jobs, ignore_conflicts=True)
        # refresh "last seen" for already known jobs
        if existing:
            Job.objects.filter(job_id__in=existing).update(last_seen_at=timezone.now())

        # --- link jobs to this search query (M2M, no duplicates)
        through = Job.search_queries.through
        pk_map = dict(Job.objects.filter(job_id__in=ids).values_list("job_id", "id"))
        through.objects.bulk_create(
            [through(job_id=pk_map[i], searchquery_id=self.query.pk) for i in ids if i in pk_map],
            ignore_conflicts=True,
        )
        return len(ids) - len(existing)
