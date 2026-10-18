"""Result publication and recount surfaces.

Only aggregate data ever leaves this module: snapshots carry per-position
totals, never ballot payloads, voter identifiers or receipt codes.
"""
from __future__ import annotations

import json

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from audit import services as audit
from elections.models import Election
from organizations.roles import can_manage_election, can_view_results_early, is_system_admin

from . import services
from .models import TallySnapshot


def _latest_publishable(election):
    return (
        TallySnapshot.objects.filter(election=election)
        .exclude(status=TallySnapshot.STATUS_SUPERSEDED)
        .order_by("-version")
        .first()
    )


@require_http_methods(["GET"])
def results(request, pk):
    """Results page honouring the election's visibility policy."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    privileged = can_view_results_early(request.user, election)

    snapshot = _latest_publishable(election)
    published = election.state in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED)
    live = (
        election.state == Election.STATE_OPEN
        and election.results_visibility == Election.VISIBILITY_LIVE
    )
    visible = published or live or privileged
    if not visible:
        snapshot = None
    versions = []
    if privileged:
        versions = TallySnapshot.objects.filter(election=election).order_by("-version")
    return render(
        request,
        "tally/results.html",
        {
            "election": election,
            "snapshot": snapshot,
            "visible": visible,
            "published": published,
            "live": live,
            "versions": versions,
            "privileged": privileged,
        },
    )


@require_http_methods(["POST"])
def recount(request, pk):
    """Deterministic recount -> new snapshot version (officials only)."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not (is_system_admin(request.user) or can_manage_election(request.user, election)):
        raise PermissionDenied()
    if election.state not in (
        Election.STATE_CLOSED,
        Election.STATE_RECONCILIATION,
        Election.STATE_REVIEW,
        Election.STATE_CERTIFIED,
        Election.STATE_PUBLISHED,
    ):
        return JsonResponse(
            {"ok": False, "error": "Recounts are only possible after voting has closed."},
            status=409,
        )
    reason = (request.POST.get("reason") or "").strip()
    snapshot = services.compute_snapshot(election.pk, actor=request.user, reason=reason)
    audit.record(
        action="recount_requested",
        actor=request.user,
        election=election,
        request=request,
        object_type="tally_snapshot",
        object_id=str(snapshot.pk),
        reason=reason,
        details={"version": snapshot.version},
    )
    return redirect("snapshot_detail", pk=pk, version=snapshot.version)


@require_http_methods(["GET"])
def snapshot_detail(request, pk, version: int):
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    snapshot = get_object_or_404(
        TallySnapshot, election=election, version=version
    )
    privileged = can_view_results_early(request.user, election)
    published = snapshot.status != TallySnapshot.STATUS_INTERIM and (
        election.state in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED)
    )
    if snapshot.status == TallySnapshot.STATUS_INTERIM and not privileged:
        raise PermissionDenied("Interim results are restricted to election officials.")
    if not (privileged or published):
        raise PermissionDenied("Results have not been published yet.")
    return render(
        request,
        "tally/snapshot_detail.html",
        {
            "election": election,
            "snapshot": snapshot,
            "payload": snapshot.payload,
            "privileged": privileged,
        },
    )


@require_http_methods(["GET"])
def verify(request, pk, version: int):
    """Re-run the count and report whether the stored snapshot matches."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    snapshot = get_object_or_404(
        TallySnapshot, election=election, version=version
    )
    privileged = can_view_results_early(request.user, election)
    published = election.state in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED)
    if not (privileged or published):
        raise PermissionDenied()
    report = services.verify_snapshot(snapshot)
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"version": version, **report})
    return render(
        request,
        "tally/verify.html",
        {"election": election, "snapshot": snapshot, "report": report},
    )


@require_http_methods(["GET"])
def export_snapshot(request, pk, version: int):
    """Signed, deterministic JSON export of one snapshot."""
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    snapshot = get_object_or_404(
        TallySnapshot, election=election, version=version
    )
    privileged = can_view_results_early(request.user, election)
    published = election.state in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED)
    if snapshot.status == TallySnapshot.STATUS_INTERIM and not privileged:
        raise PermissionDenied()
    if not (privileged or published):
        raise PermissionDenied()
    body = {
        "election": str(election.pk),
        "election_title": election.title,
        "version": snapshot.version,
        "status": snapshot.status,
        "config_hash": snapshot.config_hash,
        "snapshot_hash": snapshot.snapshot_hash,
        "prev_snapshot_hash": snapshot.prev_snapshot_hash,
        "signature": snapshot.signature,
        "signature_algorithm": snapshot.signature_algorithm,
        "computed_at": snapshot.computed_at.isoformat(),
        "certified_at": snapshot.certified_at.isoformat() if snapshot.certified_at else None,
        "payload": snapshot.payload,
    }
    response = HttpResponse(
        json.dumps(body, sort_keys=True, indent=2, default=str),
        content_type="application/json",
    )
    response["Content-Disposition"] = f'attachment; filename="tally-{election.pk}-v{version}.json"'
    return response
