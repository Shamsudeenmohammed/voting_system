"""Local / LAN hosted profile.

Runs without internet access, uses SQLite by default and keeps debug
conveniences.  Set DB_ENGINE=postgresql + DB_* to use PostgreSQL locally.
"""

from .base import *  # noqa: F401,F403
from .base import DEBUG, SECRET_KEY, TESTING, env, env_bool  # noqa: F401

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0"
).split(",")
ALLOWED_HOSTS = [h.strip() for h in ALLOWED_HOSTS if h.strip()]

# Local profile: never redirect to HTTPS (LAN deployments terminate TLS only
# when explicitly configured).
SECURE_SSL_REDIRECT = False

# Static files served by WhiteNoise even locally (one code path everywhere).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

