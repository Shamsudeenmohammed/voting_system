"""Live monitoring surfaces.

All data exposed here is aggregate: election state, ballot-box counters,
turnout and incident/station statuses.  No endpoint in this module can ever
return ballot selections, voter identifiers or credential material.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit import services as audit
from ballot.models import BallotBox
from elections.models import Election, PollingStation, Voter
from organizations.roles import (
    can_view_election,
    can_view_results_early,
    organizations_for,
)
from representatives.models import IncidentReport, Representative


def _live_payload(election) -> dict:
    total = election.voters.filter(status=Voter.STATUS_ACTIVE).count()
    voted = election.voters.filter(status=Voter.STATUS_ACTIVE, has_voted=True).count()
    box = BallotBox.objects.filter(election=election).first()
    stations = [
        {"name": s.name, "status": s.status, "ballots": s.ballot_count}
        for s in election.stations.annotate(ballot_count=Count("ballots")).order_by("name")
    ]
    return {
        "election": str(election.pk),
        "title": election.title,
        "state": election.state,
        "now": timezone.now().isoformat(),
        "start_at": election.start_at.isoformat(),
        "end_at": election.end_at.isoformat(),
        "turnout": {
            "total": total,
            "voted": voted,
            "percent": round(100 * voted / total, 1) if total else 0.0,
        },
        "ballot_box": {"last_seq": box.last_seq if box else 0},
        "stations": stations,
        "incidents_open": IncidentReport.objects.filter(election=election, status="open").count(),
        "representatives": {
            "approved": Representative.objects.filter(
                election=election, status=Representative.STATUS_APPROVED
            ).count(),
            "pending": Representative.objects.filter(
                election=election, status=Representative.STATUS_PENDING
            ).count(),
        },
    }


@require_http_methods(["GET"])
def live_status(request, pk):
    """JSON snapshot polled by the live monitoring dashboard."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not can_view_election(request.user, election):
        raise PermissionDenied()
    payload = _live_payload(election)
    if not can_view_results_early(request.user, election):
        # Turnout rates are withheld from observers/representatives until the
        # election is closed; raw counts of an open election stay internal.
        if election.state == Election.STATE_OPEN:
            payload["turnout"] = {"total": None, "voted": None, "percent": None}
    return JsonResponse(payload)


@require_http_methods(["GET"])
def monitor(request, pk):
    """HTML monitoring dashboard (polls ``live_status``)."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not can_view_election(request.user, election):
        raise PermissionDenied()
    return render(
        request,
        "monitoring/monitor.html",
        {
            "election": election,
            "live_url": f"/monitoring/elections/{election.pk}/live/",
            "privileged": can_view_results_early(request.user, election),
        },
    )


@require_http_methods(["GET"])
def org_dashboard(request):
    """Cross-election overview for one of the user's organizations."""
    orgs = organizations_for(request.user)
    if not orgs.exists():
        raise PermissionDenied()
    org = get_object_or_404(orgs, pk=request.GET.get("org") or orgs.first().pk)
    elections = (
        Election.objects.filter(organization=org)
        .annotate(voter_count=Count("voters", distinct=True))
        .order_by("-created_at")
    )
    return render(
        request,
        "monitoring/org_dashboard.html",
        {"org": org, "orgs": orgs, "elections": elections},
    )


@require_http_methods(["GET"])
def audit_export(request, pk):
    """Deterministic JSON export of the election's hash-chained audit log.

    Restricted to privileged roles: the export carries actor labels and
    operational reasons (identity-linked), so observers and mere
    representatives are refused.
    """
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not can_view_results_early(request.user, election):
        raise PermissionDenied()
    body = audit.export_chain(f"election:{election.pk}")
    audit.record(
        action="audit_exported",
        actor=request.user,
        election=election,
        request=request,
        details={"events": body["head"]["seq"]},
    )
    return JsonResponse(body, json_dumps_params={"sort_keys": True})
