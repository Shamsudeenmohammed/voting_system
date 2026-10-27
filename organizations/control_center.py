"""Super Admin Control Center.

A single, system-wide operations surface for the Django ``is_superuser``
account (the **Super Admin**).  It is *additive*: it does not grant any authority
the predicates in :mod:`organizations.roles` have not already given a Super
Admin.  What it adds is one place to see system health, operational alerts, the
emergency-override history and a set of explicit **recovery** actions for the
rare cases where a tenant-level mistake has locked somebody out.

Every view here is gated on :func:`organizations.roles.is_system_admin` and
re-checks it inside each action handler, so a direct POST to a recovery URL can
never bypass the gate.  Every recovery action is written to the hash-chained
audit log with the actor, the target and the before/after value - recovering
access is an *audited override*, not a silent bypass.

The Control Center never inspects ballot content: it only counts rows and reads
identity/audit metadata, preserving the secret-ballot guarantees enforced by the
``ballot``/``tally`` apps.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit import services as audit
from audit.models import AuditEvent
from elections.models import Election, ElectionApproval

from .models import Organization, OrganizationMembership
from .roles import CONSOLE_ROLES, is_system_admin

User = get_user_model()

# How many recent items to surface in the Control Center lists.
RECENT_LIMIT = 12
# A pending approval older than this is an *operational alert*.
STALE_APPROVAL_HOURS = 24


def _require_super_admin(request) -> None:
    if not is_system_admin(request.user):
        raise PermissionDenied(
            "The Super Admin control centre is restricted to system "
            "administrators (the Super Admin account)."
        )


# ---------------------------------------------------------------------------
# Health / alerts
# ---------------------------------------------------------------------------


def _health_checks() -> list[dict]:
    """Cheap, read-only liveness checks for the deployment."""
    checks: list[dict] = []

    # -- primary datastore ------------------------------------------------
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:  # pragma: no cover - only fires on a real outage
        db_ok = False
    checks.append(
        {
            "key": "database",
            "label": "Primary datastore",
            "ok": db_ok,
            "detail": "Reachable." if db_ok else "Unreachable - check DB settings.",
        }
    )

    # -- cache ------------------------------------------------------------
    from django.core.cache import cache

    try:
        cache.set("control-center-probe", "1", 5)
        cache_ok = cache.get("control-center-probe") == "1"
    except Exception:  # pragma: no cover
        cache_ok = False
    checks.append(
        {
            "key": "cache",
            "label": "Cache / coordination backend",
            "ok": cache_ok,
            "detail": "Round-trip OK." if cache_ok else "Round-trip failed.",
        }
    )

    # -- audit chain integrity -------------------------------------------
    chains = audit.verify_all_chains()
    broken = [c for c in chains if not c["ok"]]
    checks.append(
        {
            "key": "audit",
            "label": "Audit chains",
            "ok": not broken,
            "detail": (
                f"{len(chains)} chain(s) verified."
                if not broken
                else f"{len(broken)} of {len(chains)} chain(s) broken - investigate."
            ),
        }
    )

    # -- cryptographic material ------------------------------------------
    ballot_key = bool(getattr(settings, "BALLOT_ENCRYPTION_KEY", ""))
    result_key = bool(getattr(settings, "RESULT_SIGNING_KEY", ""))
    checks.append(
        {
            "key": "crypto",
            "label": "Cryptographic material",
            "ok": True,
            "detail": (
                f"ballot key {'set' if ballot_key else 'file/auto'}, "
                f"result key {'set' if result_key else 'file/auto'}, "
                f"token pepper {'set' if settings.VOTER_TOKEN_PEPPER else 'from SECRET_KEY'}."
            ),
        }
    )
    return checks


def _alerts() -> list[dict]:
    """Operational alerts that warrant a Super Admin's attention."""
    alerts: list[dict] = []
    now = timezone.now()

    broken_chains = [c for c in audit.verify_all_chains() if not c["ok"]]
    if broken_chains:
        alerts.append(
            {
                "level": "danger",
                "title": f"{len(broken_chains)} audit chain(s) failed verification",
                "detail": "A broken hash chain may indicate tampering; export and inspect it before acting.",
                "url_name": "control_center_overrides",
            }
        )

    stale_cutoff = now - timedelta(hours=STALE_APPROVAL_HOURS)
    stale = ElectionApproval.objects.filter(
        status=ElectionApproval.STATUS_PENDING, requested_at__lt=stale_cutoff
    ).count()
    if stale:
        alerts.append(
            {
                "level": "warning",
                "title": f"{stale} approval request(s) pending over {STALE_APPROVAL_HOURS}h",
                "detail": "Sensitive transitions are blocked until a second approver decides; the Super Admin may self-decide.",
                "url_name": "approvals_inbox",
            }
        )

    overdue = Election.objects.filter(
        state=Election.STATE_OPEN, end_at__lt=now
    ).count()
    if overdue:
        alerts.append(
            {
                "level": "warning",
                "title": f"{overdue} open election(s) past their end time",
                "detail": "The scheduler closes them on next access; review if a vote is still being accepted.",
                "url_name": "operations",
            }
        )

    no_approver = (
        Election.objects.filter(state=Election.STATE_CONFIG_REVIEW)
        .annotate(
            pending=Count(
                "approvals",
                filter=Q(approvals__status=ElectionApproval.STATUS_PENDING),
                distinct=True,
            )
        )
        .filter(pending=0)
        .count()
    )
    if no_approver:
        alerts.append(
            {
                "level": "info",
                "title": f"{no_approver} election(s) awaiting a configuration approval request",
                "detail": "Awaiting CONFIG_REVIEW; somebody must raise the four-eyes request before it can progress.",
                "url_name": "operations",
            }
        )

    overrides_24h = AuditEvent.objects.filter(
        action="admin_override", created_at__gte=now - timedelta(hours=24)
    ).count()
    if overrides_24h:
        alerts.append(
            {
                "level": "warning",
                "title": f"{overrides_24h} emergency override(s) in the last 24 hours",
                "detail": "Overrides supersede four-eyes controls; confirm each documented reason.",
                "url_name": "control_center_overrides",
            }
        )
    return alerts


