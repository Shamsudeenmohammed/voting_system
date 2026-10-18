"""Celery application for non-critical background work.

Deliberate scope: voter-registry imports and report/exports run in the
background.  Ballot acceptance, tallying and certification are always
synchronous and transactional - they must never depend on a broker.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("voting_system")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
