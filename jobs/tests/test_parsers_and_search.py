from datetime import date
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from jobs.linkedin.client import PaginationEnd
from jobs.linkedin.parsers import (
    company_slug_from_url, parse_company_page, parse_job_posting, parse_search_page,
)
from jobs.models import Company, Job, SearchQuery, SearchSlice
from jobs.services.search import SearchRunner

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class ParserTests(SimpleTestCase):
    def test_search_page(self):
        cards = parse_search_page(fixture("search_page.html"))
        self.assertEqual(len(cards), 10)
        c = cards[0]
        self.assertEqual(c["job_id"], 4451909310)
        self.assertEqual(c["title"], "Python Django Developer")
        self.assertEqual(c["company_name"], "Tata Consultancy Services")
        self.assertEqual(company_slug_from_url(c["company_url"]), "tata-consultancy-services")
        self.assertEqual(c["posted_date"], date(2026, 11, 12))
        self.assertNotIn("?", c["url"])

    def test_job_posting(self):
        d = parse_job_posting(fixture("job_posting.html"))
        self.assertEqual(d["company_name"], "JTEC Consulting LLC")
        self.assertEqual(d["employment_type"], "Full-time")
        self.assertEqual(d["salary"], "$160,000.00/yr - $180,000.00/yr")
        self.assertFalse(d["is_easy_apply"])
        self.assertTrue(len(d["description"]) > 200)

    def test_company_page(self):
        d = parse_company_page(fixture("company_page.html"))
        self.assertEqual(d["name"], "Tata Consultancy Services")
        self.assertEqual(d["company_size"], "10,001+ employees")
        self.assertEqual(d["founded"], "1968")
        self.assertEqual(d["country"], "IN")
        self.assertGreater(d["employees_on_linkedin"], 100000)
        self.assertGreater(d["followers"], 1000000)

    def test_empty_page(self):
        self.assertEqual(parse_search_page("<!DOCTYPE html>"), [])