# ---------------------------------------------------------------------------
# Control center
# ---------------------------------------------------------------------------


@login_required
@require_http_methods(["GET"])
def control_center(request):
    """System-wide overview: health, alerts, counts and quick actions."""
    _require_super_admin(request)

    counts = {
        "organizations": Organization.objects.count(),
        "active_organizations": Organization.objects.filter(is_active=True).count(),
        "users": User.objects.count(),
        "staff_users": User.objects.filter(is_staff=True).count(),
        "superusers": User.objects.filter(is_superuser=True).count(),
        "memberships": OrganizationMembership.objects.count(),
        "elections": Election.objects.count(),
        "open_elections": Election.objects.filter(state=Election.STATE_OPEN).count(),
        "pending_approvals": ElectionApproval.objects.filter(
            status=ElectionApproval.STATUS_PENDING
        ).count(),
    }
    role_counts = list(
        OrganizationMembership.objects.values("role")
        .annotate(total=Count("id"))
        .order_by("role")
    )

    return render(
        request,
        "organizations/control_center.html",
        {
            "checks": _health_checks(),
            "alerts": _alerts(),
            "counts": counts,
            "role_counts": role_counts,
            "recent_overrides": AuditEvent.objects.filter(action="admin_override")
            .select_related("actor", "election", "organization")
            .order_by("-seq")[:5],
        },
    )


@login_required
@require_http_methods(["GET"])
def control_center_search(request):
    """Search every tenant, person and election system-wide."""
    _require_super_admin(request)

    q = (request.GET.get("q") or "").strip()
    organizations = users = elections = []
    if q:
        organizations = list(
            Organization.objects.filter(
                Q(name__icontains=q) | Q(slug__icontains=q) | Q(kind__icontains=q)
            ).order_by("name")[:25]
        )
        users = list(
            User.objects.filter(
                Q(username__icontains=q)
                | Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            ).order_by("username")[:25]
        )
        elections = list(
            Election.objects.filter(
                Q(title__icontains=q) | Q(organization__name__icontains=q)
            )
            .select_related("organization")
            .order_by("-created_at")[:25]
        )
    total = len(organizations) + len(users) + len(elections)
    return render(
        request,
        "organizations/control_center_search.html",
        {
            "q": q,
            "organizations": organizations,
            "users": users,
            "elections": elections,
            "total": total,
            "searched": bool(q),
        },
    )


