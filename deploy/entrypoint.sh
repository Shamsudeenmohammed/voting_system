#!/bin/sh
# Container entrypoint: wait for the database, apply migrations, collect
# static files, then exec the container command (gunicorn, celery worker, ...).
set -eu

echo "entrypoint: waiting for the database..."
python - <<'PY'
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()  # fails fast on the production profile if secrets are missing

from django.db import connections
from django.db.utils import OperationalError

for attempt in range(60):
    try:
        connections["default"].ensure_connection()
        break
    except OperationalError as exc:
        print(f"entrypoint: database not ready ({exc}); retrying...", flush=True)
        time.sleep(2)
else:
    sys.exit("entrypoint: database never became ready")
PY

echo "entrypoint: applying migrations..."
python manage.py migrate --noinput

echo "entrypoint: collecting static files..."
python manage.py collectstatic --noinput

echo "entrypoint: exec $*"
exec "$@"
