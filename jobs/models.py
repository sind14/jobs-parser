from django.db import models
from django.utils import timezone


class ProcessingStatus(models.TextChoices):
    NEW = "new", "Нова"
    PROCESSING = "processing", "В обробці"
    DONE = "done", "Готово"
    FAILED = "failed", "Помилка"
    NOT_FOUND = "not_found", "Не знайдено / закрита"


class SearchQuery(models.Model):
    """A search request configured by the user in the admin (keywords + country)."""

    class Status(models.TextChoices):
        NEW = "new", "Новий"
        RUNNING = "running", "Виконується"
        DONE = "done", "Завершено"
        LIMIT = "limit", "Досягнуто ліміт"
        FAILED = "failed", "Помилка"

    class TimePosted(models.TextChoices):
        ANY = "", "За весь час"
        DAY = "r86400", "За 24 години"
        WEEK = "r604800", "За тиждень"
        MONTH = "r2592000", "За місяць"

    keywords = models.CharField("Пошуковий запит", max_length=255)
    location = models.CharField(
        "Країна / локація", max_length=255, help_text="Наприклад: United States, Germany, Ukraine"
    )
    geo_id = models.CharField(
        "LinkedIn geoId", max_length=32, blank=True,
        help_text="Необов'язково. Точний geoId локації (наприклад 103644278 для United States)",
    )
    time_posted = models.CharField(
        "Дата публікації", max_length=16, blank=True, choices=TimePosted.choices, default=TimePosted.ANY
    )
    regions = models.TextField(
        "Регіони для розбиття", blank=True,
        help_text="По одному в рядку. Порожньо — вбудований список регіонів країни "
                  "(США, Канада, UK, Німеччина, Польща, Україна, Індія, Австралія). "
                  "Потрібно, щоб обійти ліміт LinkedIn ~1000 результатів на один пошук.",
    )
    max_jobs = models.PositiveIntegerField(
        "Ліміт вакансій", null=True, blank=True, help_text="Порожньо — зібрати все доступне"
    )
    is_active = models.BooleanField("Активний", default=True)

    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.NEW)
    jobs_found = models.PositiveIntegerField("Знайдено унікальних вакансій", default=0)
    requests_made = models.PositiveIntegerField("HTTP-запитів", default=0)
    last_error = models.TextField("Остання помилка", blank=True)
    started_at = models.DateTimeField("Запущено", null=True, blank=True)
    finished_at = models.DateTimeField("Завершено", null=True, blank=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Змінено", auto_now=True)

    class Meta:
        verbose_name = "Пошуковий запит"
        verbose_name_plural = "Пошукові запити"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["keywords", "location", "time_posted"], name="uniq_search_query"
            )
        ]

    def __str__(self):
        return f"{self.keywords} — {self.location}"