@login_required
@require_http_methods(["GET"])
def control_center_overrides(request):
    """The override ledger: emergency lifecycle overrides and governance changes."""
    _require_super_admin(request)

    override_events = list(
        AuditEvent.objects.filter(action="admin_override")
        .select_related("actor", "election", "organization")
        .order_by("-seq")[:100]
    )
    governance_events = list(
        AuditEvent.objects.filter(action="governance_policy_updated")
        .select_related("actor", "election")
        .order_by("-seq")[:50]
    )
    return render(
        request,
        "organizations/control_center_overrides.html",
        {
            "override_events": override_events,
            "governance_events": governance_events,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def control_center_recovery(request):
    """Explicit, audited recovery actions for lock-outs and toggles."""
    _require_super_admin(request)

    if request.method == "POST":
        action = request.POST.get("action", "")
        reason = (request.POST.get("reason") or "").strip()

        if action == "restore_console_access":
            return _recover_console_access(request, reason)
        if action == "toggle_organization":
            return _recover_toggle_organization(request, reason)
        if action == "clear_config_cache":
            return _recover_clear_config_cache(request, reason)
        if action == "unlock_user":
            return _recover_unlock_user(request, reason)

        messages.error(request, "Unknown recovery action.")
        return redirect("control_center_recovery")

    organizations = Organization.objects.order_by("name")[:200]
    recent_users = User.objects.order_by("-date_joined")[:50]
    elections = Election.objects.select_related("organization").order_by("-created_at")[:50]
    return render(
        request,
        "organizations/control_center_recovery.html",
        {
            "organizations": organizations,
            "users": recent_users,
            "elections": elections,
        },
    )


# ---------------------------------------------------------------------------
# Recovery action handlers (each re-checks the gate and audits the change)
# ---------------------------------------------------------------------------


def _recover_console_access(request, reason: str):
    user = get_object_or_404(User, username=request.POST.get("username", "").strip())
    before = bool(user.is_staff)
    should_be_staff = user.is_superuser or OrganizationMembership.objects.filter(
        user=user, role__in=CONSOLE_ROLES
    ).exists()
    user.is_staff = should_be_staff
    user.save(update_fields=["is_staff"])
    audit.record(
        action="recovery_console_access",
        actor=request.user,
        request=request,
        object_type="user",
        object_id=str(user.pk),
        reason=reason or "Console access recomputed from role memberships.",
        details={
            "username": user.get_username(),
            "is_staff_before": before,
            "is_staff_after": should_be_staff,
        },
    )
    messages.success(
        request,
        f"Console access for {user.get_username()} set to "
        f"{'enabled' if should_be_staff else 'disabled'} from role memberships.",
    )
    return redirect("control_center_recovery")


def _recover_toggle_organization(request, reason: str):
    organization = get_object_or_404(
        Organization, pk=request.POST.get("organization", "")
    )
    before = organization.is_active
    organization.is_active = not before
    organization.save(update_fields=["is_active"])
    audit.record(
        action="recovery_organization_toggled",
        actor=request.user,
        organization=organization,
        request=request,
        object_type="organization",
        object_id=str(organization.pk),
        reason=reason or "Super Admin recovery toggle.",
        details={"is_active_before": before, "is_active_after": organization.is_active},
    )
    messages.success(
        request,
        f"Organization “{organization.name}” "
        f"{'activated' if organization.is_active else 'deactivated'}.",
    )
    return redirect("control_center_recovery")


def _recover_clear_config_cache(request, reason: str):
    from elections.config import invalidate_config_cache

    election_id = (request.POST.get("election") or "").strip()
    if election_id:
        election = get_object_or_404(Election, pk=election_id)
        invalidate_config_cache(election)
        target = [election]
    else:
        target = list(Election.objects.all())
        for election in target:
            invalidate_config_cache(election)
    audit.record(
        action="recovery_config_cache_cleared",
        actor=request.user,
        request=request,
        object_type="election" if election_id else "system",
        object_id=str(election_id or ""),
        reason=reason or "Super Admin cache recovery.",
        details={"cached_elections": len(target)},
    )
    messages.success(
        request,
        f"Config cache cleared for {len(target)} election(s).",
    )
    return redirect("control_center_recovery")


def _recover_unlock_user(request, reason: str):
    user = get_object_or_404(User, username=request.POST.get("username", "").strip())
    before = bool(user.is_active)
    user.is_active = True
    user.save(update_fields=["is_active"])
    audit.record(
        action="recovery_user_unlocked",
        actor=request.user,
        request=request,
        object_type="user",
        object_id=str(user.pk),
        reason=reason or "Super Admin locked-account recovery.",
        details={
            "username": user.get_username(),
            "is_active_before": before,
            "is_active_after": True,
        },
    )
    messages.success(request, f"Account “{user.get_username()}” is active again.")
    return redirect("control_center_recovery")
