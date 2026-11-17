"""
Thin HTTP client for LinkedIn public (guest) endpoints.

Endpoints were found via DevTools -> Network on the public jobs page
(https://www.linkedin.com/jobs/search?keywords=...&location=...):

* Search (infinite scroll "See more jobs"):
  GET /jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&start=
  -> HTML fragment with 10 job cards per page.
* Job details (right pane when a card is clicked):
  GET /jobs-guest/jobs/api/jobPosting/{job_id}
* Company: public company page /company/{slug} (JSON-LD + "About us" block).
"""
import logging
import random
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger("jobs.linkedin")

BASE = "https://www.linkedin.com"
SEARCH_URL = BASE + "/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_URL = BASE + "/jobs-guest/jobs/api/jobPosting/{job_id}"
COMPANY_URL = BASE + "/company/{slug}"


def company_page_url(slug):
    """
    Public page of an employer. Plain slugs are /company/ pages; universities and
    showcase pages are stored with their section prefix ("school/asu").
    """
    return f"{BASE}/{slug}" if "/" in slug else COMPANY_URL.format(slug=slug)


def is_bot_check_page(text):
    """
    LinkedIn sometimes answers HTTP 200 with an anti-bot interstitial (obfuscated JS +
    <meta http-equiv="refresh" ... url=https://www.linkedin.com>) instead of content.
    It must be treated as a block, not as an empty result — otherwise Search would
    take it for "end of results" and silently cut a slice short.
    """
    head = (text or "")[:3000].lower()
    return 'http-equiv="refresh"' in head and "url=https://www.linkedin.com" in head

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


class LinkedInError(Exception):
    """Retryable error (network, throttling, unexpected answer)."""


class NotFound(LinkedInError):
    """Resource does not exist anymore (404/410) — not retryable."""


class PaginationEnd(LinkedInError):
    """Search endpoint refused the offset (HTTP 400) — end of results."""


class Blocked(LinkedInError):
    """
    The endpoint keeps answering 429/999 even at the minimal rate: the IP is
    temporarily banned. Continuing only produces useless requests, so the
    caller should stop and retry later (records are not marked as failed).
    """


class AdaptiveRateLimiter:
    """
    Process-wide limiter shared by all worker threads (AIMD).

    * every request waits for its slot: interval = 1 / rate (with jitter);
    * on success the rate grows additively (+step), up to max_rate;
    * on 429/999 the rate is halved and *all* threads pause (cooldown),
      instead of each thread hammering the server independently.
    """

    def __init__(self, rate, min_rate=0.2, max_rate=None, step=0.02):
        self.rate = rate
        self.min_rate = min_rate
        self.max_rate = max_rate or rate * 2
        self.step = step
        self.consecutive_blocks = 0
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + random.uniform(0.7, 1.3) / self.rate
        if slot > now:
            time.sleep(slot - now)

    def success(self):
        with self._lock:
            self.consecutive_blocks = 0
            self.rate = min(self.max_rate, self.rate + self.step)

    def throttled(self, cooldown):
        with self._lock:
            self.consecutive_blocks += 1
            self.rate = max(self.min_rate, self.rate / 2)
            self._next = max(self._next, time.monotonic() + cooldown)
            return self.rate


