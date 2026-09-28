from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
_REQUIRE_STRONG_SECRET = os.environ.get("FLEETLINE_REQUIRE_STRONG_SECRET", "0") == "1"
_DEVELOPMENT_SECRET_KEY = "development-only-change-me"  # noqa: S105 - debug-only fallback
_configured_secret_key = os.environ.get("DJANGO_SECRET_KEY")


def _strong_secret_key(value: str | None) -> bool:
    return bool(
        value
        and len(value) >= 50
        and len(set(value)) >= 5
        and not value.startswith(("django-insecure-", "development-only-", "replace-with-"))
    )


if (_REQUIRE_STRONG_SECRET or not DEBUG) and not _strong_secret_key(_configured_secret_key):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be explicitly set to a random value of at least "
        "50 characters with at least 5 unique characters for non-debug or "
        "production-entrypoint settings."
    )
SECRET_KEY = _configured_secret_key or _DEVELOPMENT_SECRET_KEY
ALLOWED_HOSTS = [x for x in os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x]
CSRF_TRUSTED_ORIGINS = [x for x in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if x]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "core",
    "assets",
    "maintenance",
    "inventory",
    "purchasing",
    "integrations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.ActiveUserMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fleetops.urls"
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
            ]
        },
    }
]
WSGI_APPLICATION = "fleetops.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "fleetline"),
        "USER": os.environ.get("POSTGRES_USER", "fleetline"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "fleetline"),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
    }
}

AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "frontend_dist"] if (BASE_DIR / "frontend_dist").exists() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", str(8 * 60 * 60)))
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_NAME = "fleetline_sessionid"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "0") == "1"
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
SECURE_HSTS_PRELOAD = os.environ.get("SECURE_HSTS_PRELOAD", "0") == "1"
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "0") == "1"
if TRUST_PROXY_HEADERS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGIN_ATTEMPT_LIMIT = min(32_767, max(1, int(os.environ.get("LOGIN_ATTEMPT_LIMIT", "5"))))
LOGIN_ATTEMPT_WINDOW_SECONDS = max(1, int(os.environ.get("LOGIN_ATTEMPT_WINDOW_SECONDS", "300")))
LOGIN_LOCKOUT_SECONDS = max(1, int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900")))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "core.authentication.ScopedTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "core.schema.FleetlineAutoSchema",
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Fleetline API",
    "DESCRIPTION": "Versioned API for fleet maintenance and parts operations.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
OFFLINE_CACHE_HOURS = int(os.environ.get("OFFLINE_CACHE_HOURS", "24"))
ATTACHMENT_MAX_BYTES = int(os.environ.get("ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024)))
DOCUMENT_MAX_BYTES = max(
    ATTACHMENT_MAX_BYTES,
    int(os.environ.get("DOCUMENT_MAX_BYTES", str(100 * 1024 * 1024))),
)
DOCUMENT_MAX_PAGES = min(1000, max(1, int(os.environ.get("DOCUMENT_MAX_PAGES", "500"))))
DOCUMENT_COMMAND_TIMEOUT_SECONDS = min(
    120.0, max(1.0, float(os.environ.get("DOCUMENT_COMMAND_TIMEOUT_SECONDS", "30")))
)
DOCUMENT_PROCESS_TIMEOUT_SECONDS = min(
    900.0,
    max(
        DOCUMENT_COMMAND_TIMEOUT_SECONDS,
        float(os.environ.get("DOCUMENT_PROCESS_TIMEOUT_SECONDS", "180")),
    ),
)
DOCUMENT_MAX_PAGE_TEXT_BYTES = max(
    1024,
    min(2 * 1024 * 1024, int(os.environ.get("DOCUMENT_MAX_PAGE_TEXT_BYTES", str(512 * 1024)))),
)
DOCUMENT_MAX_TOTAL_TEXT_BYTES = max(
    DOCUMENT_MAX_PAGE_TEXT_BYTES,
    min(
        128 * 1024 * 1024,
        int(os.environ.get("DOCUMENT_MAX_TOTAL_TEXT_BYTES", str(16 * 1024 * 1024))),
    ),
)
DOCUMENT_MAX_OCR_IMAGE_BYTES = max(
    1024 * 1024,
    min(
        64 * 1024 * 1024,
        int(os.environ.get("DOCUMENT_MAX_OCR_IMAGE_BYTES", str(16 * 1024 * 1024))),
    ),
)
DOCUMENT_MAX_OCR_TSV_BYTES = max(
    DOCUMENT_MAX_PAGE_TEXT_BYTES,
    min(8 * 1024 * 1024, int(os.environ.get("DOCUMENT_MAX_OCR_TSV_BYTES", str(2 * 1024 * 1024)))),
)
DOCUMENT_MAX_OCR_PIXELS = max(
    1_000_000,
    min(64_000_000, int(os.environ.get("DOCUMENT_MAX_OCR_PIXELS", "20000000"))),
)
STAGED_ATTACHMENT_MAX_COUNT = int(os.environ.get("STAGED_ATTACHMENT_MAX_COUNT", "25"))
STAGED_ATTACHMENT_MAX_BYTES = int(
    os.environ.get("STAGED_ATTACHMENT_MAX_BYTES", str(50 * 1024 * 1024))
)
STAGED_ATTACHMENT_TTL_HOURS = int(os.environ.get("STAGED_ATTACHMENT_TTL_HOURS", "24"))
PO_APPROVAL_THRESHOLD = os.environ.get("PO_APPROVAL_THRESHOLD", "1000.00")
INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD = os.environ.get(
    "INVENTORY_COUNT_VARIANCE_APPROVAL_THRESHOLD", "0.00"
)
METER_MAX_MILES_PER_HOUR = os.environ.get("METER_MAX_MILES_PER_HOUR", "100.0")
METER_MAX_ENGINE_HOURS_PER_HOUR = os.environ.get("METER_MAX_ENGINE_HOURS_PER_HOUR", "1.25")
METER_FUTURE_TOLERANCE_SECONDS = max(
    0, int(os.environ.get("METER_FUTURE_TOLERANCE_SECONDS", "300"))
)
TELEMATICS_MAX_BODY_BYTES = max(
    1024, int(os.environ.get("TELEMATICS_MAX_BODY_BYTES", str(1024 * 1024)))
)
TELEMATICS_MAX_DIAGNOSTICS = max(1, int(os.environ.get("TELEMATICS_MAX_DIAGNOSTICS", "100")))
# Hours after a DTC alert is closed before the same code on the same asset may raise a new one.
DTC_ALERT_COOLDOWN_HOURS = max(0, int(os.environ.get("DTC_ALERT_COOLDOWN_HOURS", "24")))
WEBHOOK_ALLOW_HTTP = os.environ.get("WEBHOOK_ALLOW_HTTP", "0") == "1"
WEBHOOK_ALLOW_PRIVATE_NETWORKS = os.environ.get("WEBHOOK_ALLOW_PRIVATE_NETWORKS", "0") == "1"
WEBHOOK_MAX_SUBSCRIPTIONS = max(1, int(os.environ.get("WEBHOOK_MAX_SUBSCRIPTIONS", "20")))
WEBHOOK_MAX_EVENT_TYPES = max(1, int(os.environ.get("WEBHOOK_MAX_EVENT_TYPES", "25")))
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = min(
    30.0, max(1.0, float(os.environ.get("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "5")))
)
REQUIRE_WORKER = os.environ.get("REQUIRE_WORKER", "1") == "1"
PM_RECALCULATION_INTERVAL_SECONDS = max(
    1, int(os.environ.get("PM_RECALCULATION_INTERVAL_SECONDS", "3600"))
)
