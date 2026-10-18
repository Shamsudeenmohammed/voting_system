"""Election administration views. All querysets scoped by organization."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import is_org_official, require_org_admin, user_orgs
from . import state_machine
from .models import Election, ElectionMilestone, LifecycleEvent, PollingStation, Position
from .state_machine import StateTransitionError


def _scoped_election(request, slug) -> Election:
    """Tenant isolation: never fetch elections outside the user's orgs
    (superusers excepted)."""
    qs = Election.objects.select_related("organization")
    if not request.user.is_superuser:
        org_ids = user_orgs(request.user).values_list("organization_id", flat=True)
        qs = qs.filter(organization_id__in=org_ids)
    return get_object_or_404(qs, slug=slug)


@login_required
@never_cache
def home(request):
    orgs = [m.organization for m in user_orgs(request.user)]
    if request.user.is_superuser and not orgs:
        from apps.orgs.models import Organization

        orgs = list(Organization.objects.filter(is_active=True))
    elections = (
        Election.objects.filter(organization__in=orgs)
        .select_related("organization")
        .order_by("-created_at")
        if orgs
        else Election.objects.none()
    )
    states = dict(Election.State.choices)
    return render(
        request,
        "elections/home.html",
        {"elections": elections, "states": states, "orgs": orgs},
    )


@login_required
@never_cache
def detail(request, slug):
    election = _scoped_election(request, slug)
    if not request.user.is_superuser and not is_org_official(
        request.user, election.organization
    ):
        raise PermissionDenied
    events = election.lifecycle_events.select_related("actor")[:30]
    positions = election.positions.prefetch_related("candidates").order_by("order")
    stations = election.stations.order_by("name")
    incidents = election.incidents.order_by("-created_at")[:20]
    from apps.monitoring.queries import election_snapshot

    snapshot = election_snapshot(election)
    return render(
        request,
        "elections/detail.html",
        {
            "election": election,
            "events": events,
            "positions": positions,
            "stations": stations,
            "incidents": incidents,
            "snapshot": snapshot,
            "states": dict(Election.State.choices),
            "can_transition": _available_transitions(election, request.user),
            "ALLOW_REOPEN": settings.ALLOW_ELECTION_REOPEN,
        },
    )


def _available_transitions(election: Election, user) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for to_state in state_machine.FORWARD_TRANSITIONS.get(election.state, set()):
        try:
            state_machine.check_permission(election, user, to_state)
            out.append((to_state, Election.State(to_state).label))
        except (PermissionDenied, ValueError):
            continue
    return sorted(out, key=lambda x: x[1])


@login_required
@require_http_methods(["GET", "POST"])
def create(request):
    from apps.orgs.models import Membership

    org_qs = Membership.objects.filter(
        user=request.user, role="admin", is_active=True
    ).values_list("organization", flat=True)
    if request.user.is_superuser:
        from apps.orgs.models import Organization

        org_qs = Organization.objects.filter(is_active=True).values_list("id", flat=True)
    org_ids = list(org_qs)
    if not org_ids:
        raise PermissionDenied("You administer no organizations.")

    from .forms import ElectionForm

    initial = {"organization": request.GET.get("org") or org_ids[0]}
    form = ElectionForm(request.POST or None, initial=initial)
    form.fields["organization"].queryset = form.fields["organization"].queryset.filter(
        id__in=org_ids
    )
    if request.method == "POST" and form.is_valid():
        election = form.save(commit=False)
        election.created_by = request.user
        election.save()
        from apps.audit.services import record_audit

        record_audit(
            organization=election.organization,
            actor=request.user,
            action="election.created",
            object_type="Election",
            object_id=str(election.pk),
            payload={"election": election.slug, "name": election.name},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Election created.")
        return redirect("elections:detail", slug=election.slug)
    return render(request, "elections/form.html", {"form": form, "title": "New election"})


@login_required
@require_http_methods(["GET", "POST"])
def edit(request, slug):
    election = _scoped_election(request, slug)
    require_org_admin(request.user, election.organization)
    if election.state not in (Election.State.DRAFT, Election.State.REGISTRATION):
        messages.error(request, "Only draft/registration elections can be edited.")
        return redirect("elections:detail", slug=election.slug)
    from .forms import ElectionForm

    form = ElectionForm(request.POST or None, instance=election)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Election updated.")
        return redirect("elections:detail", slug=election.slug)
    return render(request, "elections/form.html", {"form": form, "title": "Edit election"})


@login_required
@require_http_methods(["POST"])
def transition(request, slug):
    election = _scoped_election(request, slug)
    to_state = request.POST.get("to_state", "")
    reason = request.POST.get("reason", "")
    try:
        state_machine.transition(election, to_state, request.user, reason=reason)
        messages.success(request, f"Election moved to '{Election.State(to_state).label}'.")
    except StateTransitionError as exc:
        messages.error(request, str(exc))
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    except ValidationError as exc:
        for m in exc.messages:
            messages.error(request, m)
    return redirect("elections:detail", slug=election.slug)


@login_required
@require_http_methods(["POST"])
def reopen(request, slug):
    election = _scoped_election(request, slug)
    reason = request.POST.get("reason", "")
    approver_username = request.POST.get("approver", "")
    approver_pw = request.POST.get("approver_password", "")
    from django.contrib.auth import authenticate

    approver = None
    if approver_username:
        approver = authenticate(username=approver_username, password=approver_pw)
    try:
        state_machine.reopen(
            election,
            request.user,
            reason,
            approver,
            approver_password_ok=approver is not None,
        )
        messages.success(request, "Election reopened under the exception procedure.")
    except (StateTransitionError, PermissionDenied, ValidationError) as exc:
        msgs = getattr(exc, "messages", None) or [str(exc)]
        for m in msgs:
            messages.error(request, m)
    return redirect("elections:detail", slug=election.slug)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def position_create(request, slug):
    election = _scoped_election(request, slug)
    require_org_admin(request.user, election.organization)
    from .forms import PositionForm

    form = PositionForm(request.POST or None, initial={"election": election.pk})
    if request.method == "POST" and form.is_valid():
        pos = form.save(commit=False)
        pos.election = election
        pos.save()
        messages.success(request, "Position added.")
        return redirect("elections:detail", slug=election.slug)
    return render(
        request, "elections/form.html", {"form": form, "title": "Add position"}
    )


@login_required
@require_http_methods(["POST"])
def station_set_status(request, slug, station_id):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    station = get_object_or_404(PollingStation, pk=station_id, election=election)
    new_status = request.POST.get("status")
    if new_status in dict(PollingStation.Status.choices):
        station.status = new_status
        now = timezone.now()
        if new_status == PollingStation.Status.OPEN and not station.opened_at:
            station.opened_at = now
        if new_status == PollingStation.Status.CLOSED and not station.closed_at:
            station.closed_at = now
        station.save(update_fields=["status", "opened_at", "closed_at", "updated_at"])
        messages.success(request, f"Station '{station.name}' → {station.get_status_display()}.")
    return redirect("elections:detail", slug=election.slug)


@login_required
@require_http_methods(["GET", "POST"])
def milestone_create(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()
        if title:
            ElectionMilestone.objects.create(
                election=election, title=title, body=body, is_public=False
            )
            messages.success(request, "Milestone added.")
        return redirect("elections:detail", slug=election.slug)
    return redirect("elections:detail", slug=election.slug)
