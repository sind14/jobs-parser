"""Django settings for the LinkedIn jobs parser project."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if cast is bool:
        return value.lower() in ("1", "true", "yes", "on")
    return cast(value)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG", True, bool)
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "*").split(",")
# needed only when the admin is served from a public domain, e.g. https://demo.example.com
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # serves the admin's CSS/JS in Docker (gunicorn, DEBUG=0) without a separate web server
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "linkedin_jobs"),
        "USER": env("POSTGRES_USER", "linkedin"),
        "PASSWORD": env("POSTGRES_PASSWORD", "linkedin"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5433"),
        "CONN_MAX_AGE": 60,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "uk"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Parser settings (all can be overridden through environment / .env)
# ---------------------------------------------------------------------------
LINKEDIN = {
    # Adaptive (AIMD) request rate per process, requests/second.
    # Halved on HTTP 429/999, slowly grows back on success.
    "RATE": env("LI_RATE", 1.5, float),
    "RATE_MIN": env("LI_RATE_MIN", 0.2, float),
    "RATE_MAX": env("LI_RATE_MAX", 4.0, float),
    # Company pages are protected much harder (HTTP 999 already at ~0.2 req/s),
    # so they get their own, slower limiter that can go much lower and grows back slowly.
    "COMPANY_RATE": env("LI_COMPANY_RATE", 0.15, float),
    "COMPANY_RATE_MIN": env("LI_COMPANY_RATE_MIN", 0.03, float),
    "COMPANY_RATE_MAX": env("LI_COMPANY_RATE_MAX", 0.3, float),
    "COMPANY_RATE_STEP": env("LI_COMPANY_RATE_STEP", 0.002, float),
    "TIMEOUT": env("LI_TIMEOUT", 20, int),
    "MAX_RETRIES": env("LI_MAX_RETRIES", 5, int),
    # Base pause after HTTP 429 / 999; grows exponentially
    "BACKOFF_BASE": env("LI_BACKOFF_BASE", 10, float),
    # Comma-separated list of proxies: http://user:pass@host:port,...
    "PROXIES": [p for p in env("LI_PROXIES", "").split(",") if p.strip()],
    # Number of parallel workers for the details stage
    "DETAILS_WORKERS": env("LI_DETAILS_WORKERS", 6, int),
    # Circuit breaker: stop the run after this many 429/999 in a row (IP is banned
    # for now; going on only produces useless requests). Re-run later to continue.
    "MAX_CONSECUTIVE_BLOCKS": env("LI_MAX_CONSECUTIVE_BLOCKS", 15, int),
    # Keep compressed raw HTML of job/company pages (table jobs_rawpage) so the
    # parsers can be re-run offline with `manage.py reparse` — no new requests.
    "STORE_RAW_HTML": env("LI_STORE_RAW_HTML", True, bool),
    # Max attempts for one job/company before giving up
    "DETAILS_MAX_ATTEMPTS": env("LI_DETAILS_MAX_ATTEMPTS", 3, int),
    # If a search slice returns at least this many results it is considered
    # truncated by LinkedIn pagination limit and is split by additional filters
    "SLICE_SPLIT_THRESHOLD": env("LI_SLICE_SPLIT_THRESHOLD", 900, int),
    # Hard pagination limit of the guest search endpoint
    "MAX_START": env("LI_MAX_START", 990, int),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "parser.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "simple",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "jobs": {"handlers": ["console", "file"], "level": env("LOG_LEVEL", "INFO")},
    },
}
(BASE_DIR / "logs").mkdir(exist_ok=True)
if "test" in sys.argv[1:2]:  # keep test runs out of the parser log
    LOGGING["loggers"]["jobs"] = {"handlers": [], "level": "CRITICAL"}