class SearchSlice(models.Model):
    """
    A unit of search work: base query + extra LinkedIn parameters.

    The guest search endpoint returns at most ~1000 results per search, so a
    query is adaptively split into slices (by region of the country, then by
    date posted). Each slice stores its own pagination cursor, so an
    interrupted run resumes exactly where it stopped.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Очікує"
        RUNNING = "running", "Виконується"
        DONE = "done", "Готово"
        SPLIT = "split", "Розбито на під-зрізи"
        FAILED = "failed", "Помилка"

    query = models.ForeignKey(SearchQuery, on_delete=models.CASCADE, related_name="slices")
    filters = models.JSONField("Фільтри", default=dict, blank=True)
    filters_key = models.CharField("Ключ фільтрів", max_length=255)
    depth = models.PositiveSmallIntegerField("Глибина", default=0)
    status = models.CharField("Статус", max_length=16, choices=Status.choices, default=Status.PENDING)
    next_start = models.PositiveIntegerField("Курсор пагінації", default=0)
    results_seen = models.PositiveIntegerField("Отримано карток", default=0)
    new_jobs = models.PositiveIntegerField("Нових вакансій", default=0)
    attempts = models.PositiveSmallIntegerField("Спроб", default=0)
    last_error = models.TextField("Остання помилка", blank=True)
    updated_at = models.DateTimeField("Змінено", auto_now=True)

    class Meta:
        verbose_name = "Зріз пошуку"
        verbose_name_plural = "Зрізи пошуку"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["query", "filters_key"], name="uniq_slice_per_query")
        ]

    def __str__(self):
        return self.filters_key or "(базовий)"


class Company(models.Model):
    slug = models.CharField("Slug (ідентифікатор)", max_length=255, unique=True)
    linkedin_url = models.URLField("LinkedIn URL", max_length=500)
    name = models.CharField("Назва", max_length=500, blank=True)
    logo_url = models.URLField("Логотип", max_length=1000, blank=True)

    # --- details (stage 2) ---
    website = models.URLField("Сайт", max_length=1000, blank=True)
    industry = models.CharField("Галузь", max_length=255, blank=True)
    company_size = models.CharField("Розмір компанії", max_length=100, blank=True)
    employees_on_linkedin = models.PositiveIntegerField("Співробітників на LinkedIn", null=True, blank=True)
    followers = models.PositiveIntegerField("Підписників", null=True, blank=True)
    headquarters = models.CharField("Головний офіс", max_length=255, blank=True)
    street_address = models.CharField("Адреса", max_length=500, blank=True)
    city = models.CharField("Місто", max_length=255, blank=True)
    region = models.CharField("Регіон", max_length=255, blank=True)
    postal_code = models.CharField("Індекс", max_length=50, blank=True)
    country = models.CharField("Країна", max_length=100, blank=True)
    organization_type = models.CharField("Тип організації", max_length=255, blank=True)
    founded = models.CharField("Рік заснування", max_length=20, blank=True)
    specialties = models.TextField("Спеціалізації", blank=True)
    description = models.TextField("Опис", blank=True)
    slogan = models.CharField("Слоган", max_length=1000, blank=True)

    details_status = models.CharField(
        "Статус обробки", max_length=16, choices=ProcessingStatus.choices,
        default=ProcessingStatus.NEW, db_index=True,
    )
    details_attempts = models.PositiveSmallIntegerField("Спроб", default=0)
    details_error = models.TextField("Помилка", blank=True)
    details_locked_at = models.DateTimeField("Взято в роботу", null=True, blank=True)
    details_fetched_at = models.DateTimeField("Деталі отримано", null=True, blank=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Змінено", auto_now=True)

    class Meta:
        verbose_name = "Компанія"
        verbose_name_plural = "Компанії"
        ordering = ["name"]

    def __str__(self):
        return self.name or self.slug


class Job(models.Model):
    job_id = models.BigIntegerField("LinkedIn Job ID", unique=True)
    url = models.URLField("URL", max_length=500)
    title = models.CharField("Назва", max_length=500)
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="jobs",
        verbose_name="Компанія",
    )
    company_name = models.CharField("Компанія (як у картці)", max_length=500, blank=True)
    location = models.CharField("Локація", max_length=255, blank=True)
    posted_date = models.DateField("Дата публікації", null=True, blank=True, db_index=True)
    benefits = models.CharField("Мітка (Actively Hiring тощо)", max_length=255, blank=True)
    salary = models.CharField("Зарплата", max_length=255, blank=True)
    search_queries = models.ManyToManyField(
        SearchQuery, related_name="jobs", blank=True, verbose_name="Пошукові запити"
    )
    first_seen_at = models.DateTimeField("Вперше знайдено", auto_now_add=True)
    # set explicitly by Search only (auto_now would also bump it on every Details save)
    last_seen_at = models.DateTimeField("Востаннє у видачі", default=timezone.now)

    # --- details (stage 2) ---
    description_html = models.TextField("Опис (HTML)", blank=True)
    description = models.TextField("Опис", blank=True)
    seniority_level = models.CharField("Рівень", max_length=255, blank=True)
    employment_type = models.CharField("Тип зайнятості", max_length=255, blank=True)
    job_function = models.CharField("Функція", max_length=500, blank=True)
    industries = models.CharField("Галузі", max_length=500, blank=True)
    applicants = models.CharField("Відгуки", max_length=100, blank=True)
    posted_time_ago = models.CharField("Опубліковано (текст)", max_length=100, blank=True)
    apply_url = models.URLField("Посилання на відгук", max_length=2000, blank=True)
    is_easy_apply = models.BooleanField("Easy Apply", null=True, blank=True)
    is_closed = models.BooleanField("Закрита", default=False)

    details_status = models.CharField(
        "Статус обробки", max_length=16, choices=ProcessingStatus.choices,
        default=ProcessingStatus.NEW, db_index=True,
    )
    details_attempts = models.PositiveSmallIntegerField("Спроб", default=0)
    details_error = models.TextField("Помилка", blank=True)
    details_locked_at = models.DateTimeField("Взято в роботу", null=True, blank=True)
    details_fetched_at = models.DateTimeField("Деталі отримано", null=True, blank=True)

    class Meta:
        verbose_name = "Вакансія"
        verbose_name_plural = "Вакансії"
        ordering = ["-posted_date", "-job_id"]

    def __str__(self):
        return f"{self.title} @ {self.company_name}"


class RawPage(models.Model):
    """
    Raw (zlib-compressed) HTML of every fetched job / company page.

    Lets us re-run the parsers offline (`manage.py reparse`) after a parser fix or
    a LinkedIn markup change, without sending a single new request to LinkedIn.
    Kept in a separate table so the main tables stay small and fast.
    """

    class Kind(models.TextChoices):
        JOB = "job", "Вакансія"
        COMPANY = "company", "Компанія"

    kind = models.CharField("Тип", max_length=16, choices=Kind.choices)
    key = models.CharField("Ключ (job_id / slug)", max_length=255)
    url = models.URLField("URL", max_length=500)
    html_gz = models.BinaryField("HTML (zlib)")
    size = models.PositiveIntegerField("Розмір HTML, байт", default=0)
    fetched_at = models.DateTimeField("Завантажено", auto_now=True)

    class Meta:
        verbose_name = "Сира сторінка"
        verbose_name_plural = "Сирі сторінки"
        constraints = [models.UniqueConstraint(fields=["kind", "key"], name="uniq_raw_page")]

    def __str__(self):
        return f"{self.kind}:{self.key}"

    @property
    def html(self) -> str:
        import zlib
        return zlib.decompress(bytes(self.html_gz)).decode("utf-8")

    @classmethod
    def store(cls, kind, key, url, html):
        """Insert or replace the latest raw page for (kind, key)."""
        import zlib
        data = html.encode("utf-8")
        cls.objects.update_or_create(
            kind=kind, key=str(key),
            defaults={"url": url, "html_gz": zlib.compress(data, 6), "size": len(data)},
        )
