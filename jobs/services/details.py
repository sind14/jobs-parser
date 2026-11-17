"""
Stage 2 — Details.

Takes jobs (and companies) stored by the Search stage that are not processed
yet, downloads the full job posting and the public company page, and updates
the records.

* Independent from the Search stage: it only reads rows from PostgreSQL, so it
  can run at the same time as Search, after it, or in several processes.
* Work is claimed with SELECT ... FOR UPDATE SKIP LOCKED, so parallel
  processes never take the same record.
* Every record has a status (new / processing / done / failed / not_found)
  and an attempt counter — failed records are retried on the next run,
  records stuck in "processing" (crashed worker) are released after a timeout.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from jobs.linkedin.client import (
    JOB_URL, Blocked, LinkedInClient, LinkedInError, NotFound, company_page_url,
)
from jobs.linkedin.parsers import company_slug_from_url, parse_company_page, parse_job_posting
from jobs.models import Company, Job, ProcessingStatus as S, RawPage
from jobs.services.search import alive_queries

logger = logging.getLogger("jobs.details")

STUCK_AFTER = timedelta(minutes=30)
JOB_FIELDS = [
    "description_html", "description", "seniority_level", "employment_type", "job_function",
    "industries", "applicants", "posted_time_ago", "apply_url", "salary", "location",
]
COMPANY_FIELDS = [
    "name", "website", "industry", "company_size", "employees_on_linkedin", "followers",
    "headquarters", "street_address", "city", "region", "postal_code", "country",
    "organization_type", "founded", "specialties", "description", "slogan", "logo_url",
]


def _truncate(model, field, value):
    max_len = model._meta.get_field(field).max_length
    if isinstance(value, str) and max_len:
        return value[:max_len]
    return value


def apply_job_data(job, data):
    """
    Copy parsed job-posting data onto a Job (no save). Returns False if the page
    looks empty / has an unknown layout. Shared by Details and `reparse`.
    """
    if not data.get("title") and not data.get("description"):
        return False
    for f in JOB_FIELDS:
        if data.get(f):
            setattr(job, f, _truncate(Job, f, data[f]))
    job.is_easy_apply = data.get("is_easy_apply")
    job.is_closed = data.get("is_closed", False)
    if data.get("title"):
        job.title = data["title"][:500]

    # job card may have had no company link — take it from the detail page
    slug = company_slug_from_url(data.get("company_url", ""))
    if slug and (job.company_id is None or job.company.slug != slug):
        company, _ = Company.objects.get_or_create(
            slug=slug,
            defaults={"linkedin_url": company_page_url(slug),
                      "name": data.get("company_name", "")[:500]},
        )
        job.company = company
    if data.get("company_name") and not job.company_name:
        job.company_name = data["company_name"][:500]
    return True


def apply_company_data(company, data):
    """Copy parsed company-page data onto a Company (no save). False if page is empty."""
    if not data.get("name"):
        return False
    for f in COMPANY_FIELDS:
        if data.get(f) not in (None, ""):
            setattr(company, f, _truncate(Company, f, data[f]))
    return True


class DetailsRunner:
    def __init__(self, workers=None, limit=None, client=None, query_id=None,
                 jobs=True, companies=True, batch_size=50, follow=False):
        conf = settings.LINKEDIN
        self.workers = workers or conf["DETAILS_WORKERS"]
        self.max_attempts = conf["DETAILS_MAX_ATTEMPTS"]
        self.store_raw = conf["STORE_RAW_HTML"]
        self.limit = limit
        self.client = client or LinkedInClient()
        self.query_id = query_id
        self.do_jobs = jobs
        self.do_companies = companies
        self.batch_size = batch_size
        self.follow = follow  # keep waiting for new records while Search is running
        self.stats = {"jobs_done": 0, "jobs_failed": 0, "jobs_not_found": 0,
                      "companies_done": 0, "companies_failed": 0, "companies_not_found": 0}
        self._lock = threading.Lock()
        self._processed = 0
        # model classes whose endpoint is currently blocked by LinkedIn (circuit breaker)
        self.blocked = set()

    # ------------------------------------------------------------ public API
    def run(self):
        while True:
            self._release_stuck()
            if self.do_jobs:
                self._run_pool(Job, self._process_job)
            if self.do_companies:
                self._run_pool(Company, self._process_company)
            if self.blocked or not (self.follow and self._search_running()):
                break
            logger.info("queue empty, Search is still running — waiting for new records")
            time.sleep(30)
        logger.info("details finished: %s (HTTP requests: %s)", self.stats, self.client.requests_made)
        for model in self.blocked:
            logger.error("%s: stopped — LinkedIn keeps blocking these pages from this IP. "
                         "Unprocessed records stay in the queue; re-run later to continue.",
                         model.__name__)
        return self.stats

    # ------------------------------------------------------------ queue
    @staticmethod
    def _search_running():
        # only a Search that is really alive; a crashed one left in RUNNING state
        # would otherwise make --follow wait forever
        return alive_queries().exists()

    def _release_stuck(self):
        border = timezone.now() - STUCK_AFTER
        for model in (Job, Company):
            n = model.objects.filter(
                details_status=S.PROCESSING, details_locked_at__lt=border
            ).update(details_status=S.NEW)
            if n:
                logger.info("released %s stuck %s records", n, model.__name__)

    def _pending_qs(self, model):
        qs = model.objects.filter(
            Q(details_status=S.NEW)
            | Q(details_status=S.FAILED, details_attempts__lt=self.max_attempts)
        )
        if self.query_id:
            if model is Job:
                qs = qs.filter(search_queries=self.query_id)
            else:
                qs = qs.filter(jobs__search_queries=self.query_id).distinct()
        elif model is Company:
            qs = qs.filter(jobs__isnull=False).distinct()
        return qs

    def _candidates(self, model, size):
        """
        Pending pks in processing order.

        Companies go by priority: the more jobs a company has, the earlier it is
        fetched (top ~300 employers cover ~60% of all jobs), so the export gets
        useful company data long before the slow company stage finishes.
        """
        qs = self._pending_qs(model)
        if model is Company:
            qs = (Company.objects.filter(pk__in=qs.values("pk"))
                  .annotate(n_jobs=Count("jobs")).order_by("-n_jobs", "pk"))
        else:
            qs = qs.order_by("pk")
        return list(qs.values_list("pk", flat=True)[: size * 3])

    def _claim(self, model, size):
        """Atomically take a batch of records for this process."""
        candidates = self._candidates(model, size)
        if not candidates:
            return []
        with transaction.atomic():
            # FOR UPDATE can't be combined with GROUP BY, so lock by pk and keep
            # the priority order from the candidate list
            locked = set(
                model.objects.select_for_update(skip_locked=True)
                # re-check status: another process may have claimed it meanwhile
                .filter(pk__in=candidates, details_status__in=[S.NEW, S.FAILED])
                .values_list("pk", flat=True)
            )
            ids = [pk for pk in candidates if pk in locked][:size]
            if ids:
                model.objects.filter(pk__in=ids).update(
                    details_status=S.PROCESSING, details_locked_at=timezone.now()
                )
        return ids

    def _limit_left(self):
        if self.limit is None:
            return self.batch_size
        with self._lock:
            return max(0, min(self.batch_size, self.limit - self._processed))

    def _run_pool(self, model, handler):
        name = model.__name__
        self._processed = 0  # --limit applies per record type
        total = self._pending_qs(model).count()
        logger.info("%s: %s records to process with %s workers", name, total, self.workers)
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=name) as pool:
            while model not in self.blocked:
                size = self._limit_left()
                if size <= 0:
                    break
                ids = self._claim(model, size)
                if not ids:
                    break
                list(pool.map(self._wrap(handler), ids))
                done += len(ids)
                with self._lock:
                    self._processed += len(ids)
                logger.info("%s: processed %s (queue at start: %s) | rate %.3f req/s | %s",
                            name, done, total,
                            self.client.limiters["company" if model is Company else "default"].rate,
                            self.stats)

    def _wrap(self, handler):
        def inner(pk):
            close_old_connections()
            model = Job if handler == self._process_job else Company
            try:
                if model in self.blocked:
                    self._requeue(model, pk)  # rest of the batch: back to the queue untouched
                    return
                handler(pk)
            except Blocked as exc:
                with self._lock:
                    first = model not in self.blocked
                    self.blocked.add(model)
                if first:
                    logger.error("%s: circuit breaker tripped: %s", model.__name__, exc)
                self._requeue(model, pk)
            except Exception:  # noqa: BLE001 — never kill the pool
                logger.exception("unexpected error for pk=%s", pk)
            finally:
                connection.close()
        return inner

    @staticmethod
    def _requeue(model, pk):
        """Return a claimed record to the queue without counting an attempt."""
        model.objects.filter(pk=pk, details_status=S.PROCESSING).update(
            details_status=S.NEW, details_locked_at=None
        )

    def _inc(self, key):
        with self._lock:
            self.stats[key] += 1

    # ------------------------------------------------------------ handlers
    def _process_job(self, pk):
        job = Job.objects.get(pk=pk)
        try:
            html = self.client.job_posting(job.job_id)
        except Blocked:
            raise  # handled by the pool: record goes back to the queue
        except NotFound as exc:
            self._fail(job, exc, S.NOT_FOUND)
            self._inc("jobs_not_found")
            return
        except LinkedInError as exc:
            self._fail(job, exc, S.FAILED)
            self._inc("jobs_failed")
            return
        # store before parsing: even if the parser fails, the page can be re-parsed offline
        if self.store_raw:
            RawPage.store(RawPage.Kind.JOB, job.job_id, JOB_URL.format(job_id=job.job_id), html)
        if not apply_job_data(job, parse_job_posting(html)):
            self._fail(job, "empty/unknown page layout", S.FAILED)
            self._inc("jobs_failed")
            return
        job.details_status = S.DONE
        job.details_attempts += 1
        job.details_error = ""
        job.details_fetched_at = timezone.now()
        job.save()
        self._inc("jobs_done")

    def _process_company(self, pk):
        company = Company.objects.get(pk=pk)
        try:
            html = self.client.company_page(company.slug)
        except Blocked:
            raise  # handled by the pool: record goes back to the queue
        except NotFound as exc:
            self._fail(company, exc, S.NOT_FOUND)
            self._inc("companies_not_found")
            return
        except LinkedInError as exc:
            self._fail(company, exc, S.FAILED)
            self._inc("companies_failed")
            return
        if self.store_raw:
            RawPage.store(RawPage.Kind.COMPANY, company.slug, company_page_url(company.slug), html)
        if not apply_company_data(company, parse_company_page(html)):
            self._fail(company, "empty/unknown page layout", S.FAILED)
            self._inc("companies_failed")
            return
        company.details_status = S.DONE
        company.details_attempts += 1
        company.details_error = ""
        company.details_fetched_at = timezone.now()
        company.save()
        self._inc("companies_done")

    @staticmethod
    def _fail(obj, exc, status):
        obj.details_attempts += 1
        obj.details_status = status
        obj.details_error = str(exc)[:2000]
        obj.save(update_fields=["details_attempts", "details_status", "details_error"])
        logger.warning("%s %s -> %s: %s", obj.__class__.__name__, obj.pk, status, exc)
