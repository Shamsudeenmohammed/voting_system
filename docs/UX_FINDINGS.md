# UX / operability audit — findings and fixes

A short internal record of the root causes found in the real codebase (not a
mockup review) and what was changed.  See `docs/ARCHITECTURE.md` for the
domain model.

## Root causes found

1. **Election lifecycle has no user interface (critical).**
   `elections.views.election_config` builds `transitions`, `pending_approvals`,
   `approval_form`, `stations`, `station_form` and `can_approve`, but
   `templates/elections/election_config.html` rendered none of them.  There was
   no button anywhere to open registration, submit for configuration review,
   approve, schedule, open, close, reconcile, certify, publish or archive an
   election — nor to create/decide the four‑eyes approvals those transitions
   require.  As a result an election could only progress through the Django
   admin or the shell.  This is why even a Super Admin "could not reliably
   perform all required tasks".

2. **No organisation / user‑access management UI.**
   The `organizations` app had models + role predicates only (no `views.py`,
   `urls.py`, `templates/` or registered admin).  Creating tenants, campuses
   and role memberships required the Django shell.  The Super Admin could not
   onboard officials from the product.

3. **No task‑oriented navigation.**
   The whole app was reachable from a short top bar with 3–6 links.  There
   were no navigation groups, no role awareness, no active‑page highlight for
   election sub‑pages and several duplicate/ambiguous links.

4. **Confusing lifecycle terminology and no prerequisites.**
   States like `config_review`, `reconciliation` and `review` were shown with
   no explanation of what was missing or who had to act next.

5. **Every dashboard was the same generic list.** No "where am I / what needs
   attention / what next" summary, no cross‑election operations or approvals
   inbox, no readiness information, no voter guidance.

6. **Hidden `is_staff` coupling.** All management views use
   `@staff_member_required`, but the tenancy model is `OrganizationMembership`.
   A membership alone did not grant console access, and nothing said so.

7. **Fragile routing.** `voting_system/urls.py` included `monitoring.urls`
   twice and mixed includes at the root; the ballot app was not mounted (it is
   included via `elections`? — see note) — reverse lookup happened to work but
   the layout was confusing.

8. **Election creation silently guessed the tenant** (`orgs.first()`), risking
   an election in the wrong organisation.

9. **Polling stations were unreachable** from the UI although monitoring and
   the representative dashboard depend on them.

## Fixes implemented

* A task‑oriented, role‑aware sidebar (`organizations/navigation.py` +
  `templates/base.html`) with collapsible groups, automatic active‑group
  expansion, current‑page highlight and a mobile drawer.
* A full organisation & people‑access console (`organizations/views.py`,
  `urls.py`, templates) and registered Django admin, so Super Admins can create
  organisations and assign roles end to end.
* A rebuilt election workspace (`templates/elections/election_config.html`)
  that renders the readiness checklist, the state‑machine actions (with role
  gating), the polling‑station manager and the four‑eyes approval panel —
  restoring end‑to‑end operability while leaving every server‑side guard in
  `elections/lifecycle.py` untouched.
* Cross‑election console pages: **Needs attention** (readiness), **Approvals**
  inbox, **Audit overview**, **Results & reports** and **System settings**.
* Readiness service (`elections/readiness.py`) that explains exactly what is
  missing, who is responsible and the next action — surfaced on the workspace,
  the operations page and the dashboards.
* Role‑aware dashboards that answer "where am I / what needs attention / what
  next", plus voter guidance and participation confirmation on the home page.
  The representative dashboard now leads with a *What you can do* panel and a
  *My reports* list (issue status at a glance) instead of bare links.
* Safer election creation that requires an explicit organisation.
* Consolidated URL layout.

## Guided election setup

The workspace (`templates/elections/election_config.html`) now presents the
operational path as explicit, validated steps with a live readiness banner and
a single recommended next action:

1. **Basic information** — title, category, schedule, visibility.
2. **Ballot setup** — positions and candidates (DRAFT only), each checked for a
   contestable candidate.
3. **Polling stations** — on‑site/online/hybrid stations for monitoring.
4. **Voter registry** — add/import voters during REGISTRATION, credentials
   issued at freeze.
5. **Configuration & approval** — submit for review, then the four‑eyes
   `approve_config` request/decision panel.
6. **Schedule / open** — schedule, then open voting when ready.

Every step is also enforced server‑side in `elections/lifecycle.py`; the
workspace only explains and routes.  A direct attempt to skip a step (or a
`certify`/`open`/`close` on a certified election) is refused with a safe
message and the state is unchanged.

## End‑to‑end coverage

`elections/test_e2e.py` drives one election through the complete browser path —
create → structure → registration → review → four‑eyes approve → schedule →
open → cast a ballot → close → reconcile → certify → publish → archive — and
asserts the safety invariants at the HTTP boundary (four‑eyes, registry lock,
readiness gating, published results visibility and "certified results are never
silently overwritten").

All security controls (ballot secrecy, tenant isolation, duplicate‑vote
prevention, lifecycle enforcement, four‑eyes approvals, audit chaining) are
preserved unchanged.

## Test coverage added

* `organizations/tests.py::ConsoleViewTests` — HTTP tests for the tenancy
  console: scoped listing, system‑admin‑only creation, subtree‑only editing,
  activation toggle, role grant/revoke with `is_staff` synchronisation and
  account creation.
* `elections/test_console.py` — navigation, cross‑election consoles and the
  workspace rendering.
* `elections/test_e2e.py` — the full guided lifecycle over HTTP.
* `representatives/tests.py` — the representative dashboard's guidance panel,
  report list and live‑turnout visibility.
* `organizations/test_admin_theme.py` — the django-jazzmin back office: the
  custom sign‑in page, staff authentication through it, the themed admin shell
  (Jazzmin layout + custom stylesheet + per‑model icons) and the registered
  changelists rendering.

## Back‑office theme (django‑jazzmin)

The Django admin at `/watch/` is skinned with **django-jazzmin** so the
back office matches the product UI rather than the stock Django look:

* `JAZZMIN_SETTINGS` / `JAZZMIN_UI_TWEAKS` in `config/settings/base.py` set the
  brand, the icon side‑menu (grouped and ordered by app: Elections →
  Organisations → Audit → Auth), top‑menu links back into the product console
  and the horizontal‑tab change forms.  `BRAND_NAME` (and the `JAZZMIN_*`
  overrides) are read from the environment, so deployments can rebrand without
  code changes.
* `static/css/admin_theme.css` re‑maps the palette to the product tokens
  (Inter typeface, indigo primary, navy sidebar, soft borders, rounded cards and
  buttons).  It loads *after* the Jazzmin/AdminLTE stylesheets, so it only
  restyles — it changes no behaviour or permissions.
* `templates/admin/login.html` replaces Jazzmin's sign‑in page with a modern,
  split‑panel layout (brand panel + sign‑in card, password reveal, error
  handling) that continues to honour `next` deep links into the admin.
* `static/img/brand-mark.svg` is the shared brand mark used for the site logo,
  favicon and the sign‑in panel.

Ballot‑secrecy is unaffected: no ballot/tally model is registered in the admin,
and the existing admin guards (frozen‑configuration refusal, append‑only audit,
no delete for voters who have voted) are unchanged.
