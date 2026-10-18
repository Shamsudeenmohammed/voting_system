# Voting System

A multi-tenant, **secret-ballot** election management platform built with
Django.  One election's whole lifecycle is supported: voter registration →
configuration review → scheduling → open voting → reconciliation →
certification → publication → archive, with a backend-enforced state machine,
four-eyes approvals for the sensitive transitions, an append-only audit
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
suite (102 tests, all green).

| Area | Status | Notes |
| --- | --- | --- |
| Secret ballot engine (single-use hashed credentials, one-time preparation capability, encrypted ballots with **no voter link**, row-locked ballot-box sequence, idempotent retries, receipt lookup) | **Implemented, tested** (14 tests incl. threaded-concurrency and double-vote attempts) | `docs/BALLOT_SECRECY.md` |
| Election lifecycle state machine (DRAFT→…→ARCHIVED) with server-side transition rules and automatic open/close | **Implemented, tested** | `elections/lifecycle.py` |
| Four-eyes dual control for approve-config / reopen / decertify (requester ≠ decider, mandatory reason, stale-approval invalidation) | **Implemented, tested** | incl. HTTP-level flow tests |
| Voter registry (single add, pasted-CSV bulk add up to 5000 rows, blank-ID tolerance, revocation) | **Implemented, tested** | XLSX/file upload **not** implemented |
| Tally: deterministic recount, versioned hash-linked signed snapshots (Ed25519), live-reverified certification, decertify-with-supersede, JSON export, public verify endpoint | **Implemented, tested** (8 tests) | CSV/XLSX/PDF export **not** implemented |
| Audit: per-election + global hash chains, chain verification, signed checkpoints, selection-free event allowlist, privileged JSON export | **Implemented, tested** (7 tests) | no CLI management command — verification is via dashboard/endpoints |
| Tenancy: organization tree, role memberships (org_admin / election_official / auditor / observer), ancestor/descendant scoping, staff-vs-observer separation | **Implemented, tested** (13 tests) | |
| Organization/membership **management UI** | **Not implemented** | manage via Django shell/ORM; orgs are not even registered in the admin |
| Representatives: public self-application, staff decide/revoke, accreditation, station incidents (evidence upload, severity, resolution) | **Implemented, tested** (13 tests) | dashboard text promises notifications — **no email/SMS is ever sent** |
| Monitoring: 5-second polling live-status JSON, monitor page, per-org dashboard, audit export, turnout-withholding while open | **Implemented, tested** (7 tests) | polling, **no websockets** (Channels is pinned but not wired up) |
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
   is intentionally not at `/admin/`).
2. Create an organization and memberships in the Django shell, attach an
   election to it, and drive it from **`/dashboard/`**.
3. Voters use `/elections/<id>/access/` to obtain a single-use credential,
   then `/ballot/<token>/` to vote; observers watch `/elections/<id>/monitor/`.

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
Ran 102 tests in ~15s
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
