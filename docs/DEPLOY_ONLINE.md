# Online deployment guide

This document describes how to run the voting system as an internet-facing
service.  It is the file referenced by `config/settings/base.py`.

## 1. Topology

```
                   ┌─ TLS terminator (reverse proxy) ─┐
                   │  forwards X-Forwarded-Proto: https│
                   └───────────────┬───────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │ web (gunicorn)     │   worker (celery)  │   ← Docker image
              │ voting_system.wsgi │   celery -A config │
              └───────┬────────────┴─────────┬──────────┘
                      │                      │
               PostgreSQL (votes)      Redis (cache/broker)
```

* The app is a **modular monolith**: one image serves the public site, the
  voter access point, the monitor pages and the dashboard.
* Redis is used for multi-process-safe rate limiting and caching.  Without it
  the cache falls back to in-process LocMem, which is only safe with a single
  gunicorn worker.
* Celery is configured but currently has **no background tasks** (ballot
  acceptance and tallying run in-request, transactionally).  The worker
  service is optional scaffolding.

## 2. Required configuration

Copy `.env.example` to `.env` and fill in real values.  The `production`
profile (`DJANGO_ENV=production`) **exits at startup** rather than running
with development defaults when any of these is missing:

| Variable | Why it is required |
| --- | --- |
| `DJANGO_SECRET_KEY` | sessions/CSRF; must not be the insecure dev default |
| `DJANGO_ALLOWED_HOSTS` | non-empty list of public host names |
| `DB_ENGINE=postgresql` | SQLite is not permitted in production |
| `CELERY_BROKER_URL` (or `CELERY_TASK_ALWAYS_EAGER=1`) | explicit broker for the (optional) worker |
| `BALLOT_ENCRYPTION_KEY` **or** an existing `BALLOT_KEY_FILE` | ballot-at-rest encryption |
| TLS: `SECURE_SSL_REDIRECT=true` (default) + `DJANGO_CSRF_TRUSTED_ORIGINS` | HTTPS-only cookies and CSRF |

### Generating key material

```bash
# Django secret key
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Ballot encryption key (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Result/audit signing key (Ed25519 PEM) - never use a raw string in production
openssl genpkey -algorithm ed25519 -out result_signing.key

# Voter credential pepper (any high-entropy string)
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Key material can be provided as environment variables (secret manager) or as
files mounted into the container; the settings default file paths are
`<BASE_DIR>/secrets/ballot.key` and `<BASE_DIR>/secrets/result_signing.key`,
which the provided `docker-compose.yml` persists in the `secrets` volume.

**Never rotate `BALLOT_ENCRYPTION_KEY` or `VOTER_TOKEN_PEPPER` while sealed
ballots or unexpired credentials exist** — see `docs/OPERATIONS.md`.

## 3. Running with Docker (reference setup)

```bash
cp .env.example .env          # fill in secrets
docker compose up -d --build  # db + redis + web + worker
```

`deploy/entrypoint.sh` waits for PostgreSQL, runs `migrate` and
`collectstatic`, then execs gunicorn.  Static files are served by WhiteNoise
(`CompressedManifestStaticFilesStorage`), so no separate web-server alias is
required.

### Media files (candidate passport photographs)

Uploaded candidate photographs are stored on disk under `MEDIA_ROOT`
(`./media`, persisted in the `media` volume of `docker-compose.yml`).

* `SERVE_MEDIA` controls whether Django serves `/media/` itself.  It defaults
  to `DEBUG` (on for local development).  The docker-compose stack sets
  `SERVE_MEDIA=true` so photographs work out of the box without a proxy.
* **Production:** put nginx (or your TLS terminator) in front of gunicorn and
  serve `/media/` directly from the `media` volume (alias or bind mount), then
  set `SERVE_MEDIA=false` so Django does not serve files itself.  Example nginx
  location:

  ```nginx
  location /media/ {
      alias /app/media/;        # same path as the `media` volume
      expires 7d;
      add_header Cache-Control "public";
  }
  ```

* **Persistence/backup:** the `media` volume must be backed up together with
  the database and the `secrets`/`credentials` volumes — photographs are
  referenced by URL in the ballot and management screens, and re-uploading
  them after a restore is impractical once elections are locked.
* `CANDIDATE_PHOTO_MAX_UPLOAD_MB` (default 5) caps the size of a single
  photograph upload; the server validates real image content (JPEG/PNG/WebP)
  regardless of the client-supplied filename or content type.

Without Docker:

```powershell
$env:DJANGO_ENV="production"; $env:DJANGO_SECRET_KEY="..."; # ...all DB_*/key vars
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
gunicorn voting_system.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

## 4. First-run checklist

1. Migrations applied (entrypoint does this).  The historical data
   (2 elections, 50 voters, legacy votes) is converted in-place by
   `ballot/0002` + `elections/0003`; take a DB backup first if migrating a
   live copy (`docs/OPERATIONS.md`).
2. Create the first back-office account:
   `python manage.py createsuperuser` — it can log in at `/watch/` (the admin
   is deliberately not mounted at `/admin/`, and is themed with django-jazzmin;
   `collectstatic` gathers the theme's assets along with the rest).
3. Create organizations and memberships from the UI at
   `/manage/organizations/` (organization + people & access consoles), or with
   the Django ORM/shell.  Creating an organization requires a system
   administrator; `org_admin`s manage within their own subtree.
4. Verify the deployment:
   * `GET /` returns the public election list (200).
   * `GET /elections/<any-election-id>/live/` returns JSON (health check
     with a real election; `/` itself is enough for a basic probe).
   * `GET /watch/login/` returns the admin login page.
5. After every deploy, run an audit-chain verification for one election
   (dashboard → election → Audit, or the tenant-wide **Security & audit**
   overview) to confirm the chain still validates.

## 5. TLS and proxying

Terminate TLS at the proxy and pass `X-Forwarded-Proto: https`;
`SECURE_PROXY_SSL_HEADER` is already configured.  With the default
`SECURE_SSL_REDIRECT=true`, plain-HTTP requests are 301-redirected.
HSTS is enabled for one year including subdomains — do not enable HSTS
before you are sure all subdomains serve HTTPS.

## 6. Backups and disaster recovery

See `docs/OPERATIONS.md` for the backup set that actually preserves
decryptability (database + `secrets/` + `private/credentials/` + the `media`
volume with candidate photographs), restore steps, and the
reconciliation/certification runbook.

## 7. Known deployment limitations

* Credential delivery is out-of-band: voters receive a single-use token by
  an on-screen display (voter access point) or via the credentials CSV that
  operators distribute manually.  There is **no audited download view** for
  that CSV; it is written to `private/credentials/<election>.csv` with
  restrictive permissions.
* Live monitoring is 5-second polling of a JSON endpoint — no websockets
  (Channels is not wired up).
* Idempotent ballot retries (lost response) replay only while the election is
  open; a retry after the election closed returns a refusal (the ballot is
  still counted exactly once — see `docs/BALLOT_SECRECY.md`).
* Exports are JSON (signed tally snapshots, audit chain); no CSV/XLSX/PDF.
