"""Hardened production profile for online elections.

Refuses to start when secrets are missing so that a production deployment can
never silently fall back to development defaults.
"""

import sys

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, SECRET_KEY, env, env_bool, env_list  # noqa: F401

DEBUG = False

# --- Secrets must be provided explicitly -----------------------------------
if SECRET_KEY.startswith("django-insecure") or not SECRET_KEY:  # noqa: SIM114
    sys.exit(
        "FATAL: DJANGO_SECRET_KEY must be set to a strong random value in production."
    )
if not env("BALLOT_ENCRYPTION_KEY") and not env("BALLOT_KEY_FILE"):
    # A key file outside the repository is also acceptable; the default
    # secrets/ path inside BASE_DIR must exist and be git-ignored.
    from pathlib import Path

    if not Path(BALLOT_KEY_FILE).exists():  # noqa: F405
        sys.exit(
            "FATAL: set BALLOT_ENCRYPTION_KEY or provide an existing "
            f"key file at {BALLOT_KEY_FILE}."
        )

# --- PostgreSQL is mandatory in production ----------------------------------
DB_ENGINE = env("DB_ENGINE", "postgresql").strip().lower()
if not DB_ENGINE.startswith("postgres"):
    sys.exit("FATAL: production profile requires DB_ENGINE=postgresql.")

# --- Hosts / HTTPS -----------------------------------------------------------
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    sys.exit("FATAL: DJANGO_ALLOWED_HOSTS must list the public host name(s).")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 31536000)  # noqa: F405
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# --- Static files via WhiteNoise manifest storage ---------------------------
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# --- Logging to stdout for containers ---------------------------------------
LOGGING["handlers"] = {  # noqa: F405
    "console": {
        "class": "logging.StreamHandler",
        "formatter": "standard",
    },
}
LOGGING["root"] = {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")}

# Background worker must have an explicit broker in production.
if not CELERY_BROKER_URL:  # noqa: F405
    sys.exit(
        "FATAL: CELERY_BROKER_URL (redis://...) required in production; "
        "set CELERY_TASK_ALWAYS_EAGER=1 only for single-process evaluations."
    )
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)  # noqa: F405
