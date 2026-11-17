import subprocess
import sys

from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils.html import format_html

from jobs.export import export_response
from jobs.models import Company, Job, ProcessingStatus, RawPage, SearchQuery, SearchSlice

STATUS_COLORS = {
    "new": "#6c757d", "pending": "#6c757d", "processing": "#0d6efd", "running": "#0d6efd",
    "done": "#198754", "split": "#20c997", "failed": "#dc3545", "not_found": "#fd7e14",
    "limit": "#6f42c1",
}


def status_badge(value, label):
    color = STATUS_COLORS.get(value, "#6c757d")
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
        'font-size:11px;white-space:nowrap">{}</span>', color, label,
    )


def spawn(*args):
    """Run a management command in a separate background process."""
    kwargs = {"cwd": settings.BASE_DIR}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "manage.py", *args], **kwargs)


# ---------------------------------------------------------------- search queries
class SearchSliceInline(admin.TabularInline):
    model = SearchSlice
    extra = 0
    can_delete = False
    fields = ("filters_key", "depth", "status", "next_start", "results_seen", "new_jobs",
              "attempts", "last_error", "updated_at")
    readonly_fields = fields
    show_change_link = False
    classes = ("collapse",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    list_display = ("keywords", "location", "time_posted", "is_active", "status_badge",
                    "jobs_found", "details_progress", "requests_made", "started_at", "finished_at")
    list_filter = ("status", "is_active", "location")
    search_fields = ("keywords", "location")
    list_editable = ("is_active",)
    readonly_fields = ("status", "jobs_found", "requests_made", "last_error", "started_at",
                       "finished_at", "created_at", "updated_at", "slices_summary")
    fieldsets = (
        ("Параметри пошуку", {"fields": ("keywords", "location", "geo_id", "time_posted",
                                         "regions", "max_jobs", "is_active")}),
        ("Стан", {"fields": ("status", "jobs_found", "requests_made", "slices_summary",
                                  "last_error", "started_at", "finished_at", "created_at",
                                  "updated_at")}),
    )
    inlines = [SearchSliceInline]
    actions = ["run_search", "run_details", "restart_search", "export_csv", "export_xlsx"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _done=Count("jobs", filter=Q(jobs__details_status=ProcessingStatus.DONE)),
        )

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return status_badge(obj.status, obj.get_status_display())

    @admin.display(description="Деталі зібрано")
    def details_progress(self, obj):
        if not obj.jobs_found:
            return "—"
        return f"{obj._done} / {obj.jobs_found} ({obj._done * 100 // obj.jobs_found}%)"

    @admin.display(description="Зрізи")
    def slices_summary(self, obj):
        rows = obj.slices.values("status").annotate(n=Count("id")).order_by("status")
        return ", ".join(f"{r['status']}: {r['n']}" for r in rows) or "—"

    @admin.action(description="▶ Запустити Search (у фоні)")
    def run_search(self, request, queryset):
        for q in queryset:
            spawn("run_search", "--query-id", str(q.pk))
        self.message_user(request, f"Пошук запущено для запитів: {queryset.count()}. "
                                   "Прогрес — у цьому розділі та в logs/parser.log", messages.SUCCESS)

    @admin.action(description="▶ Запустити Details для вакансій запиту (у фоні)")
    def run_details(self, request, queryset):
        for q in queryset:
            spawn("run_details", "--query-id", str(q.pk))
        self.message_user(request, "Збір деталей запущено у фоні", messages.SUCCESS)

    @admin.action(description="↻ Скинути прогрес пошуку (почати заново)")
    def restart_search(self, request, queryset):
        for q in queryset:
            q.slices.all().delete()
        queryset.update(status=SearchQuery.Status.NEW, last_error="")
        self.message_user(request, "Прогрес скинуто. Уже зібрані вакансії збережено.")

    @admin.action(description="⬇ Вивантажити вакансії в CSV")
    def export_csv(self, request, queryset):
        return export_response(Job.objects.filter(search_queries__in=queryset).distinct(), "csv")

    @admin.action(description="⬇ Вивантажити вакансії в Excel")
    def export_xlsx(self, request, queryset):
        return export_response(Job.objects.filter(search_queries__in=queryset).distinct(), "xlsx")


# ---------------------------------------------------------------- jobs
class StatusResetMixin:
    @admin.action(description="↻ Поставити в чергу на повторний збір деталей")
    def reset_details(self, request, queryset):
        n = queryset.update(details_status=ProcessingStatus.NEW, details_attempts=0, details_error="")
        self.message_user(request, f"Поставлено в чергу записів: {n}")


@admin.register(Job)
class JobAdmin(StatusResetMixin, admin.ModelAdmin):
    list_display = ("job_id", "title", "company_link", "location", "posted_date",
                    "seniority_level", "employment_type", "applicants", "salary",
                    "status_badge", "first_seen_at")
    list_filter = ("details_status", "search_queries", ("company", admin.EmptyFieldListFilter),
                   "seniority_level", "employment_type", "is_easy_apply", "is_closed", "posted_date")
    search_fields = ("=job_id", "title", "company_name", "location", "description")
    list_select_related = ("company",)
    list_per_page = 50
    date_hierarchy = "posted_date"
    autocomplete_fields = ("company",)
    filter_horizontal = ("search_queries",)
    actions = ["reset_details", "export_csv", "export_xlsx"]
    readonly_fields = ("job_id", "first_seen_at", "last_seen_at", "details_fetched_at",
                       "details_locked_at", "open_link", "description_preview")
    fieldsets = (
        ("Картка (Search)", {"fields": ("job_id", "open_link", "title", "company", "company_name",
                                          "location", "posted_date", "benefits", "salary",
                                          "search_queries", "first_seen_at", "last_seen_at")}),
        ("Деталі (Details)", {"fields": ("seniority_level", "employment_type", "job_function",
                                         "industries", "applicants", "posted_time_ago",
                                         "is_easy_apply", "apply_url", "is_closed",
                                         "description_preview")}),
        ("Обробка", {"fields": ("details_status", "details_attempts", "details_error",
                                  "details_locked_at", "details_fetched_at")}),
    )

    @admin.display(description="Статус", ordering="details_status")
    def status_badge(self, obj):
        return status_badge(obj.details_status, obj.get_details_status_display())

    @admin.display(description="Компанія", ordering="company_name")
    def company_link(self, obj):
        if obj.company_id:
            return format_html('<a href="/admin/jobs/company/{}/change/">{}</a>',
                               obj.company_id, obj.company_name or obj.company)
        return obj.company_name

    @admin.display(description="LinkedIn")
    def open_link(self, obj):
        return format_html('<a href="{}" target="_blank">{}</a>', obj.url, obj.url)

    @admin.display(description="Опис")
    def description_preview(self, obj):
        # plain text (escaped) — never render scraped HTML as-is in the admin
        if not obj.description:
            return "—"
        return format_html('<div style="max-height:400px;overflow:auto;max-width:900px;'
                           'white-space:pre-wrap">{}</div>', obj.description)

    @admin.action(description="⬇ Вивантажити вибрані в CSV")
    def export_csv(self, request, queryset):
        return export_response(queryset, "csv")

    @admin.action(description="⬇ Вивантажити вибрані в Excel")
    def export_xlsx(self, request, queryset):
        return export_response(queryset, "xlsx")


# ---------------------------------------------------------------- companies
@admin.register(Company)
class CompanyAdmin(StatusResetMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "industry", "company_size", "employees_on_linkedin",
                    "followers", "headquarters", "jobs_count", "status_badge")
    list_filter = ("details_status", "company_size", "organization_type", "country")
    search_fields = ("name", "slug", "industry", "headquarters")
    readonly_fields = ("details_fetched_at", "details_locked_at", "created_at", "updated_at")
    actions = ["reset_details"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_jobs=Count("jobs"))

    @admin.display(description="Вакансій", ordering="_jobs")
    def jobs_count(self, obj):
        return obj._jobs

    @admin.display(description="Статус", ordering="details_status")
    def status_badge(self, obj):
        return status_badge(obj.details_status, obj.get_details_status_display())


# ---------------------------------------------------------------- raw pages (read-only)
@admin.register(RawPage)
class RawPageAdmin(admin.ModelAdmin):
    list_display = ("kind", "key", "size_kb", "fetched_at")
    list_filter = ("kind",)
    search_fields = ("=key",)
    fields = ("kind", "key", "url", "size", "fetched_at")
    readonly_fields = fields

    @admin.display(description="Розмір, КБ", ordering="size")
    def size_kb(self, obj):
        return obj.size // 1024

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = "LinkedIn Jobs Parser"
admin.site.site_title = "LinkedIn Jobs Parser"
admin.site.index_title = "Керування парсером"
