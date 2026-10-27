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
    # Back-office theme.  Must come before django.contrib.admin so its admin
    # templates (and sign-in page) take precedence over the stock ones.
    "jazzmin",
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
                "organizations.context_processors.navigation",
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
    # {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    # {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    # {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    # {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
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

# Backdrop for the verification workstation (and other kiosk-style screens).
# Use a path relative to a static directory (for example
# "img/workstation-background.svg") or an absolute URL/path (for example
# "/media/backgrounds/station.jpg").  Drop any picture into static/img/ and
# point this setting at it; no template changes are required.
VERIFICATION_BACKGROUND_IMAGE = env(
    "VERIFICATION_BACKGROUND_IMAGE", "img/nursing.jpeg"
)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Candidate passport photographs.
# Maximum size (in bytes) of an uploaded candidate passport photograph.
CANDIDATE_PHOTO_MAX_UPLOAD_SIZE = env_int(
    "CANDIDATE_PHOTO_MAX_UPLOAD_MB", 5
) * 1024 * 1024

# Whether Django itself should serve media files (candidate photographs).
# Defaults to DEBUG for local development. In production a real web server
# (nginx, etc.) should serve MEDIA_ROOT instead; see docs/DEPLOY_ONLINE.md.
SERVE_MEDIA = env_bool("SERVE_MEDIA", DEBUG)

# ---------------------------------------------------------------------------
# Back-office theme (django-jazzmin)
#
# Jazzmin re-skins django.contrib.admin, which is mounted at /watch/.  The
# branding is deliberately kept in step with the product UI (templates/base.html
# and templates/registration/login.html): Inter typeface, indigo accents and a
# navy sidebar.  Visual overrides live in static/css/admin_theme.css and the
# custom sign-in page in templates/admin/login.html.
# ---------------------------------------------------------------------------

BRAND_NAME = env("BRAND_NAME", "VoteSys")

JAZZMIN_SETTINGS = {
    # -- Titles / branding ---------------------------------------------------
    "site_title": env("JAZZMIN_SITE_TITLE", f"{BRAND_NAME} back office"),
    "site_header": env("JAZZMIN_SITE_HEADER", BRAND_NAME),
    "site_brand": env("JAZZMIN_SITE_BRAND", BRAND_NAME),
    "site_logo": "img/brand-mark.svg",
    "login_logo": "img/brand-mark.svg",
    "site_icon": "img/brand-mark.svg",
    "site_logo_classes": "brand-image",
    "welcome_sign": "Sign in to the election administration console",
    "copyright": BRAND_NAME,
    # -- Search --------------------------------------------------------------
    "search_model": ["auth.User", "elections.Election"],
    "user_avatar": None,
    # -- Top navigation (links back into the product console) ----------------
    "topmenu_links": [
        {"name": "Product console", "url": "dashboard", "icon": "fas fa-gauge-high"},
        {"name": "All elections", "url": "home", "icon": "fas fa-vote-yea"},
        {"name": "Needs attention", "url": "operations", "icon": "fas fa-bell"},
        {"name": "Results", "url": "results_index", "icon": "fas fa-chart-column"},
        {"name": "Audit", "url": "audit_overview", "icon": "fas fa-shield-halved"},
    ],
    # -- User menu -----------------------------------------------------------
    "usermenu_links": [
        {"name": "Product console", "url": "dashboard", "icon": "fas fa-gauge-high"},
        {"name": "Organisations console", "url": "manage_organizations", "icon": "fas fa-sitemap"},
    ],
    # -- Side menu -----------------------------------------------------------
    "show_sidebar": True,
    "navigation_expanded": True,
    "hide_apps": [],
    "hide_models": [],
    "order_with_respect_to": [
        "elections",
        "organizations",
        "audit",
        "auth",
        "elections.election",
        "elections.position",
        "elections.candidate",
        "elections.voter",
        "elections.pollingstation",
        "elections.electionapproval",
        "organizations.organization",
        "organizations.organizationmembership",
        "audit.auditchain",
        "audit.auditevent",
        "audit.auditcheckpoint",
        "auth.user",
        "auth.group",
    ],
    "icons": {
        "elections": "fas fa-vote-yea",
        "elections.election": "fas fa-poll",
        "elections.position": "fas fa-list-ol",
        "elections.candidate": "fas fa-user-tie",
        "elections.voter": "fas fa-id-card",
        "elections.pollingstation": "fas fa-map-location-dot",
        "elections.electionapproval": "fas fa-check-double",
        "organizations": "fas fa-sitemap",
        "organizations.organization": "fas fa-building",
        "organizations.organizationmembership": "fas fa-user-tag",
        "audit": "fas fa-shield-halved",
        "audit.auditchain": "fas fa-link",
        "audit.auditevent": "fas fa-clipboard-list",
        "audit.auditcheckpoint": "fas fa-flag-checkered",
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.group": "fas fa-users",
    },
    "default_icon_parents": "fas fa-folder",
    "default_icon_children": "fas fa-circle",
    # -- Change form / extras ------------------------------------------------
    "related_modal_active": False,
    "custom_css": "css/admin_theme.css",
    "custom_js": None,
    # Inter is pulled in by the custom stylesheet, so skip the bundled Source
    # Sans Pro webfont.
    "use_google_fonts_cdn": False,
    "show_ui_builder": False,
    "show_theme_chooser": False,
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {},
    "language_chooser": False,
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text": False,
    "footer_small_text": False,
    "body_small_text": False,
    "brand_small_text": False,
    "brand_colour": False,
    "accent": "accent-primary",
    "navbar": "navbar-white navbar-light",
    "no_navbar_border": True,
    "navbar_fixed": True,
    "layout_boxed": False,
    "footer_fixed": False,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_small_text": False,
    "sidebar_disable_expand": False,
    "sidebar_nav_child_indent": True,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style": False,
    "theme": "default",
    "default_theme_mode": "light",
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}

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

# Explicit system-wide oversight grant for named election-commission chairs.
# Comma-separated usernames; each gains election and people oversight across
# every tenant (but NOT organization lifecycle administration, which stays with
# the Super Admin).  Empty by default: scope comes from memberships unless a
# deployment deliberately names a system-wide chair here.
EC_CHAIR_SYSTEM_SCOPE_USERNAMES = env_list("EC_CHAIR_SYSTEM_SCOPE_USERNAMES")

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