class LinkedInClient:
    """Thread-safe client: one requests.Session per thread."""

    def __init__(self, conf=None):
        self.conf = conf or settings.LINKEDIN
        self._local = threading.local()
        self.requests_made = 0
        self._lock = threading.Lock()
        # Separate limiters: LinkedIn tolerates far fewer company-page requests
        # than job requests, so one shared rate would either waste requests on
        # company pages or needlessly slow down jobs.
        self.limiters = {
            "default": AdaptiveRateLimiter(
                rate=self.conf["RATE"], max_rate=self.conf["RATE_MAX"],
                min_rate=self.conf["RATE_MIN"],
            ),
            "company": AdaptiveRateLimiter(
                rate=self.conf["COMPANY_RATE"], max_rate=self.conf["COMPANY_RATE_MAX"],
                min_rate=self.conf["COMPANY_RATE_MIN"], step=self.conf["COMPANY_RATE_STEP"],
            ),
        }
        self.limiter = self.limiters["default"]

    # ------------------------------------------------------------------ utils
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            proxies = self.conf["PROXIES"]
            if proxies:
                p = random.choice(proxies)
                s.proxies = {"http": p, "https": p}
            self._local.session = s
        return s

    def _reset_session(self):
        self._local.session = None

    def get(self, url, params=None, kind="default") -> requests.Response:
        """GET with rate limiting, retries and exponential backoff."""
        limiter = self.limiters[kind]
        max_retries = self.conf["MAX_RETRIES"]
        last_exc = None
        for attempt in range(1, max_retries + 1):
            limiter.acquire()
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.conf["TIMEOUT"], allow_redirects=True
                )
            except requests.RequestException as exc:
                last_exc = LinkedInError(f"network error: {exc}")
                logger.warning("GET %s failed (%s), attempt %s/%s", url, exc, attempt, max_retries)
                time.sleep(min(2 ** attempt, 60))
                continue
            with self._lock:
                self.requests_made += 1

            code = resp.status_code
            resp.encoding = "utf-8"
            if code == 200:
                block_reason = None
                # LinkedIn sometimes redirects guests to the auth wall
                if "/authwall" in resp.url or "/login" in resp.url:
                    block_reason = "redirected to authwall"
                    self._reset_session()
                elif is_bot_check_page(resp.text):
                    block_reason = "anti-bot page (HTTP 200)"
                if block_reason is None:
                    limiter.success()
                    return resp
                last_exc = LinkedInError(block_reason)
                self._throttled(limiter, attempt, block_reason)
                self._check_breaker(limiter, kind)
                continue
            if code in (404, 410):
                raise NotFound(f"HTTP {code} for {url}")
            if code == 400 and url == SEARCH_URL:
                raise PaginationEnd("HTTP 400 — pagination limit reached")
            if code in (429, 999):
                last_exc = LinkedInError(f"HTTP {code}")
                # keep the session (and its cookies) like a normal browser would;
                # just slow down for this endpoint type
                self._throttled(limiter, attempt, f"HTTP {code}")
                self._check_breaker(limiter, kind)
                continue
            if code >= 500:
                last_exc = LinkedInError(f"HTTP {code}")
                self._backoff(attempt, f"HTTP {code}")
                continue
            last_exc = LinkedInError(f"unexpected HTTP {code}")
            self._backoff(attempt, f"HTTP {code}")
        raise last_exc or LinkedInError("unknown error")

    def _check_breaker(self, limiter, kind):
        """Circuit breaker: blocked again and again — stop instead of hammering."""
        if limiter.consecutive_blocks >= self.conf["MAX_CONSECUTIVE_BLOCKS"]:
            raise Blocked(
                f"{limiter.consecutive_blocks} blocked requests in a row ({kind}) — "
                "LinkedIn is blocking this IP, retry later"
            )

    def _throttled(self, limiter, attempt, reason):
        """Server asked us to slow down: shared cooldown + lower rate for all threads."""
        pause = min(self.conf["BACKOFF_BASE"] * (2 ** (attempt - 1)), 300) + random.uniform(0, 3)
        rate = limiter.throttled(pause)
        logger.warning("%s — global pause %.0fs, rate -> %.3f req/s (attempt %s)",
                       reason, pause, rate, attempt)
        time.sleep(pause)

    def _backoff(self, attempt, reason):
        pause = self.conf["BACKOFF_BASE"] * (2 ** (attempt - 1)) + random.uniform(0, 5)
        pause = min(pause, 300)
        logger.warning("%s — sleeping %.0fs (attempt %s)", reason, pause, attempt)
        time.sleep(pause)

    # ---------------------------------------------------------------- endpoints
    def search(self, keywords, location, start=0, geo_id="", extra=None) -> str:
        params = {"keywords": keywords, "location": location, "start": start, "sortBy": "DD"}
        if geo_id and not (extra and "location" in extra):
            params["geoId"] = geo_id
        if extra:
            params.update(extra)  # slice filters (e.g. region location, f_TPR)
        return self.get(SEARCH_URL, params=params).text

    def job_posting(self, job_id) -> str:
        return self.get(JOB_URL.format(job_id=job_id)).text

    def company_page(self, slug) -> str:
        return self.get(company_page_url(slug), kind="company").text
