# Architecture

Django **modular monolith**.  One deployable serves the public site, the
voter access point, the staff dashboard, the monitoring pages and the JSON
endpoints.  Referenced by `elections/models.py`.

## Apps

| App | Responsibility |
| --- | --- |
| `organizations` | Tenancy: `Organization` tree + `OrganizationMembership` roles; role predicates (`organizations/roles.py`) that every other app asks for authorization |
| `elections` | Election domain: `Election`, `Position`, `Candidate`, `Voter`, `PollingStation`, approvals; the lifecycle state machine (`elections/lifecycle.py`); voter registry UI |
| `ballot` | Secret-ballot engine: single-use hashed authorizations, short-lived ballot preparations, sealed ballots, the per-election `BallotBox` sequence, receipt lookup |
| `tally` | Deterministic recount, versioned signed `TallySnapshot`s, reconciliation, certification/decertification, results visibility |
| `audit` | Hash-chained append-only event log, chain verification, signed checkpoints, JSON export |
| `representatives` | Candidate self-application, representative accreditation, station incidents/objections |
| `monitoring` | Live status JSON (5 s polling), monitor page, per-org dashboard, audit export |
| `config` / `voting_system` | Settings profiles (`config/settings/{base,local,production,test}.py` selected by `DJANGO_ENV`, incl. the `jazzmin` back-office theme), Celery app, root URLconf (`voting_system.urls`) |

The dashboard is at `/dashboard/…`, the Django admin is mounted at `/watch/`
(not `/admin/`) and is themed with **django-jazzmin**, the voter-facing access
point at `/elections/<id>/access/` and the ballot at `/ballot/<token>/`.

## Tenancy

Every `Election` belongs to an `Organization`.  Organizations form a tree
(`parent`); roles are memberships on an organization:

* `org_admin` — **institution election administrator**; full control of its
  own organization subtree only;
* `ec_chair` — **election-commission chair**; election oversight for the
  assigned organization subtree (lifecycle, readiness, officials, escalations,
  reconciliation, and certification/publication where the election governance
  policy assigns it);
* `election_official`, `auditor` — election staff
  (`organizations.roles.is_election_staff`);
* `observer` — read-only monitoring, never staff surfaces.

A Django `is_superuser` is the **Super Admin**: unrestricted, system-wide, and
never required to obtain another person's approval.  `EC_CHAIR_SYSTEM_SCOPE_USERNAMES`
(settings/env) is an explicit grant of system-wide election/people oversight to
named chairs — it never grants tenant lifecycle administration, which stays
with the Super Admin.

`role_in(user, org)` matches membership on the organization **or any
ancestor**, and `organizations_for(user)` expands to the whole managed
subtree, so a regional admin sees everything beneath their node.  Result
publication additionally requires `can_view_results_early` (privileged
staff roles), which also gates audit export.

The management console is gated by `organizations.roles.console_required`,
which admits the Super Admin and any console-role membership regardless of the
Django `is_staff` flag.  Anonymous visitors are redirected to the admin sign-in
page; an authenticated user without console access receives a 403 that names
the missing permission.  Candidate representatives keep their own read-only
surface and never reach console or staff-only material.

## Election lifecycle

`Election.state` is a backend-enforced state machine
(`elections/lifecycle.py`):

```
DRAFT → REGISTRATION → CONFIG_REVIEW → APPROVED → SCHEDULED → OPEN
      → CLOSED → RECONCILIATION → REVIEW → CERTIFIED → PUBLISHED → ARCHIVED
```

* All transitions go through `transition()`; views never set `state` directly.
  Open/close also happen automatically from the schedule (`sync_election_state`).
