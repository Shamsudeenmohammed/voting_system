"""Role predicates used by every view, API, service and template in the platform.

This module is the single source of truth for *who may do what*.  Views and
services call these predicates; templates may only use them to decide what to
render (hiding a button is never the enforcement mechanism).

Roles (``OrganizationMembership.role``)
---------------------------------------
* ``is_superuser`` (a.k.a. **Super Admin** / system administrator)
      -> everything, system-wide.  A Super Admin is never scoped by an
         organization membership and never has to wait for another person's
         approval; sensitive overrides are recorded in the audit chain instead.
* ``ec_chair``  -> **Election commission chair**.  Oversees the elections of the
      organization(s) they are assigned to (a membership at an institution/root
      organization covers its whole subtree): manage lifecycle, readiness,
      schedules and polling stations, appoint/replace officials, resolve
      escalated operational issues, decide permitted exceptions, oversee
      reconciliation and - when the election governance policy assigns it -
      certify and publish results.
* ``org_admin`` -> **Institution election administrator**.  Full control of its
      organization's elections, including approvals/certification/reopen
      decisions, within its own tenant subtree only.
* ``election_official`` -> configuration, registry, lifecycle execution,
      monitoring and incidents for its organization.  Ballot secrecy: officials
      issue credentials and see turnout, never ballot selections.
* ``auditor``   -> strictly read-only: dashboards, results, audit chain
      verification and authorized exports.
* ``observer``  -> public data only; no management console.
* Representatives get their own restricted surface (see ``representatives``);
      they are never organization members unless separately assigned, and they
      may never read private voter records or individual ballot selections.

Scope
-----
* A membership may be granted at any organization in a tree.  ``role_in``
  matches a membership on the election's own organization **or its root**, so a
  chair/admin at an institution/root covers the institution (its subtree).
* ``EC_CHAIR_SYSTEM_SCOPE_USERNAMES`` (settings/env) is an *explicit* grant of
  system-wide oversight to named chairs.  It grants election and people
  oversight across every tenant; it does **not** grant organization lifecycle
  administration (creating/renaming tenants) which stays with the Super Admin.
"""

from __future__ import annotations

import functools

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import resolve_url

from .models import Organization, OrganizationMembership

# ---------------------------------------------------------------------------
# Role vocabulary (do not duplicate these strings elsewhere)
# ---------------------------------------------------------------------------

ORG_ADMIN = "org_admin"
EC_CHAIR = "ec_chair"
ELECTION_OFFICIAL = "election_official"
AUDITOR = "auditor"
OBSERVER = "observer"

# Pseudo-role for Django's ``is_superuser`` flag.
SUPER_ADMIN = "super_admin"

ALL_ROLES = (ORG_ADMIN, EC_CHAIR, ELECTION_OFFICIAL, AUDITOR, OBSERVER)

# Roles that may run/configure an election (lifecycle, registry, structure).
MANAGE_ROLES = (ORG_ADMIN, EC_CHAIR, ELECTION_OFFICIAL)
# Roles that may decide four-eyes approval requests / elevated archive actions.
APPROVAL_ROLES = (ORG_ADMIN, EC_CHAIR)
# Roles with internal read access.
READ_ROLES = (ORG_ADMIN, EC_CHAIR, ELECTION_OFFICIAL, AUDITOR, OBSERVER)
# Roles that may see internal (non-public) operational material: full incident
# lists, representative management, early results, audit exports.
STAFF_ROLES = (ORG_ADMIN, EC_CHAIR, ELECTION_OFFICIAL, AUDITOR)
# Roles that may administer tenant lifecycle (create/rename/toggle tenants).
ADMIN_ROLES = (ORG_ADMIN,)
# Roles that may appoint/replace election officials.
OFFICIALS_ROLES = (ORG_ADMIN, EC_CHAIR)
# Roles that may reach the management console (``is_staff`` is kept in sync
# with these by the people console; superusers always have access).
CONSOLE_ROLES = (ORG_ADMIN, EC_CHAIR, ELECTION_OFFICIAL, AUDITOR)
# Roles a chair may grant/revoke.  Deliberately excludes ``org_admin`` and
# ``ec_chair`` so a chair can never escalate their own or another user's
# authority.
CHAIR_GRANTABLE_ROLES = (ELECTION_OFFICIAL, OBSERVER)


