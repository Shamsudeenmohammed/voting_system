"""Monitoring room views: officials' dashboard, representative room, SSE feed.

Permission matrix (enforced server-side, tested):
* officials/auditors: full aggregate snapshot for their org's elections;
* representatives (approved): aggregate snapshot for THEIR election only,
  public/published results, incident filing, notices — never per-candidate
  breakdown unless the visibility policy allows, never voter/ballot data;
* everyone: figures come from monitoring.queries (real data only).
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.candidates.models import Incident, Representative
from apps.core.permissions import is_org_official, user_orgs
from apps.elections.models import Election
from .models import OperationalNotice
from .queries import election_snapshot, visibility_gate


def _representative_for(request, election) -> Representative | None:
    if not request.user.is_authenticated:
        return None
    return (
        Representative.objects.filter(
            user=request.user,
            election=election,
            status=Representative.Status.APPROVED,
        )
        .select_related("candidate")
        .first()
    )


def _official_election(request, slug) -> Election:
    qs = Election.objects.select_related("organization")
    if not request.user.is_superuser:
        org_ids = user_orgs(request.user).values_list("organization_id", flat=True)
        qs = qs.filter(organization_id__in=org_ids)
    return get_object_or_404(qs, slug=slug)


def _rep_election(request, slug) -> tuple[Election, Representative]:
    election = get_object_or_404(Election, slug=slug)
    rep = _representative_for(request, election)
    if rep is None:
        raise PermissionDenied("Approved representative access required.")
    return election, rep


@login_required
@never_cache
def official_dashboard(request, slug):
    election = _official_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    snap = election_snapshot(election)
    gate = visibility_gate(election)
    notices = election.notices.filter(is_active=True)[:20]
    return render(
        request,
        "monitoring/official_dashboard.html",
        {"election": election, "snap": snap, "gate": gate, "notices": notices},
    )


@never_cache
def rep_home(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    reps = (
        Representative.objects.filter(user=request.user)
        .select_related("election", "candidate")
        .order_by("-created_at")
    )
    pending = [r for r in reps if r.status == Representative.Status.PENDING]
    approved = [r for r in reps if r.status == Representative.Status.APPROVED]
    # Registration entry points: open elections of user's org
    from apps.orgs.models import Membership

    org_ids = Membership.objects.filter(user=request.user).values_list(
        "organization_id", flat=True
    )
    open_elections = Election.objects.filter(
        organization_id__in=org_ids,
        state__in=[Election.State.SCHEDULED, Election.State.OPEN, Election.State.CLOSED],
    ).exclude(representatives__user=request.user)
    return render(
        request,
        "monitoring/rep_home.html",
        {
            "pending": pending,
            "approved": approved,
            "open_elections": open_elections,
        },
    )


@login_required
@never_cache
def rep_room(request, slug):
    election, rep = _rep_election(request, slug)
    snap = election_snapshot(election)
    gate = visibility_gate(election)
    notices = election.notices.filter(is_active=True).exclude(audience="officials")[:20]
    my_incidents = Incident.objects.filter(representative=rep).order_by("-created_at")[:20]
    return render(
        request,
        "monitoring/rep_room.html",
        {
            "election": election,
            "rep": rep,
            "snap": snap,
            "gate": gate,
            "notices": notices,
            "my_incidents": my_incidents,
        },
    )


@require_http_methods(["GET", "POST"])
def rep_incident_new(request, slug):
    election, rep = _rep_election(request, slug)
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()
        category = request.POST.get("category", Incident.Category.OTHER)
        if title and body:
            incident = Incident.objects.create(
                election=election,
                organization=election.organization,
                filed_by=request.user,
                representative=rep,
                category=category if category in dict(Incident.Category.choices) else Incident.Category.OTHER,
                title=title[:200],
                body=body,
                evidence=request.FILES.get("evidence") if request.FILES.get("evidence") and request.FILES["evidence"].size <= 10 * 1024 * 1024 else None,
            )
            from apps.audit.services import record_audit

            record_audit(
                organization=election.organization,
                actor=request.user,
                action="incident.filed",
                object_type="Incident",
                object_id=str(incident.pk),
                payload={"election": election.slug, "category": incident.category},
            )
            from django.contrib import messages

            messages.success(request, "Incident reported. Officials will review it.")
            return redirect("monitoring:rep-room", slug=slug)
    return render(
        request,
        "monitoring/rep_incident_form.html",
        {"election": election, "rep": rep},
    )


def _sse_stream(election_id: int):
    """Server-Sent Events generator: aggregate figures only.

    Emits a payload every 5 seconds for up to 10 minutes per connection;
    the client auto-reconnects (EventSource default). Payload contains ONLY
    aggregate counts — never selections, never voter identities.
    """
    import time

    from .queries import election_snapshot, visibility_gate

    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            election = Election.objects.filter(pk=election_id).first()
            if election is None:
                break
            snap = election_snapshot(election)
            gate = visibility_gate(election)
            if not gate["show_totals"]:
                # Withheld policy: emit turnout totals only (already aggregate)
                snap.pop("accepted_ballots", None)
            payload = {
                "turnout_pct": snap["turnout_pct"],
                "eligible_voters": snap["eligible_voters"],
                "accepted_ballots": snap["accepted_ballots"]
                if gate["show_totals"]
                else None,
                "stations_open": snap["stations_open"],
                "station_count": snap["station_count"],
                "open_incidents": snap["open_incidents"],
                "state": snap["state"],
                "server_time": snap["server_time"],
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            # Never leak internals to the stream; emit keepalive.
            yield ": keepalive\n\n"
        time.sleep(5)
    yield "event: end\ndata: {}\n\n"


@login_required
@never_cache
def live_feed(request, slug):
    """SSE endpoint — authorized per recipient (official OR rep of THIS
    election). Aggregates only."""
    is_official = False
    try:
        election = _official_election(request, slug)
        is_official = is_org_official(request.user, election.organization)
    except Http404:
        election = None
    if election is None or not is_official:
        try:
            election, _rep = _rep_election(request, slug)
        except (Http404, PermissionDenied):
            raise PermissionDenied("Not authorized for this feed.")
    resp = StreamingHttpResponse(
        _sse_stream(election.pk), content_type="text/event-stream"
    )
    resp["Cache-Control"] = "no-store"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@require_http_methods(["POST"])
def official_incident_update(request, slug, pk):
    election = _official_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    incident = get_object_or_404(Incident, pk=pk, election=election)
    status = request.POST.get("status")
    resolution = request.POST.get("resolution", "")
    if status in dict(Incident.Status.choices):
        incident.status = status
        if status in (Incident.Status.RESOLVED, Incident.Status.DISMISSED):
            incident.resolved_by = request.user
            from django.utils import timezone

            incident.resolved_at = timezone.now()
            incident.resolution = resolution
        incident.save()
        from apps.audit.services import record_audit

        record_audit(
            organization=election.organization,
            actor=request.user,
            action="incident.updated",
            object_type="Incident",
            object_id=str(incident.pk),
            payload={"election": election.slug, "status": status},
        )
    return redirect("candidates:incidents", slug=slug)
