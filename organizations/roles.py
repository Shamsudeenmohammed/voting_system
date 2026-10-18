"""Role predicates used by every view and service in the platform.

Rules (enforced in code and covered by tests):

* ``is_superuser`` (system administrator) -> everything.
* ``org_admin``        -> full control of its organization's elections,
                          including approvals/certification/reopen decisions.
* ``election_official``-> configuration, registry, lifecycle execution,
                          monitoring and incidents for its organization.
* ``auditor``          -> strictly read-only: dashboards, results, audit chain
                          verification and authorized exports.
* ``observer``         -> public data only.
* Representatives get their own restricted surface (see ``representatives``);
  they are never organization members unless separately assigned.
"""

from __future__ import annotations

import functools

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import Organization, OrganizationMembership

MANAGE_ROLES = ("org_admin", "election_official")
APPROVAL_ROLES = ("org_admin",)
READ_ROLES = ("org_admin", "election_official", "auditor", "observer")
# Roles that may see internal (non-public) operational material: full incident
# lists, representative management, early results, audit exports.
STAFF_ROLES = ("org_admin", "election_official", "auditor")


def is_system_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_superuser)


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


def organizations_for(user) -> Organization.QuerySet:
    """Organizations the user may administrate (own + whole subtree).

    Managing a root organization implies managing its descendants (the same
    scope :func:`role_in` grants), so the listing matches the permissions.
    """
    if is_system_admin(user):
        return Organization.objects.all()
    if not user or not user.is_authenticated:
        return Organization.objects.none()
    direct = list(
        Organization.objects.filter(
            memberships__user=user,
            memberships__role__in=MANAGE_ROLES,
        )
        .values_list("pk", flat=True)
    )
    ids: set = set(direct)
    frontier = set(direct)
    while frontier:
        children = set(
            Organization.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        ) - ids
        ids |= children
        frontier = children
    return Organization.objects.filter(pk__in=ids)


def can_manage_election(user, election) -> bool:
    if is_system_admin(user):
        return True
    return role_in(user, election.organization, MANAGE_ROLES)


def can_approve_election(user, election) -> bool:
    if is_system_admin(user):
        return True
    return role_in(user, election.organization, APPROVAL_ROLES)


def can_view_election(user, election) -> bool:
    """Internal read access (dashboards, registry, monitoring)."""
    if is_system_admin(user):
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
    if is_system_admin(user):
        return True
    return role_in(user, election.organization, STAFF_ROLES)


def can_view_results_early(user, election) -> bool:
    """Whether hidden (pre-publication) results may be shown to this user."""
    if is_system_admin(user):
        return True
    return role_in(user, election.organization, STAFF_ROLES)


def user_org_roles(user) -> dict[str, str]:
    """Mapping organization-id -> highest role, for nav/template hints."""
    result: dict[str, str] = {}
    order = ["org_admin", "election_official", "auditor", "observer"]
    for m in memberships(user):
        key = str(m.organization_id)
        if key not in result or order.index(m.role) < order.index(result[key]):
            result[key] = m.role
    return result


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