class FakeClient:
    """Returns the same fixture page N times, then signals end of pagination."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.requests_made = 0

    def search(self, keywords, location, start=0, geo_id="", extra=None):
        self.calls.append((start, dict(extra or {})))
        self.requests_made += 1
        if start // 10 >= self.pages:
            raise PaginationEnd()
        return fixture("search_page.html")


class SearchRunnerTests(TestCase):
    def test_dedup_and_resume(self):
        q = SearchQuery.objects.create(keywords="Python Developer", location="United States")
        client = FakeClient(pages=3)
        SearchRunner(q, client=client).run()
        # 3 pages with identical cards -> still only 10 unique jobs
        self.assertEqual(Job.objects.count(), 10)
        self.assertEqual(Company.objects.filter(slug="tata-consultancy-services").count(), 1)
        q.refresh_from_db()
        self.assertEqual(q.status, SearchQuery.Status.DONE)
        self.assertEqual(q.jobs_found, 10)

        # Running again does nothing (slices done), no duplicates
        SearchRunner(q, client=FakeClient(pages=3)).run()
        self.assertEqual(Job.objects.count(), 10)

    def test_saturated_slice_is_split(self):
        q = SearchQuery.objects.create(keywords="x", location="USA")
        with self.settings(LINKEDIN={**settings.LINKEDIN, "SLICE_SPLIT_THRESHOLD": 20}):
            client = FakeClient(pages=3)
            SearchRunner(q, client=client).run()
        keys = set(SearchSlice.objects.values_list("filters_key", flat=True))
        # base -> 51 US regions -> each region -> 3 date windows
        self.assertIn("location=California, United States", keys)
        self.assertIn("f_TPR=r86400&location=Texas, United States", keys)
        self.assertEqual(SearchSlice.objects.count(), 1 + 51 + 51 * 3)
        self.assertEqual(client.calls[0][1], {})
        self.assertEqual(Job.objects.count(), 10)

    def test_unknown_country_splits_by_time_only(self):
        q = SearchQuery.objects.create(keywords="x", location="Atlantis")
        with self.settings(LINKEDIN={**settings.LINKEDIN, "SLICE_SPLIT_THRESHOLD": 20}):
            SearchRunner(q, client=FakeClient(pages=3)).run()
        keys = set(SearchSlice.objects.values_list("filters_key", flat=True))
        self.assertEqual(keys, {"", "f_TPR=r86400", "f_TPR=r604800", "f_TPR=r2592000"})

    def test_resume_from_cursor(self):
        q = SearchQuery.objects.create(keywords="x", location="y")
        SearchSlice.objects.create(query=q, filters={}, filters_key="", next_start=20,
                                   status=SearchSlice.Status.RUNNING)
        client = FakeClient(pages=3)
        SearchRunner(q, client=client).run()
        self.assertEqual(client.calls[0][0], 20)  # continued where it stopped


class DetailsTests(TransactionTestCase):
    @mock.patch("jobs.linkedin.client.LinkedInClient.company_page")
    @mock.patch("jobs.linkedin.client.LinkedInClient.job_posting")
    def test_details_updates_records(self, job_posting, company_page):
        from jobs.services.details import DetailsRunner

        job_posting.return_value = fixture("job_posting.html")
        company_page.return_value = fixture("company_page.html")
        c = Company.objects.create(slug="jtec-consulting-llc", linkedin_url="https://x")
        Job.objects.create(job_id=1, url="https://x", title="t", company=c)
        stats = DetailsRunner(workers=2).run()
        self.assertEqual(stats["jobs_done"], 1)
        self.assertEqual(stats["companies_done"], 1)
        j = Job.objects.get(job_id=1)
        self.assertEqual(j.details_status, "done")
        self.assertEqual(j.employment_type, "Full-time")
        self.assertEqual(Company.objects.get().company_size, "10,001+ employees")

    def test_companies_claimed_by_job_count(self):
        from jobs.services.details import DetailsRunner

        small = Company.objects.create(slug="small", linkedin_url="https://x")
        big = Company.objects.create(slug="big", linkedin_url="https://x")
        mid = Company.objects.create(slug="mid", linkedin_url="https://x")
        for i, (company, n) in enumerate([(small, 1), (big, 5), (mid, 3)]):
            for k in range(n):
                Job.objects.create(job_id=i * 100 + k, url="https://x", title="t", company=company)
        runner = DetailsRunner(workers=1)
        self.assertEqual(runner._claim(Company, 2), [big.pk, mid.pk])
        # already claimed records are not handed out again
        self.assertEqual(runner._claim(Company, 2), [small.pk])
        self.assertEqual(runner._claim(Company, 2), [])


class RateLimiterTests(SimpleTestCase):
    def test_aimd(self):
        from jobs.linkedin.client import AdaptiveRateLimiter

        lim = AdaptiveRateLimiter(rate=0.15, min_rate=0.03, max_rate=0.3, step=0.002)
        self.assertAlmostEqual(lim.throttled(0), 0.075)
        lim.throttled(0)
        lim.throttled(0)
        self.assertEqual(lim.throttled(0), 0.03)  # never below the floor
        lim.success()
        self.assertAlmostEqual(lim.rate, 0.032)

    def test_company_pages_use_own_limiter(self):
        from jobs.linkedin.client import LinkedInClient

        client = LinkedInClient()
        with mock.patch.object(LinkedInClient, "get") as get:
            client.company_page("acme")
            client.job_posting(1)
        self.assertEqual(get.call_args_list[0].kwargs.get("kind"), "company")
        self.assertNotIn("kind", get.call_args_list[1].kwargs)
        self.assertLess(client.limiters["company"].rate, client.limiters["default"].rate)


class CircuitBreakerTests(TransactionTestCase):
    def test_client_raises_blocked_after_n_blocks_in_a_row(self):
        from jobs.linkedin.client import Blocked, LinkedInClient

        conf = {**settings.LINKEDIN, "MAX_CONSECUTIVE_BLOCKS": 3, "MAX_RETRIES": 10,
                "BACKOFF_BASE": 0, "COMPANY_RATE": 1000, "COMPANY_RATE_MIN": 1000}
        client = LinkedInClient(conf=conf)
        resp = mock.Mock(status_code=999, url="https://www.linkedin.com/company/x")
        with mock.patch("requests.Session.get", return_value=resp) as get, \
                mock.patch("jobs.linkedin.client.time.sleep"):
            with self.assertRaises(Blocked):
                client.company_page("x")
        self.assertEqual(get.call_count, 3)  # stopped after 3, not after MAX_RETRIES=10

    @mock.patch("jobs.linkedin.client.LinkedInClient.company_page")
    def test_details_stops_and_keeps_records_in_queue(self, company_page):
        from jobs.linkedin.client import Blocked
        from jobs.services.details import DetailsRunner

        company_page.side_effect = Blocked("banned")
        for i in range(5):
            c = Company.objects.create(slug=f"c{i}", linkedin_url="https://x")
            Job.objects.create(job_id=i, url="https://x", title="t", company=c,
                               details_status="done")
        runner = DetailsRunner(workers=2, jobs=False, batch_size=2)
        stats = runner.run()
        self.assertIn(Company, runner.blocked)
        self.assertEqual(stats["companies_failed"], 0)
        # nothing marked as failed, no attempts burned, nothing stuck in "processing"
        self.assertEqual(Company.objects.filter(details_status="new", details_attempts=0).count(), 5)
        self.assertLessEqual(company_page.call_count, 2)  # stopped after the first batch


class RawHtmlTests(TransactionTestCase):
    @mock.patch("jobs.linkedin.client.LinkedInClient.company_page")
    @mock.patch("jobs.linkedin.client.LinkedInClient.job_posting")
    def test_details_stores_raw_html_and_reparse_restores_fields(self, job_posting, company_page):
        from django.core.management import call_command
        from jobs.models import RawPage
        from jobs.services.details import DetailsRunner

        job_posting.return_value = fixture("job_posting.html")
        company_page.return_value = fixture("company_page.html")
        c = Company.objects.create(slug="jtec-consulting-llc", linkedin_url="https://x")
        Job.objects.create(job_id=4461031287, url="https://x", title="t", company=c)
        DetailsRunner(workers=1).run()

        page = RawPage.objects.get(kind="job", key="4461031287")
        self.assertEqual(page.html, fixture("job_posting.html"))  # round-trips through zlib
        self.assertLess(len(bytes(page.html_gz)), page.size)     # actually compressed
        self.assertTrue(RawPage.objects.filter(kind="company", key="jtec-consulting-llc").exists())

        # simulate a parser bug that lost data: wipe fields, then re-parse offline
        Job.objects.update(employment_type="", salary="", description="")
        Company.objects.update(company_size="")
        calls_before = job_posting.call_count + company_page.call_count
        call_command("reparse", stdout=__import__("io").StringIO())
        self.assertEqual(job_posting.call_count + company_page.call_count, calls_before)  # no requests
        j = Job.objects.get()
        self.assertEqual(j.employment_type, "Full-time")
        self.assertEqual(j.salary, "$160,000.00/yr - $180,000.00/yr")
        self.assertEqual(Company.objects.get().company_size, "10,001+ employees")

    def test_store_raw_html_can_be_disabled(self):
        from jobs.models import RawPage
        from jobs.services.details import DetailsRunner

        c = Company.objects.create(slug="x", linkedin_url="https://x")
        Job.objects.create(job_id=1, url="https://x", title="t", company=c)
        with self.settings(LINKEDIN={**settings.LINKEDIN, "STORE_RAW_HTML": False}), \
                mock.patch("jobs.linkedin.client.LinkedInClient.job_posting",
                           return_value=fixture("job_posting.html")), \
                mock.patch("jobs.linkedin.client.LinkedInClient.company_page",
                           return_value=fixture("company_page.html")):
            DetailsRunner(workers=1).run()
        self.assertFalse(RawPage.objects.exists())


class ResumeLogicTests(TestCase):
    """run_search must continue unfinished work instead of skipping it or starting over."""

    def test_limit_then_resume_after_raising_limit(self):
        from jobs.services.search import plan_run

        q = SearchQuery.objects.create(keywords="x", location="Atlantis", max_jobs=10)
        SearchRunner(q, client=FakeClient(pages=3)).run()
        q.refresh_from_db()
        self.assertEqual(q.status, SearchQuery.Status.LIMIT)  # not "done": slices are left
        self.assertFalse(plan_run(q, explicit=True)[0])        # limit still reached -> skip

        q.max_jobs = None
        q.save()
        run, restart, _ = plan_run(q, explicit=True)
        self.assertEqual((run, restart), (True, False))         # resume, NOT a restart
        client = FakeClient(pages=3)
        SearchRunner(q, client=client).run(restart=restart)
        self.assertEqual(client.calls[0][0], 10)               # continued from the cursor
        q.refresh_from_db()
        self.assertEqual(q.status, SearchQuery.Status.DONE)

    def test_failed_slices_are_retried_on_next_run(self):
        q = SearchQuery.objects.create(keywords="x", location="Atlantis")
        SearchSlice.objects.create(query=q, filters={}, filters_key="", next_start=10,
                                   status=SearchSlice.Status.FAILED, attempts=3)
        client = FakeClient(pages=2)
        SearchRunner(q, client=client).run()
        self.assertEqual(client.calls[0][0], 10)
        q.refresh_from_db()
        self.assertEqual(q.status, SearchQuery.Status.DONE)

    def test_finished_query_is_refreshed_only_when_asked(self):
        from jobs.services.search import plan_run

        q = SearchQuery.objects.create(keywords="x", location="Atlantis")
        SearchRunner(q, client=FakeClient(pages=1)).run()
        q.refresh_from_db()
        self.assertEqual(plan_run(q)[:2], (False, False))
        self.assertEqual(plan_run(q, explicit=True)[:2], (True, True))
        self.assertEqual(plan_run(q, include_done=True)[:2], (True, True))

    def test_running_query_is_not_started_twice_but_a_dead_one_is_resumed(self):
        from datetime import timedelta
        from django.utils import timezone
        from jobs.services.search import alive_queries, plan_run

        q = SearchQuery.objects.create(keywords="x", location="Atlantis",
                                       status=SearchQuery.Status.RUNNING)
        SearchSlice.objects.create(query=q, filters={}, filters_key="")
        self.assertFalse(plan_run(q, explicit=True)[0])  # live run in another process

        old = timezone.now() - timedelta(hours=2)        # process died 2 hours ago
        SearchQuery.objects.filter(pk=q.pk).update(updated_at=old)
        SearchSlice.objects.filter(query=q).update(updated_at=old)
        self.assertFalse(alive_queries().exists())       # --follow won't wait for it
        self.assertEqual(plan_run(q, explicit=True)[:2], (True, False))


class LastSeenTests(TransactionTestCase):
    @mock.patch("jobs.linkedin.client.LinkedInClient.company_page")
    @mock.patch("jobs.linkedin.client.LinkedInClient.job_posting")
    def test_details_does_not_touch_last_seen_at(self, job_posting, company_page):
        from datetime import timedelta
        from django.utils import timezone
        from jobs.services.details import DetailsRunner

        job_posting.return_value = fixture("job_posting.html")
        company_page.return_value = fixture("company_page.html")
        Job.objects.create(job_id=1, url="https://x", title="t")
        seen = timezone.now() - timedelta(days=1)
        Job.objects.update(last_seen_at=seen)
        DetailsRunner(workers=1).run()
        j = Job.objects.get()
        self.assertEqual(j.details_status, "done")
        self.assertEqual(j.last_seen_at, seen)


class EmployerPagesTests(SimpleTestCase):
    def test_school_and_showcase_slugs(self):
        from jobs.linkedin.client import company_page_url

        self.assertEqual(company_slug_from_url("https://in.linkedin.com/company/Acme-Inc?trk=x"),
                         "acme-inc")
        self.assertEqual(company_slug_from_url("https://www.linkedin.com/school/arizona-state-university/"),
                         "school/arizona-state-university")
        self.assertEqual(company_slug_from_url("https://www.linkedin.com/showcase/aws-training"),
                         "showcase/aws-training")
        self.assertEqual(company_page_url("acme-inc"), "https://www.linkedin.com/company/acme-inc")
        self.assertEqual(company_page_url("school/asu"), "https://www.linkedin.com/school/asu")

    def test_university_json_ld_is_parsed(self):
        html = ('<script type="application/ld+json">{"@type": "CollegeOrUniversity", '
                '"name": "Arizona State University", "numberOfEmployees": {"value": 30000}}</script>')
        d = parse_company_page(html)
        self.assertEqual(d["name"], "Arizona State University")
        self.assertEqual(d["employees_on_linkedin"], 30000)


class AntiBotPageTests(SimpleTestCase):
    # shape of the interstitial LinkedIn returned with HTTP 200 during the research
    CHALLENGE = ('<html><head>\n    <meta http-equiv="refresh" content="1;url=https://www.linkedin.com" />'
                 '\n    <script type="text/javascript">var _0x26bb58=_0x3a5d;</script></head></html>')

    def test_detection_does_not_flag_real_pages(self):
        from jobs.linkedin.client import is_bot_check_page

        self.assertTrue(is_bot_check_page(self.CHALLENGE))
        for name in ("search_page.html", "job_posting.html", "company_page.html"):
            self.assertFalse(is_bot_check_page(fixture(name)), name)

    def test_challenge_page_is_a_block_not_a_result(self):
        from jobs.linkedin.client import Blocked, LinkedInClient

        conf = {**settings.LINKEDIN, "MAX_CONSECUTIVE_BLOCKS": 2, "MAX_RETRIES": 10,
                "BACKOFF_BASE": 0, "RATE": 1000, "RATE_MIN": 1000, "RATE_MAX": 1000}
        client = LinkedInClient(conf=conf)
        challenge = mock.Mock(status_code=200, url="https://www.linkedin.com/jobs-guest/x",
                              text=self.CHALLENGE)
        with mock.patch("requests.Session.get", return_value=challenge) as get, \
                mock.patch("jobs.linkedin.client.time.sleep"):
            with self.assertRaises(Blocked):
                client.job_posting(1)
        self.assertEqual(get.call_count, 2)

        normal = mock.Mock(status_code=200, url="https://www.linkedin.com/jobs-guest/x",
                           text=fixture("job_posting.html"))
        with mock.patch("requests.Session.get", return_value=normal), \
                mock.patch("jobs.linkedin.client.time.sleep"):
            self.assertIs(client.get("https://www.linkedin.com/jobs-guest/x"), normal)
        self.assertEqual(client.limiters["default"].consecutive_blocks, 0)


class CheckAccessTests(TestCase):
    def test_one_request_per_endpoint_no_retries_nothing_saved(self):
        import io
        from django.core.management import call_command

        c = Company.objects.create(slug="acme", linkedin_url="https://x")
        Job.objects.create(job_id=42, url="https://x", title="t", company=c)
        responses = [
            mock.Mock(status_code=200, url="https://www.linkedin.com/jobs-guest/search",
                      text=fixture("search_page.html")),
            mock.Mock(status_code=200, url="https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/42",
                      text=AntiBotPageTests.CHALLENGE),
            mock.Mock(status_code=999, url="https://www.linkedin.com/company/acme", text=""),
        ]
        out = io.StringIO()
        with mock.patch("requests.Session.get", side_effect=responses) as get, \
                mock.patch("jobs.management.commands.check_access.time.sleep"):
            call_command("check_access", stdout=out)

        self.assertEqual(get.call_count, 3)  # exactly one request per endpoint, no retries
        self.assertEqual(get.call_args_list[2].args[0], "https://www.linkedin.com/company/acme")
        text = out.getvalue()
        self.assertRegex(text, r"search\s+OK")
        self.assertRegex(text, r"job\s+BLOCKED.*anti-bot")
        self.assertRegex(text, r"company\s+BLOCKED.*999")
        self.assertIn("Not served yet: job, company", text)
        self.assertEqual(Job.objects.get().details_status, "new")       # nothing written
        self.assertEqual(Company.objects.get().details_attempts, 0)

    def test_only_option_probes_a_single_endpoint(self):
        import io
        from django.core.management import call_command

        Company.objects.create(slug="acme", linkedin_url="https://x")
        ok = mock.Mock(status_code=200, url="https://www.linkedin.com/company/acme",
                       text=fixture("company_page.html"))
        out = io.StringIO()
        with mock.patch("requests.Session.get", return_value=ok) as get:
            call_command("check_access", "--only", "company", stdout=out)
        self.assertEqual(get.call_count, 1)
        self.assertIn("Access looks restored", out.getvalue())


class EnsureAdminTests(TestCase):
    def test_creates_the_admin_once(self):
        import io
        import os
        from django.contrib.auth import get_user_model
        from django.core.management import call_command

        env = {"DJANGO_SUPERUSER_USERNAME": "demo", "DJANGO_SUPERUSER_PASSWORD": "pw-123456"}
        with mock.patch.dict(os.environ, env):
            call_command("ensure_admin", stdout=io.StringIO())
            out = io.StringIO()
            call_command("ensure_admin", stdout=out)  # second start of the container
        user = get_user_model().objects.get(username="demo")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("pw-123456"))
        self.assertIn("already exists", out.getvalue())
