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
from elections.models import Candidate, Election
from organizations.roles import can_manage_election, can_view_results_early, is_system_admin

from . import services
from .models import TallySnapshot


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _LiveCountView:
    """Read-only adapter presenting a live count as a snapshot-like object.

    Used when the election is open and results are shown live: the stored
    snapshot may be stale (certified before later votes were cast), so we
    compute a fresh count instead of displaying outdated numbers.
    """

    def __init__(self, payload: dict):
        totals = payload.get("totals", {})
        self.payload = payload
        self.version = None
        self.status = "live"
        self.ballots_accepted = totals.get("ballots_accepted", 0)
        self.ballots_counted = totals.get("ballots_valid", 0)
        self.ballots_invalid = totals.get("ballots_invalid", 0)
        self.config_hash = payload.get("config_hash", "")
        self.reconciliation_ok = totals.get("reconciliation_ok", False)

    def get_status_display(self) -> str:
        return "Live (uncertified)"

    def __str__(self) -> str:
        return f"live count ({self.ballots_accepted} ballots)"


def _initials(full_name: str) -> str:
    words = [word for word in (full_name or "").split() if word]
    return "".join(word[0].upper() for word in words[:2]) or "–"


def _photo_url(candidate) -> str:
    """Media URL for a candidate photograph, or "" when there is none."""
    if candidate is None or not candidate.photo:
        return ""
    try:
        return candidate.photo.url
    except (ValueError, AttributeError):
        return ""


def _display_positions(snapshot, election) -> list[dict]:
    """Enrich snapshot rows with presentation data (never stored in the tally).

    Photographs and display percentages are deliberately resolved at render
    time.  The signed snapshot payload stays untouched, so adding visuals can
    never invalidate its hash or signature.
    """
    if snapshot is None:
        return []

    raw_positions = snapshot.payload.get("positions") or []
    candidate_ids = [
        row.get("id")
        for position in raw_positions
        for row in (position.get("candidates") or [])
        if row.get("id")
    ]
    candidate_map = {}
    if candidate_ids:
        candidate_map = {
            str(candidate.pk): candidate
            for candidate in Candidate.objects.filter(
                position__election=election, pk__in=candidate_ids
            ).only("id", "full_name", "photo")
        }

    positions = []
    for position in raw_positions:
        rows = []
        for row in position.get("candidates") or []:
            full_name = row.get("full_name") or ""
            candidate = candidate_map.get(str(row.get("id")))
            rows.append(
                {
                    "id": row.get("id"),
                    "full_name": full_name,
                    "votes": _as_int(row.get("votes")),
                    "photo_url": _photo_url(candidate),
                    "initials": (
                        candidate.initials if candidate else _initials(full_name)
                    ),
                }
            )

        totals = position.get("totals") or {}
        total_votes = _as_int(totals.get("votes_counted"))
        if total_votes == 0:
            total_votes = sum(row["votes"] for row in rows)

        top_votes = max((row["votes"] for row in rows), default=0)
        top_count = sum(
            1 for row in rows if row["votes"] == top_votes and top_votes > 0
        )
        for row in rows:
            row["percent"] = (
                round(row["votes"] * 100 / total_votes, 1) if total_votes else 0.0
            )
            row["leading"] = top_votes > 0 and row["votes"] == top_votes
            row["tied"] = row["leading"] and top_count > 1

        positions.append(
            {
                "id": position.get("id"),
                "title": position.get("title") or "",
                "max_choices": _as_int(position.get("max_choices")) or 1,
                "candidates": rows,
                "totals": totals,
                "total_votes": total_votes,
            }
        )
    return positions


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

    # For open elections with live visibility, show a fresh count instead of a
    # potentially stale stored snapshot (e.g. certified before later votes
    # were cast, or after the election was reopened).
    if live:
        live_payload = services.live_count(election)
        snapshot = _LiveCountView(live_payload)

    versions = []
    if privileged:
        versions = TallySnapshot.objects.filter(election=election).order_by("-version")
    return render(
        request,
        "tally/results.html",
        {
            "election": election,
            "snapshot": snapshot,
            "positions": _display_positions(snapshot, election),
            "results_final": bool(
                snapshot and snapshot.status == TallySnapshot.STATUS_CERTIFIED
            ),
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
