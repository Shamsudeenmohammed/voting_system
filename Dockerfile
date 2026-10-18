# Voting System - production image.
#
# Build does NOT run Django: the `production` settings profile requires
# runtime secrets and fails fast without them (see config/settings/
# production.py).  Migrations + collectstatic run in deploy/entrypoint.sh.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system votesys && adduser --system --ingroup votesys votesys

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Runtime-writable paths (credentials CSVs, auto-generated local keys,
# collected static files).  Also drop anything a dev may have left behind.
RUN rm -rf .git .test_dbs db.sqlite3 secrets private __pycache__ \
    && mkdir -p /app/secrets /app/private/credentials /app/staticfiles \
    && chown -R votesys:votesys /app

USER votesys
EXPOSE 8000

# Shell form so the script does not depend on the git exec bit (Windows devs).
ENTRYPOINT ["/bin/sh", "/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "voting_system.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "90", "--access-logfile", "-", "--error-logfile", "-"]
