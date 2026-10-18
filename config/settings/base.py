"""Base settings shared by every deployment profile.

All secrets and environment-specific values are read from environment
variables (never committed to source control).  See ``.env.example``.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

TESTING = "test" in sys.argv


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

# Insecure fallback is only allowed outside production; production.py refuses
# to start with the default key.
SECRET_KEY = env("DJANGO_SECRET_KEY", "django-insecure-dev-only-key-rotate-me")

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project applications
    "organizations",
    "elections",
    "audit",
    "ballot",
    "tally",
    "representatives",
    "monitoring",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "voting_system.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "voting_system.wsgi.application"
ASGI_APPLICATION = "voting_system.asgi.application"

# ---------------------------------------------------------------------------
# Database
#
# SQLite is the zero-configuration default for local/LAN development.
# Production (and optionally local) uses PostgreSQL via DB_* variables.
# ---------------------------------------------------------------------------

DB_ENGINE = env("DB_ENGINE", "sqlite").strip().lower()

if DB_ENGINE.startswith("postgres"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "voting"),
            "USER": env("DB_USER", "voting"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": env_int("DB_CONNECT_TIMEOUT", 10),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(env("DB_NAME", str(BASE_DIR / "db.sqlite3"))),
            "OPTIONS": {
                "timeout": env_int("DB_SQLITE_TIMEOUT", 30),
                # BEGIN IMMEDIATE takes the write lock at transaction start,
                # so concurrent ballot acceptance waits (busy timeout) instead
                # of deadlocking on a deferred read->write lock upgrade
                # ("database table is locked").
                "transaction_mode": "IMMEDIATE",
            },
            "CONN_MAX_AGE": 0,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Caching / transient coordination
#
# Redis is used when REDIS_URL is provided (multi-process safe rate limiting
# and caching).  LocMem is the fallback for single-process local operation.
# ---------------------------------------------------------------------------

REDIS_URL = env("REDIS_URL", "")

if REDIS_URL and not TESTING:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": env("REDIS_KEY_PREFIX", "votesys"),
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "votesys-local",
            "TIMEOUT": 300,
        }
    }

# ---------------------------------------------------------------------------
# Authentication / sessions
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 4 * 3600)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_ENGINE = "django.contrib.sessions.backends.db"

CSRF_COOKIE_HTTPONLY = False  # JavaScript must read the CSRF cookie for live dashboards
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Password hashing
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# ---------------------------------------------------------------------------
# Internationalisation / time
# ---------------------------------------------------------------------------

LANGUAGE_CODE = env("DJANGO_LANGUAGE_CODE", "en-us")
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Uploads / data volume
# ---------------------------------------------------------------------------

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 12 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("FILE_UPLOAD_MAX_MEMORY_SIZE", 12 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000

# ---------------------------------------------------------------------------
# Election platform settings
# ---------------------------------------------------------------------------

# Cryptographic material.  Production must set both explicitly (see docs/
# DEPLOY_ONLINE.md); local mode auto-generates key files under secrets/
# which is git-ignored.
BALLOT_ENCRYPTION_KEY = env("BALLOT_ENCRYPTION_KEY", "")
BALLOT_KEY_FILE = Path(env("BALLOT_KEY_FILE", str(BASE_DIR / "secrets" / "ballot.key")))
RESULT_SIGNING_KEY = env("RESULT_SIGNING_KEY", "")
RESULT_SIGNING_KEY_FILE = Path(
    env("RESULT_SIGNING_KEY_FILE", str(BASE_DIR / "secrets" / "result_signing.key"))
)

# Pepper for hashing short voter tokens (peppered HMAC so a database leak
# alone does not allow offline verification of guessed tokens).
VOTER_TOKEN_PEPPER = env("VOTER_TOKEN_PEPPER", "")

# Rate limits (attempts per window, seconds).
RATE_LIMIT_WINDOW = env_int("RATE_LIMIT_WINDOW", 60)
RATE_LIMIT_VOTER_LOGIN = env_int("RATE_LIMIT_VOTER_LOGIN", 8)
RATE_LIMIT_BALLOT_CAST = env_int("RATE_LIMIT_BALLOT_CAST", 12)
RATE_LIMIT_REPORT_DOWNLOAD = env_int("RATE_LIMIT_REPORT_DOWNLOAD", 30)

# Celery (background processing for non-critical work: voter imports, report
# exports).  Ballot acceptance never depends on Celery.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not CELERY_BROKER_URL)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_TIME_LIMIT = env_int("CELERY_TASK_TIME_LIMIT", 15 * 60)

# ---------------------------------------------------------------------------
# Security defaults (production.py hardens these further)
# ---------------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

DEFAULT_AUTO_LOGGING = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django.request": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "votesys": {"level": env("DJANGO_LOG_LEVEL", "INFO"), "handlers": ["console"], "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# Test-run tweaks (applied whenever `manage.py test` runs, regardless of the
# selected profile - see config.settings.test for the dedicated profile).
# ---------------------------------------------------------------------------

if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    CELERY_TASK_ALWAYS_EAGER = True
    SECURE_SSL_REDIRECT = False
    if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
        # A file-based test database is required so that concurrency tests can
        # exercise real row-level locking across threads (in-memory SQLite
        # databases are per-connection).
        test_db_dir = BASE_DIR / ".test_dbs"
        test_db_dir.mkdir(parents=True, exist_ok=True)
        DATABASES["default"]["TEST"] = {"NAME": str(test_db_dir / "test_voting.sqlite3")}
        DATABASES["default"]["OPTIONS"] = {
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
        }
