# Voting System

A multi-tenant, **secret-ballot** election management platform built with
Django.  One election's whole lifecycle is supported: voter registration →
configuration review → scheduling → open voting → reconciliation →
certification → publication → archive, with a backend-enforced state machine,
four-eyes approvals for the sensitive transitions (with an explicit, audited
Super Admin override), a per-election governance policy, an append-only audit
chain, deterministic signed tallies and live monitoring.

**Documentation map**

| Document | Contents |
| --- | --- |
| `docs/ARCHITECTURE.md` | Apps, tenancy model, lifecycle, ballot data flow |
| `docs/BALLOT_SECRECY.md` | Secrecy model, threat model, explicit limitations |
| `docs/DEPLOY_ONLINE.md` | Production deployment (Docker and bare metal) |
| `docs/OPERATIONS.md` | Backup/restore, key custody, verification runbook |
| `.env.example` | Every environment variable, documented |

## Feature status

Honest inventory of this codebase.  "Tested" means covered by the automated
suite (279 tests, all green).

| Area | Status | Notes |
| --- | --- | --- |
| Secret ballot engine (single-use hashed credentials, one-time preparation capability, encrypted ballots with **no voter link**, row-locked ballot-box sequence, idempotent retries, receipt lookup) | **Implemented, tested** (14 tests incl. threaded-concurrency and double-vote attempts) | `docs/BALLOT_SECRECY.md` |
| Election lifecycle state machine (DRAFT→…→ARCHIVED) with server-side transition rules and automatic open/close | **Implemented, tested** | `elections/lifecycle.py` |
| Four-eyes dual control for approve-config / reopen / decertify (requester ≠ decider, mandatory reason, stale-approval invalidation) | **Implemented, tested** | incl. HTTP-level flow tests |
| Voter registry (single add, pasted-CSV bulk add up to 5000 rows, blank-ID tolerance, revocation, downloadable CSV/XLSX register template, file upload of CSV/TXT/XLSX registers) | **Implemented, tested** | `elections/voter_import.py`, `RegistryImportTests` |
| Tally: deterministic recount, versioned hash-linked signed snapshots (Ed25519), live-reverified certification, decertify-with-supersede, JSON export, public verify endpoint | **Implemented, tested** (8 tests) | CSV/XLSX/PDF export **not** implemented |
| Audit: per-election + global hash chains, chain verification, signed checkpoints, selection-free event allowlist, privileged JSON export | **Implemented, tested** (7 tests) | no CLI management command — verification is via dashboard/endpoints |
| Tenancy: organization tree, role memberships (org_admin / ec_chair / election_official / auditor / observer), ancestor/descendant scoping, staff-vs-observer separation | **Implemented, tested** (13 tests) | |
| Authority model: Super Admin (system-wide, never blocked by another person's approval, confirmed+audited emergency override), election-commission chair oversight, institution-scoped admins | **Implemented, tested** | `organizations/roles.py`, `elections/lifecycle.py` (`admin_override`), `elections/governance.py`, `organizations/test_authority.py`, `elections/test_authority.py` |
| Super Admin **Control Center**: system-wide health/alerts, cross-tenant search, override ledger, recovery actions (restore console access, toggle organization, clear config cache, unlock user) | **Implemented, tested** | `organizations/control_center.py`, `/manage/control/`; non-superusers get 403 |
| Per-election **governance policy** (certify/publish authority, institution-approval requirement, per-edge dual-control waivers) editable while DRAFT | **Implemented, tested** | `Election.rules["governance"]`, excluded from the frozen config hash |
| Organization & people‑access **management UI** (create/edit organizations, grant/revoke roles, staff-flag sync) | **Implemented, tested** (12 HTTP tests) | also registered in the Django admin; system‑admin creates orgs, org_admins manage within their subtree |
| Role‑aware navigation + guided election workspace (task‑oriented sidebar, readiness checklist with "what's missing / who acts / next", step‑by‑step setup, four‑eyes inbox, cross‑election operations/results/audit consoles) | **Implemented, tested** | `organizations/navigation.py`, `elections/readiness.py`, `elections/test_console.py`, `elections/test_e2e.py` |
| Back‑office theme (**django-jazzmin**): branded admin dashboard at `/watch/`, modernised sign‑in page, icon side‑menu grouped by app, and a stylesheet matched to the product palette | **Implemented, tested** (7 tests) | `JAZZMIN_SETTINGS` in `config/settings/base.py`, `static/css/admin_theme.css`, `templates/admin/login.html`; `organizations/test_admin_theme.py` |
| Representatives: public self-application, staff decide/revoke, accreditation, station incidents (evidence upload, severity, resolution) | **Implemented, tested** (13 tests) | dashboard text promises notifications — **no email/SMS is ever sent** |
| Monitoring: 5-second polling live-status JSON, monitor page, per-org dashboard, audit export, full-screen live results board (candidate photos, live vote percentages, 3-second polling), turnout-withholding while open | **Implemented, tested** (14 tests) | polling, **no websockets** (Channels is pinned but not wired up); board access follows the election's results-visibility policy |
| Rate limiting (per-IP throttles on the ballot page and the cast POST) | **Implemented, tested** | cache-based; needs Redis cache for multi-worker correctness.  `RATE_LIMIT_VOTER_LOGIN` / `RATE_LIMIT_REPORT_DOWNLOAD` settings are defined but **unused** (not implemented) |
| Deployment: settings profiles with production fail-fast checks, hardened HTTPS headers, WhiteNoise static, Dockerfile + entrypoint + docker-compose, `.env.example` | **Implemented, not integration-tested here** | configuration provided and documented (`docs/DEPLOY_ONLINE.md`); no CI/Docker in this environment |
| Celery background processing | **Partial — scaffolding only** | broker config + eager fallback exist; there are zero tasks (nothing runs in the background by design) |
| Voter credential delivery | **Partial** | one-time on-screen display + operator-side CSV in `private/credentials/`; no audited download view, no email/SMS |
| End-to-end verifiable mixnet-style voting, coercion resistance | **Not implemented / out of scope** | see threat model in `docs/BALLOT_SECRECY.md` |

## Quick start (local, Windows/macOS/Linux)

```powershell
python -m venv .venv
.\.venv\Scripts\activate        # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt

python manage.py migrate        # also converts legacy data in place
python manage.py runserver      # SQLite, DEBUG on, keys auto-created under secrets/
```

Then:

1. `python manage.py createsuperuser` and log in at **`/watch/`** (the admin
   is intentionally not at `/admin/` and is themed with django-jazzmin).
2. Open **`/dashboard/`** to manage elections, or **`/manage/organizations/`**
   to create organizations and assign roles from the UI (no shell needed).
   A Super Admin also gets **`/manage/control/`** (system health, cross-tenant
   search, override ledger, recovery actions).
3. Voters use `/elections/<id>/access/` to obtain a single-use credential,
   then `/ballot/<token>/` to vote; observers watch `/elections/<id>/monitor/`.
   For the engine-room projector, open
   `/elections/<id>/monitor/board/`; staff can always view it, while
   representatives follow the election's results-visibility policy.

## Quick start (Docker)

```bash
cp .env.example .env    # fill in secrets — see docs/DEPLOY_ONLINE.md
docker compose up -d --build
```

## Running the tests

```powershell
$env:DJANGO_SETTINGS_MODULE = "config.settings.test"
$env:TEST_KEEP_DB = "1"          # optional: reuse the file-based test DB
python manage.py test --keepdb
```

Expected result:

```
Ran 279 tests in ~46s
OK
```

Test DB is file-based (`.test_dbs/test_voting.sqlite3`) so the threaded
concurrency test works; never point tests at `db.sqlite3` (it holds real
data — 2 elections, 50 voters, legacy ballots, converted by migrations
`ballot/0002` + `elections/0003`).

## Known limitations and deliberate decisions

* **Idempotent replay needs an open election.** Retrying a lost ballot
  response replays the original receipt only while the election is open; a
  retry after close is refused with the uniform 403 wording.  The ballot is
  still counted exactly once (`docs/BALLOT_SECRECY.md` §Limitations).
* **`snapshot_stale`**: certification re-verifies the tally at the moment of
  certification; if a snapshot no longer matches the recomputation the
  lifecycle fails with `snapshot_stale` and nothing is certified.
* **Reconciliation is enforced**, not advisory: certification refuses when
  `ballot-box seq ≠ accepted ballots ≠ valid + invalid` (`reconciliation_failed`).
* **Key loss is fatal** (encrypted ballots become spoilage); rotation of the
  ballot key or token pepper is destructive by design
  (`docs/OPERATIONS.md`).
* **Legacy code retained but dead**: the pre-rebuild package `apps/` and
  `config/urls.py` are not in `INSTALLED_APPS` and not routed anywhere.
  Kept as reference; safe to delete in a cleanup pass.
* Celery/Redis are optional; everything important runs transactionally in
  the request.  No feature silently degrades into an unsafe mode —
  production settings **refuse to start** without required secrets,
  PostgreSQL and a broker (or explicit eager mode).
