"""Project configuration package.

``DJANGO_SETTINGS_MODULE`` should point at ``config.settings`` (auto-selects a
profile from ``DJANGO_ENV``) or explicitly at ``config.settings.local`` /
``config.settings.production`` / ``config.settings.test``.
"""

try:  # pragma: no cover - celery is optional at import time
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except ImportError:  # pragma: no cover
    # Celery is only required for background workers; the web process and the
    # eager task mode keep working without it.
    pass
