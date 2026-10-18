"""Dedicated test profile: DJANGO_SETTINGS_MODULE=config.settings.test

Used by CI and by ``docker compose run web manage.py test`` against
PostgreSQL (set DB_ENGINE=postgresql before running).
"""

from .base import *  # noqa: F401,F403
from .base import env_bool  # noqa: F401

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = ""
CELERY_RESULT_BACKEND = ""
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Never talk to a shared Redis from the test suite.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "votesys-tests",
    }
}

if env_bool("TEST_KEEP_DB", False):
    from .base import DATABASES  # noqa: F401

    # Update (do not replace): base.py already configures a file-based TEST
    # NAME so concurrency tests get real cross-connection locking instead of
    # SQLite's shared-cache in-memory database.
    DATABASES["default"].setdefault("TEST", {})
    DATABASES["default"]["TEST"].update({"MIGRATE": True, "KEEPDB": True})
