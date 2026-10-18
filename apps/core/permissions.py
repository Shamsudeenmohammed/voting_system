"""Reusable permission helpers.

Every access-control decision happens server-side. Templates only decide what
to *display*; every view re-checks.
"""

from django.core.exceptions import PermissionDenied

from apps.orgs.models import Membership


def require_org_admin(user, organization):
    if not user.is_authenticated:
        raise PermissionDenied
    if user.is_superuser:
        return True
    ok = Membership.objects.filter(
        user=user,
        organization=organization,
        role=Membership.Role.ADMIN,
        is_active=True,
    ).exists()
    if not ok:
        raise PermissionDenied("Organization administrator role required.")
    return True


def require_staff(user):
    if not user.is_authenticated or not (user.is_staff or user.is_superuser):
        raise PermissionDenied
    return True


def user_orgs(user):
    if not user.is_authenticated:
        return Membership.objects.none()
    qs = Membership.objects.filter(user=user, is_active=True).select_related("organization")
    return qs


def is_org_official(user, organization) -> bool:
    if user.is_superuser:
        return True
    return Membership.objects.filter(
        user=user,
        organization=organization,
        role__in=[Membership.Role.ADMIN, Membership.Role.ELECTION_OFFICIAL],
        is_active=True,
    ).exists()