def is_system_admin(user) -> bool:
    """True for the designated Super Admin / developer (Django ``is_superuser``)."""
    return bool(user and user.is_authenticated and user.is_superuser)


def system_wide_usernames() -> set[str]:
    """Usernames explicitly granted system-wide oversight (chairs)."""
    from django.conf import settings

    raw = getattr(settings, "EC_CHAIR_SYSTEM_SCOPE_USERNAMES", ()) or ()
    return {str(name).strip().lower() for name in raw if str(name).strip()}


def has_system_wide_scope(user) -> bool:
    """Explicitly granted system-wide election/people oversight."""
    if is_system_admin(user):
        return True
    if not user or not user.is_authenticated:
        return False
    username = (user.get_username() or "").lower()
    return bool(username) and username in system_wide_usernames()


def memberships(user) -> OrganizationMembership.QuerySet:
    if not user or not user.is_authenticated:
        return OrganizationMembership.objects.none()
    return OrganizationMembership.objects.filter(user=user).select_related("organization")


def role_in(user, organization, roles) -> bool:
    if not organization:
        return False
    root = organization.root()
    return memberships(user).filter(
        organization__in=[organization, root], role__in=roles
    ).exists()


def _organization_subtree(ids) -> Organization.QuerySet:
    """Expand a set of organization ids to include every descendant."""
    ids = set(ids)
    frontier = set(ids)
    while frontier:
        children = set(
            Organization.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        ) - ids
        ids |= children
        frontier = children
    return Organization.objects.filter(pk__in=ids)


def organizations_for(user, roles=MANAGE_ROLES) -> Organization.QuerySet:
    """Organizations the user may act on (own + whole subtree).

    Managing a root organization implies managing its descendants (the same
    scope :func:`role_in` grants), so the listing matches the permissions.
    A Super Admin - or a chair explicitly granted system-wide scope - sees
    every organization for election/people oversight.
    """
    if is_system_admin(user):
        return Organization.objects.all()
    if not user or not user.is_authenticated:
        return Organization.objects.none()
    if has_system_wide_scope(user) and set(roles) & set(MANAGE_ROLES):
        return Organization.objects.all()
    direct = list(
        Organization.objects.filter(
            memberships__user=user,
            memberships__role__in=roles,
        ).values_list("pk", flat=True)
    )
    return _organization_subtree(direct)


def admin_organizations_for(user) -> Organization.QuerySet:
    """Organizations the user may *administer* (``org_admin`` scope).

    Narrower than :func:`organizations_for`: an ``election_official`` or chair
    may run elections but must not rename organizations or manage tenants.
    Deliberately ignores the chair system-wide grant.
    """
    if is_system_admin(user):
        return Organization.objects.all()
    if not user or not user.is_authenticated:
        return Organization.objects.none()
    direct = list(
        Organization.objects.filter(
            memberships__user=user,
            memberships__role__in=ADMIN_ROLES,
        ).values_list("pk", flat=True)
    )
    return _organization_subtree(direct)


def chair_organizations_for(user) -> Organization.QuerySet:
    """Organizations a chair is assigned to oversee (institution subtree)."""
    return organizations_for(user, roles=(EC_CHAIR,))


def people_organizations_for(user) -> Organization.QuerySet:
    """Organizations in which the user may appoint/replace officials."""
    if is_system_admin(user) or has_system_wide_scope(user):
        return Organization.objects.all()
    return organizations_for(user, roles=(ORG_ADMIN, EC_CHAIR))


