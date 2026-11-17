"""HTML -> dict parsers for LinkedIn guest endpoints (pure functions, easy to test)."""
import json
import re
from datetime import date
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Comment

JOB_ID_RE = re.compile(r"jobPosting:(\d+)")
JOB_ID_URL_RE = re.compile(r"-(\d{6,})(?:\?|$)")
# employer pages: /company/<slug>, and also /school/<slug> (universities) and
# /showcase/<slug> — before, jobs posted by universities lost their employer
COMPANY_SLUG_RE = re.compile(r"/(company|school|showcase)/([^/?#]+)")
ORG_TYPES = {"Organization", "Corporation", "CollegeOrUniversity", "EducationalOrganization",
             "School"}
INT_RE = re.compile(r"[\d,\.]+")


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _soup(html):
    return BeautifulSoup(html, "lxml")


def _clean_url(url):
    """Strip tracking query string from LinkedIn URLs."""
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def company_slug_from_url(url):
    """'.../company/acme' -> 'acme'; '.../school/asu' -> 'school/asu' (no clash with companies)."""
    if not url:
        return ""
    m = COMPANY_SLUG_RE.search(url)
    if not m:
        return ""
    section, slug = m.group(1), unquote(m.group(2)).strip().lower()
    return slug if section == "company" else f"{section}/{slug}"


def _to_int(text):
    m = INT_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", "").replace(".", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- search page
def parse_search_page(html):
    """Return list of job cards from seeMoreJobPostings response."""
    soup = _soup(html)
    jobs = []
    for card in soup.select("div.base-card, div.job-search-card, li > div[data-entity-urn]"):
        urn = card.get("data-entity-urn", "")
        m = JOB_ID_RE.search(urn)
        link = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
        href = link.get("href", "") if link else ""
        if not m:
            m = JOB_ID_URL_RE.search(href.split("?")[0] + "?")
        if not m:
            continue
        job_id = int(m.group(1))
        company_link = card.select_one("h4.base-search-card__subtitle a")
        date_node = card.select_one("time")
        posted = None
        if date_node and date_node.get("datetime"):
            try:
                posted = date.fromisoformat(date_node["datetime"])
            except ValueError:
                posted = None
        logo = card.select_one("img.artdeco-entity-image")
        jobs.append(
            {
                "job_id": job_id,
                "url": _clean_url(href) or f"https://www.linkedin.com/jobs/view/{job_id}",
                "title": _text(card.select_one("h3.base-search-card__title")),
                "company_name": _text(card.select_one("h4.base-search-card__subtitle")),
                "company_url": _clean_url(company_link.get("href")) if company_link else "",
                "company_logo": (logo.get("data-delayed-url") or "") if logo else "",
                "location": _text(card.select_one(".job-search-card__location")),
                "posted_date": posted,
                "benefits": _text(card.select_one(".job-posting-benefits__text")),
                "salary": _text(card.select_one(".job-search-card__salary-info")),
            }
        )
    # de-duplicate inside one page (same card can be matched by several selectors)
    unique = {}
    for j in jobs:
        unique.setdefault(j["job_id"], j)
    return list(unique.values())


# ---------------------------------------------------------------- job posting
CRITERIA_MAP = {
    "seniority level": "seniority_level",
    "employment type": "employment_type",
    "job function": "job_function",
    "industries": "industries",
}


