# Permission Matrix

This matrix documents the authoritative permissions for every role across all active modules.  
The source of truth is `organizations/roles.py` (role predicates) + the view-level gates in each app.  
**Nothing is enforced in templates alone — every check is a server-side predicate.**

---

## Role Definitions

| Role | Django flag / membership | Scope |
|------|--------------------------|-------|
| **Super Admin** | `is_superuser == True` | **System-wide** — no scoping, no approvals required, never blocked by another person's decision. |
| **EC Chair** | `role="ec_chair"` on an organization (or its root) | Institution subtree — election oversight, lifecycle, officials, readiness, escalated exceptions, certification/publication where the election governance policy assigns it. |
| **Institution Admin** | `role="org_admin"` on an organization (or its root) | Institution subtree — full election control incl. approvals, certification, reopen, governance policy, tenant lifecycle (create/rename/toggle orgs). |
| **Election Official** | `role="election_official"` on an organization (or its root) | Institution subtree — configuration, voter registry, lifecycle execution, monitoring, incidents. **Ballot secrecy**: issues credentials, sees turnout, **never** ballot selections. |
| **Auditor** | `role="auditor"` on an organization (or its root) | Institution subtree — strictly read-only: dashboards, results, audit chain verification, authorized exports. |
| **Observer** | `role="observer"` on an organization (or its root) | Public data only; never reaches management console. |
| **Candidate Representative** | `Representative` row, `status="approved"` | **Own candidate** in **own election** only — aggregate monitoring, own incidents. Never staff surfaces, never voter identity, never ballot content. |

> **EC Chair system-wide grant** (`EC_CHAIR_SYSTEM_SCOPE_USERNAMES`): explicit list of chair usernames that get election/people oversight across *every* tenant. Does **not** grant tenant lifecycle (org create/rename/toggle), which stays with the Super Admin.

---

## Election Lifecycle & Configuration

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Create election | ✅ | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ | ❌ |
| View election (internal) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (public only) | ✅ (own election only) |
| Edit election metadata | ✅ (DRAFT only) | ✅ (DRAFT, in scope) | ✅ (DRAFT, in scope) | ✅ (DRAFT, in scope) | ❌ | ❌ | ❌ |
| Edit ballot structure (positions/candidates) | ✅ (DRAFT only) | ✅ (DRAFT, in scope) | ✅ (DRAFT, in scope) | ✅ (DRAFT, in scope) | ❌ | ❌ | ❌ |
| Edit polling stations | ✅ (DRAFT→SCHEDULED) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Manage voters | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Submit for approval (CONFIG_REVIEW) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| **Decide approval** (four-eyes) | ✅ (never blocked) | ✅ (in scope, not own) | ✅ (in scope, not own) | ❌ | ❌ | ❌ | ❌ |
| **Emergency override** (any transition) | ✅ (`OVERRIDE` token, reason ≥ 10 chars, audited) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Permanent deletion** (break-glass) | ✅ (`DELETE` token, reason ≥ 10 chars, audited) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Archive election | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Change governance policy | ✅ (DRAFT only) | ✅ (DRAFT, in scope) | ✅ (DRAFT, in scope) | ❌ | ❌ | ❌ | ❌ |
| Certification authority (per-election policy) | ✅ (always) | ✅ (if policy assigns) | ✅ (if policy assigns) | ❌ | ❌ | ❌ | ❌ |
| Publication authority (per-election policy) | ✅ (always) | ✅ (if policy assigns) | ✅ (if policy assigns) | ❌ | ❌ | ❌ | ❌ |

---