def can_administer_organizations(user) -> bool:
    """Whether the user may use the organization lifecycle console at all."""
    return is_system_admin(user) or admin_organizations_for(user).exists()


def can_manage_officials(user, organization=None) -> bool:
    """Whether the user may appoint/replace officials (at all / in one org)."""
    if is_system_admin(user) or has_system_wide_scope(user):
        return True
    if organization is not None:
        return role_in(user, organization, OFFICIALS_ROLES)
    return organizations_for(user, roles=(ORG_ADMIN, EC_CHAIR)).exists()


def grantable_roles(user, organization) -> list[str]:
    """The role codes this user may grant/revoke in ``organization``.

    Super Admins and institution administrators may grant any role; a chair
    may only appoint/remove officials and observers (never another
    administrator or chair - no privilege escalation).
    """
    if is_system_admin(user):
        return list(ALL_ROLES)
    if role_in(user, organization, ADMIN_ROLES):
        return list(ALL_ROLES)
    if role_in(user, organization, (EC_CHAIR,)) or has_system_wide_scope(user):
        return list(CHAIR_GRANTABLE_ROLES)
    return []


def is_ec_chair(user, organization) -> bool:
    """True if the user chairs ``organization`` (or its root)."""
    return role_in(user, organization, (EC_CHAIR,))


def can_manage_election(user, election) -> bool:
    if is_system_admin(user):
        return True
    if has_system_wide_scope(user):
        return True
    return role_in(user, election.organization, MANAGE_ROLES)


def can_approve_election(user, election) -> bool:
    """Whether the user may decide four-eyes approvals for this election."""
    if is_system_admin(user):
        return True
    return role_in(user, election.organization, APPROVAL_ROLES)


def can_oversee_election(user, election) -> bool:
    """Chair-specific oversight (readiness, officials, escalations, exceptions)."""
    if is_system_admin(user) or has_system_wide_scope(user):
        return True
    return role_in(user, election.organization, (EC_CHAIR,))


def can_view_election(user, election) -> bool:
    """Internal read access (dashboards, registry, monitoring)."""
    if is_system_admin(user):
        return True
    if has_system_wide_scope(user):
        return True
    if role_in(user, election.organization, READ_ROLES):
        return True
    # Approved representatives may view their own election's monitor surface.
    if user and user.is_authenticated:
        from representatives.models import Representative

        if Representative.objects.filter(
            user=user, election=election, status="approved"
        ).exists():
            return True
    return False


def is_election_staff(user, election) -> bool:
    """Organization staff access, *excluding* mere representatives.

    Used where the distinction matters: representatives may view the monitor
    surface of their own election (``can_view_election``) but must not see
    staff-only material such as the full incident list or the representative
    management page.
    """
    if is_system_admin(user) or has_system_wide_scope(user):
        return True
    return role_in(user, election.organization, STAFF_ROLES)


def can_view_results_early(user, election) -> bool:
    """Whether hidden (pre-publication) results may be shown to this user."""
    if is_system_admin(user) or has_system_wide_scope(user):
        return True
    return role_in(user, election.organization, STAFF_ROLES)


def can_view_live_totals(user, election) -> bool:
    """Whether the live candidate-total board may be shown to this user.

    Staff/auditors always have access because they run the engine room.
    Representatives and observers are governed by the election's published
    ``results_visibility`` policy:

    * ``live_totals`` - while voting is open and afterwards;
    * ``after_close`` - once voting has closed;
    * ``after_certification`` - only after certification;
    * ``private`` - never.

    The board only exposes aggregate candidate totals.  This predicate is a
    display policy - the view still enforces it server-side.
    """
    if can_view_results_early(user, election):
        return True
    if not can_view_election(user, election):
        return False

    from elections.models import Election

    visibility = election.results_visibility
    state = election.state
    if visibility == Election.VISIBILITY_PRIVATE:
        return False
    if visibility == Election.VISIBILITY_LIVE:
        return True
    if visibility == Election.VISIBILITY_AFTER_CLOSE:
        return state in {
            Election.STATE_CLOSED,
            Election.STATE_RECONCILIATION,
            Election.STATE_REVIEW,
            Election.STATE_CERTIFIED,
            Election.STATE_PUBLISHED,
            Election.STATE_ARCHIVED,
        }
    if visibility == Election.VISIBILITY_AFTER_CERT:
        return state in {
            Election.STATE_CERTIFIED,
            Election.STATE_PUBLISHED,
            Election.STATE_ARCHIVED,
        }
    return False


