"""CSV / Excel export of jobs together with company data."""
import csv
import io
from datetime import datetime

from django.http import HttpResponse

COLUMNS = [
    # (header, getter)
    ("job_id", lambda j: j.job_id),
    ("job_url", lambda j: j.url),
    ("title", lambda j: j.title),
    ("location", lambda j: j.location),
    ("posted_date", lambda j: j.posted_date.isoformat() if j.posted_date else ""),
    ("posted_time_ago", lambda j: j.posted_time_ago),
    ("seniority_level", lambda j: j.seniority_level),
    ("employment_type", lambda j: j.employment_type),
    ("job_function", lambda j: j.job_function),
    ("industries", lambda j: j.industries),
    ("salary", lambda j: j.salary),
    ("applicants", lambda j: j.applicants),
    ("benefits", lambda j: j.benefits),
    ("is_easy_apply", lambda j: "" if j.is_easy_apply is None else j.is_easy_apply),
    ("apply_url", lambda j: j.apply_url),
    ("is_closed", lambda j: j.is_closed),
    ("description", lambda j: j.description),
    ("search_queries", lambda j: "; ".join(str(q) for q in j.search_queries.all())),
    ("details_status", lambda j: j.details_status),
    ("first_seen_at", lambda j: j.first_seen_at.strftime("%Y-%m-%d %H:%M") if j.first_seen_at else ""),
    # --- company
    ("company_name", lambda j: (j.company.name if j.company else "") or j.company_name),
    ("company_linkedin_url", lambda j: j.company.linkedin_url if j.company else ""),
    ("company_website", lambda j: j.company.website if j.company else ""),
    ("company_industry", lambda j: j.company.industry if j.company else ""),
    ("company_size", lambda j: j.company.company_size if j.company else ""),
    ("company_employees_on_linkedin", lambda j: j.company.employees_on_linkedin if j.company else ""),
    ("company_followers", lambda j: j.company.followers if j.company else ""),
    ("company_headquarters", lambda j: j.company.headquarters if j.company else ""),
    ("company_address", lambda j: j.company.street_address if j.company else ""),
    ("company_city", lambda j: j.company.city if j.company else ""),
    ("company_region", lambda j: j.company.region if j.company else ""),
    ("company_postal_code", lambda j: j.company.postal_code if j.company else ""),
    ("company_country", lambda j: j.company.country if j.company else ""),
    ("company_type", lambda j: j.company.organization_type if j.company else ""),
    ("company_founded", lambda j: j.company.founded if j.company else ""),
    ("company_specialties", lambda j: j.company.specialties if j.company else ""),
    ("company_description", lambda j: j.company.description if j.company else ""),
    ("company_details_status", lambda j: j.company.details_status if j.company else ""),
]
HEADERS = [c[0] for c in COLUMNS]
EXCEL_CELL_LIMIT = 32767


def iter_rows(queryset):
    qs = queryset.select_related("company").prefetch_related("search_queries").order_by("id")
    for job in qs.iterator(chunk_size=1000):
        yield [getter(job) for _, getter in COLUMNS]


def write_csv(queryset, fp):
    writer = csv.writer(fp)
    writer.writerow(HEADERS)
    n = 0
    for row in iter_rows(queryset):
        writer.writerow(row)
        n += 1
    return n


def write_xlsx(queryset, path_or_fp):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("jobs")
    ws.freeze_panes = "A2"
    ws.append(HEADERS)
    n = 0
    for row in iter_rows(queryset):
        clean = []
        for v in row:
            if isinstance(v, str):
                v = ILLEGAL_CHARACTERS_RE.sub("", v)[:EXCEL_CELL_LIMIT]
            clean.append(v)
        ws.append(clean)
        n += 1
    wb.save(path_or_fp)
    return n


def export_response(queryset, fmt):
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if fmt == "xlsx":
        buf = io.BytesIO()
        write_xlsx(queryset, buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="linkedin_jobs_{stamp}.xlsx"'
        return resp
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="linkedin_jobs_{stamp}.csv"'
    resp.write("﻿")  # BOM so Excel opens UTF-8 correctly
    write_csv(queryset, resp)
    return resp