def parse_job_posting(html):
    soup = _soup(html)
    data = {}

    data["title"] = _text(soup.select_one(".topcard__title"))
    org = soup.select_one("a.topcard__org-name-link")
    data["company_name"] = _text(org) or _text(soup.select_one(".topcard__flavor"))
    data["company_url"] = _clean_url(org.get("href")) if org else ""

    flavors = soup.select(".topcard__flavor-row .topcard__flavor--bullet")
    data["location"] = _text(flavors[0]) if flavors else ""
    data["posted_time_ago"] = _text(soup.select_one(".posted-time-ago__text"))
    data["applicants"] = _text(
        soup.select_one(".num-applicants__caption") or soup.select_one(".num-applicants__figure")
    )
    data["salary"] = _text(soup.select_one(".compensation__salary")) or _text(
        soup.select_one(".salary")
    )

    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    if desc:
        data["description_html"] = desc.decode_contents().strip()
        data["description"] = desc.get_text("\n", strip=True)

    for item in soup.select(".description__job-criteria-item"):
        key = _text(item.select_one(".description__job-criteria-subheader")).lower()
        val = _text(item.select_one(".description__job-criteria-text"))
        field = CRITERIA_MAP.get(key)
        if field:
            data[field] = val

    # Offsite apply URL is hidden in <code id="applyUrl"><!--"https://..."--></code>
    apply_code = soup.select_one("code#applyUrl")
    if apply_code:
        comment = apply_code.find(string=lambda s: isinstance(s, Comment))
        raw = (comment or apply_code.get_text()).strip().strip('"')
        m = re.search(r"url=([^&]+)", raw)
        data["apply_url"] = unquote(m.group(1)) if m else raw
    data["is_easy_apply"] = None
    if "apply-link-onsite" in html:
        data["is_easy_apply"] = True
    elif "apply-link-offsite" in html:
        data["is_easy_apply"] = False

    closed = soup.select_one(".closed-job") or soup.find(
        string=re.compile(r"No longer accepting applications", re.I)
    )
    data["is_closed"] = bool(closed)
    return data


# ---------------------------------------------------------------- company page
ABOUT_FIELDS = {
    "website": "website",
    "industry": "industry",
    "size": "company_size",
    "headquarters": "headquarters",
    "organizationType": "organization_type",
    "foundedOn": "founded",
    "specialties": "specialties",
}


def parse_company_page(html):
    soup = _soup(html)
    data = {}

    # 1) Structured data (JSON-LD Organization)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        nodes = payload.get("@graph", [payload]) if isinstance(payload, dict) else payload
        for node in nodes:
            types = node.get("@type") if isinstance(node, dict) else None
            types = set(types) if isinstance(types, list) else {types}
            if not types & ORG_TYPES:
                continue
            data["name"] = node.get("name", "")
            data["description"] = node.get("description", "") or ""
            data["slogan"] = node.get("slogan", "") or ""
            website = node.get("sameAs")
            if isinstance(website, list):
                website = website[0] if website else ""
            data["website"] = website or ""
            emp = node.get("numberOfEmployees") or {}
            if isinstance(emp, dict) and emp.get("value") is not None:
                data["employees_on_linkedin"] = _to_int(str(emp["value"]))
            addr = node.get("address") or {}
            if isinstance(addr, dict):
                data["street_address"] = addr.get("streetAddress", "") or ""
                data["city"] = addr.get("addressLocality", "") or ""
                data["region"] = addr.get("addressRegion", "") or ""
                data["postal_code"] = addr.get("postalCode", "") or ""
                data["country"] = addr.get("addressCountry", "") or ""
            logo = node.get("logo") or {}
            if isinstance(logo, dict):
                data["logo_url"] = logo.get("contentUrl", "") or ""

    # 2) "About us" block
    for key, field in ABOUT_FIELDS.items():
        node = soup.select_one(f'[data-test-id="about-us__{key}"] dd')
        if node:
            val = _text(node)
            if field == "website":
                a = node.select_one("a")
                if a and a.get("href"):
                    href = a["href"]
                    m = re.search(r"[?&]url=([^&]+)", href)
                    val = unquote(m.group(1)) if m else val or href
            if val:
                data[field] = val

    if not data.get("description"):
        data["description"] = _text(soup.select_one('[data-test-id="about-us__description"]'))
    if not data.get("name"):
        data["name"] = _text(soup.select_one("h1.top-card-layout__title"))

    subline = _text(soup.select_one(".top-card-layout__first-subline"))
    m = re.search(r"([\d,\.]+)\s+followers", subline)
    if m:
        data["followers"] = _to_int(m.group(1))
    if not data.get("industry"):
        data["industry"] = _text(soup.select_one(".top-card-layout__headline"))
    return {k: v for k, v in data.items() if v not in (None, "")}