def user_org_roles(user) -> dict[str, str]:
    """Mapping organization-id -> highest role, for nav/template hints."""
    result: dict[str, str] = {}
    order = list(ALL_ROLES)
    for m in memberships(user):
        key = str(m.organization_id)
        if m.role not in order:
            continue
        if key not in result or order.index(m.role) < order.index(result[key]):
            result[key] = m.role
    return result


def access_flags(user) -> dict[str, bool]:
    """Compact role booleans for templates (display hints only).

    Computed with a single membership query so the navigation context
    processor stays cheap.
    """
    if not user or not user.is_authenticated:
        return {
            "is_super_admin": False,
            "is_ec_chair": False,
            "is_org_admin": False,
            "is_console_user": False,
            "can_manage_any": False,
            "can_approve_any": False,
            "can_manage_officials": False,
        }
    role_set = {m.role for m in memberships(user)}
    admin = is_system_admin(user)
    system_wide = has_system_wide_scope(user)
    return {
        "is_super_admin": admin,
        "is_ec_chair": EC_CHAIR in role_set or system_wide,
        "is_org_admin": ORG_ADMIN in role_set,
        "is_console_user": admin
        or bool(getattr(user, "is_staff", False))
        or bool(role_set & set(CONSOLE_ROLES)),
        "can_manage_any": admin or system_wide or bool(role_set & set(MANAGE_ROLES)),
        "can_approve_any": admin or bool(role_set & set(APPROVAL_ROLES)),
        "can_manage_officials": admin or system_wide or bool(role_set & set(OFFICIALS_ROLES)),
    }


def console_required(view_func):
    """Gate the management console on real console access, not just ``is_staff``.

    Anonymous visitors are redirected to the admin sign-in page (mirroring
    ``staff_member_required``, so the existing ``?next=/dashboard/...`` flow is
    preserved), but an *authenticated* user who lacks console access is refused
    with a 403 that names the missing permission instead of being bounced to a
    sign-in page they have already passed.  This admits the Super Admin
    regardless of the ``is_staff`` flag and any user holding a console role
    membership, removing the fragile coupling between ``is_staff`` and the
    tenancy model.
    """

    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user
        active = bool(getattr(user, "is_active", False))
        if user.is_authenticated and active:
            if (
                is_system_admin(user)
                or getattr(user, "is_staff", False)
                or memberships(user).filter(role__in=CONSOLE_ROLES).exists()
            ):
                return view_func(request, *args, **kwargs)
            raise PermissionDenied(
                "This area is part of the management console. Your account has no "
                "console role here; ask an institution administrator or "
                "election-commission chair to grant you one (institution "
                "administrator, election official or auditor)."
            )
        return redirect_to_login(request.get_full_path(), resolve_url("admin:login"))

    return wrapper


def require(predicate, message: str = "You are not allowed to perform this action."):
    """Decorator factory: ``@require(lambda u, e: can_manage_election(u, e))``."""

    def decorator(view_func):
        @functools.wraps(view_func)
        @login_required
        def wrapper(request, *args, **kwargs):
            election = kwargs.get("election") or kwargs.get("election_obj")
            if election is None:
                from elections.models import Election

                election = kwargs.get("election_id")
                if election is not None:
                    election = Election.objects.filter(pk=election).first()
            if not predicate(request.user, election):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