## Voting Operations

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Open election (schedule) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Close election (manual) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Cancel election | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Reopen election | ✅ (four-eyes or override) | ✅ (four-eyes, in scope) | ✅ (four-eyes, in scope) | ❌ | ❌ | ❌ | ❌ |
| Recount | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Certify results | ✅ (always) | ✅ (if policy assigns, in scope) | ✅ (if policy assigns, in scope) | ❌ | ❌ | ❌ | ❌ |
| Decertify | ✅ (four-eyes or override) | ✅ (four-eyes, if policy assigns) | ✅ (four-eyes, if policy assigns) | ❌ | ❌ | ❌ | ❌ |
| Publish results | ✅ (always) | ✅ (if policy assigns, in scope) | ✅ (if policy assigns, in scope) | ❌ | ❌ | ❌ | ❌ |

---

## Ballot & Tally (Secret Ballot Guarantees)

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Issue voter credentials | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| View turnout (live) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ (until closed) | ❌ (until closed) |
| View turnout (closed) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ | ✅ (own election) |
| View interim results | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| View certified/published results | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ | ✅ (own election) |
| Export tally snapshot | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ |
| Verify tally snapshot | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (published only) | ✅ (own, published only) |
| **Access individual ballot** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Link voter → ballot** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

> **Secret-ballot invariant**: `Ballot` has no voter FK. No view ever joins voter identity with selections. Audit `FORBIDDEN_DETAIL_KEYS` rejects any event carrying ballot content or credentials.

---

## Representatives & Incidents

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Apply as representative | ✅ (if voter) | ✅ (if voter) | ✅ (if voter) | ✅ (if voter) | ✅ (if voter) | ✅ (if voter) | — (is the role) |
| **Decide applications** (approve/revoke) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| Accredit representative | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |
| View all representatives | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ (own only) |
| View all incidents | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ (own only) |
| Create incident | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ✅ (own election) |
| Resolve incident | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ |

---

## Audit & Monitoring

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| View election audit log | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ |
| Export audit chain (JSON) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ |
| Verify audit chain | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ |
| Create audit checkpoint | ✅ | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ | ❌ |
| Live monitoring (JSON) | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ✅ (own election, no turnout while open) |
| Monitoring dashboard | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ✅ (own election) |
| Org-level dashboard | ✅ | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ |

---

## People & Access Management

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Create organization | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Rename / toggle organization | ✅ | ❌ | ✅ (in scope) | ❌ | ❌ | ❌ | ❌ |
| Grant any role | ✅ | ✅ (official, observer only) | ✅ (any) | ❌ | ❌ | ❌ | ❌ |
| Revoke any role | ✅ | ✅ (official, observer only) | ✅ (any) | ❌ | ❌ | ❌ | ❌ |
| Create user account | ✅ | ✅ (in scope) | ✅ (in scope) | ❌ | ❌ | ❌ | ❌ |
| Unlock user account (recovery) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Restore console access (recovery) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Clear config cache (recovery) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## System Settings & Control Center

| Action | Super Admin | EC Chair | Institution Admin | Election Official | Auditor | Observer | Representative |
|--------|-------------|----------|-------------------|-------------------|---------|----------|----------------|
| Control Center (health/alerts) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| System-wide search | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Override ledger | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Recovery actions | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| System settings | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Enforcement Notes

1. **Every gate is a predicate in `organizations/roles.py`** — `is_system_admin`, `can_manage_election`, `can_approve_election`, `can_oversee_election`, `can_view_election`, `can_view_results_early`, `is_election_staff`, `organizations_for`, `admin_organizations_for`, `people_organizations_for`.
2. **Views call the predicate; templates only use it for display hints**. Hiding a button is never the enforcement mechanism.
3. **Super Admin bypasses**:
   - Four-eyes: may decide their own approval (`can_approve` check includes `is_system_admin`).
   - Lifecycle: `admin_override` with explicit `OVERRIDE` token + documented reason.
   - Deletion: `election_delete` with explicit `DELETE` token + documented reason.
   - Scope: `organizations_for`, `admin_organizations_for`, `people_organizations_for` return **all** organizations for `is_system_admin`.