* Three transitions require **four-eyes dual control** and are refused on the
  direct action endpoint: `approve_config`, `reopen`, `decertify`.  They are
  driven by `ElectionApproval` records where the requester may not be the
  decider, a reason is mandatory, and approvals are invalidated when their
  target state changes.  The Super Admin is exempt: they may decide an
  approval they raised (never blocked by another person's approval).
* Each election carries a **governance policy** in `Election.rules["governance"]`
  (`elections/governance.py`): `certify_authority` / `publish_authority`
  (which role may certify/publish), `require_institution_approval`, and
  per-edge `dual_control` waivers.  Defaults preserve the pre-governance
  behaviour, and because `rules` is excluded from the configuration hash, a
  policy change never invalidates a frozen election configuration.
* The Super Admin has an explicit, confirmed **emergency override**
  (`elections.lifecycle.admin_override`, `POST …/override/`): a mandatory
  reason, an `OVERRIDE` confirmation token, and an `admin_override` audit
  event that records the actor, target and before/after state.  Every other
  role is refused, and illegal edges are still rejected.
* Certification (`tally.services.certify_latest`) re-verifies reconciliation
  **live** at certification time.  Failure codes: `reconciliation_failed`
  (ballot box drifted), `tally_missing`, `approval_invalid`,
  `snapshot_superseded`, `snapshot_stale` (a certified snapshot no longer
  matches the current recomputation).

## Super Admin Control Center

`/manage/control/` (`organizations/control_center.py`) is the system-wide
console for the Super Admin only: health checks, operational alerts, role and
object counts, a cross-tenant search (`/manage/control/search/`), an override
ledger (`/manage/control/overrides/`) and recovery actions
(`/manage/control/recovery/` — restore console access, toggle an
organization, clear the config cache, unlock a user).  Every action is
audited; non-superusers receive a 403.

## Ballot data flow (secret ballot)

```
voter id ──issue_authorization──▶ BallotAuthorization (HMAC hash only)
   token ──GET /ballot/<token>/─▶ BallotPreparation (short-lived capability)
   token+choices ──POST────────▶ transactional cast:
        select_for_update() on the election's BallotBox row → box_seq
        Ballot(encrypted selections, receipt, NO voter link)
        BallotPreparation deleted, authorization marked used
        audit: ballot_accepted{box_seq, ballot_id, ballot_hash}
```

Identity (`BallotAuthorization`/`Voter`) and content (`Ballot`) live in
different tables joined by nothing; exactly-once acceptance is enforced by
the single-use authorization, the idempotent `submission_id` and the
row-locked ballot-box sequence.  Details and limitations:
`docs/BALLOT_SECRECY.md`.

## Tally, snapshots, reconciliation

* `tally.services.compute_tally` walks sealed ballots in `box_seq` order and
  is deterministic — a recount reproduces the identical result.
* `TallySnapshot` rows are versioned and hash-linked; a new snapshot after
  certification **supersedes** (never overwrites) the certified one and forces
  decertification first.
* Snapshots are signed (Ed25519, HMAC fallback) and exported as JSON
  (`/elections/<id>/results/v<n>/export/`);
  `/elections/<id>/results/v<n>/verify/` recomputes counts and checks
  signatures.
* Reconciliation invariant: `box.last_seq == accepted ballots ==
  valid + invalid ballots == voters who voted`.

## Audit

Every mutating action records an event into a **per-election hash chain**
(SHA-256 over seq + prev-hash + normalized event fields), with a separate
global chain.  `record()` refuses fields that could carry ballot content or
credentials (allowlist/denylist enforced), and IP addresses are stored only
as keyed hashes.  Chains are verifiable at any time
(`audit.services.verify_chain`, election dashboard, JSON export).

## Settings and environments

`DJANGO_ENV` selects `local` (SQLite, DEBUG), `production` (PostgreSQL,
HTTPS, fail-fast on missing secrets) or `test`.  `voting_system/settings.py`
is a compatibility shim that imports the local profile.  Redis is optional
(LocMem fallback); Celery is optional (eager fallback) and currently has no
tasks.

## Legacy code (not deployed)

The pre-rebuild package `apps/` (voters, ballots, tally, orgs, accounts,
monitoring, core) and `config/urls.py` are **dead code**: they are not in
`INSTALLED_APPS` and nothing routes to them.  They are retained only as a
historical reference of the previous implementation and can be deleted in a
cleanup pass; do not import from them.