4. **Permanent deletion vs. archival vs. cancellation vs. result invalidation** are distinct:
   - **Archive**: reversible, election row retained, state = `ARCHIVED`.
   - **Cancel**: lifecycle transition to `CANCELLED` (if modeled), retains data.
   - **Invalidate results**: decertify + new snapshot supersedes; data retained.
   - **Permanent delete**: break-glass, `DELETE` token, reason ≥ 10 chars, audit event `election_deleted`, election row + related rows removed in dependency order, audit chain preserved via `chain_key` (`election:<uuid>`), `AuditEvent.election` FK set to `NULL` (on_delete=SET_NULL) — chain verifies post-deletion.
5. **Ballot secrecy**: `Ballot` never has a voter FK. `BallotAuthorization` (identity) and `Ballot` (content) are joined by nothing. No routine voter→ballot lookup exists. Audit `FORBIDDEN_DETAIL_KEYS` drops any event that tries to carry ballot content or credentials.

---

## Test Coverage (Automated Assertions)

- `elections.test_delete.SuperAdminDeleteTests` (13 tests):
  - Confirmation page renders for Super Admin; 403 for every other role.
  - `DELETE` token + reason (≥10 chars) enforced server-side.
  - Deletion with related records (ballots, tally, representatives, approvals, stations, positions, voters).
  - Audit event recorded with actor, timestamp, election id, outcome, reason.
  - Audit chain verifies post-deletion with nulled FK.
  - Non-superusers: 403 + election inert.
  - Unauthenticated: redirect to login.
  - Out-of-scope election: 403.
  - Super Admin can delete unrelated election by direct URL.
  - Election with ballots: permitted + audited.
  - Config POST cannot delete.

- `elections.test_authority.AuthorityFixture` derivatives:
  - Super Admin override endpoint: 403 for non-superusers; requires `OVERRIDE` token + reason.
  - Governance policy: 403 for non-admin/chair; locked outside DRAFT.
  - Approvals: four-eyes enforced; Super Admin exempt from "not own request" rule.
  - Representatives: read-only surface; cannot reach staff endpoints.

- `audit.tests`: chain verification survives deletion (FK nulled, chain_key intact).

- `organizations.tests`: role predicates, console gating, people management scope.

- **Total suite**: 230 tests, all passing (`Ran 230 tests ... OK`).

---

## Remaining Guardrails (Intentionally Not Relaxed)

| Guardrail | Rationale |
|-----------|-----------|
| Election structure (positions/candidates) editable **only in DRAFT** | Data-integrity protection: frozen config hash, voter credentials issued against a known ballot. Not evaluated as an accidental Super Admin block. |
| `admin_override` still requires `OVERRIDE` token + reason ≥ `MIN_OVERRIDE_REASON` | Auditability: every override is explicit, confirmed, documented. |
| `election_delete` requires `DELETE` token + reason ≥ 10 chars | Break-glass must be deliberate; prevents accidental/malicious deletion. |
| No voter→ballot linkage, ever | Secret-ballot invariant — absolute. |
| Audit `FORBIDDEN_DETAIL_KEYS` blocks ballot content in events | Tamper-evident log must never carry secret data. |

---

## Quick Reference: Super Admin "Never Blocked" Checklist

- [x] Create / view / edit / archive / restore / **permanently delete** elections
- [x] Manage institutions, organizations, users, roles, permissions
- [x] Manage voters, candidates, ballot config, polling stations, schedules
- [x] Override **any** workflow restriction (explicit token + reason + audit)
- [x] Manage opening / closing / cancellation / reopening / results / certification / publication
- [x] Manage system settings, audit, security, recovery (Control Center)
- [x] No institution-scope restriction
- [x] No ownership restriction
- [x] No approval / four-eyes restriction (may self-decide)
- [x] No role-based restriction (bypasses all `role_in` checks)
- [x] All overrides and deletions audited with actor, timestamp, election id, outcome, reason