"""Live monitoring surfaces.

All data exposed here is aggregate: election state, ballot-box counters,
turnout, candidate totals and incident/station statuses.  No endpoint in this
module can ever return ballot selections, voter identifiers or credential
material.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from audit import services as audit
from ballot.models import BallotBox
from elections.lifecycle import LifecycleError, sync_election_state
from elections.models import Candidate, Election, PollingStation, Voter
from organizations.roles import (
    can_view_election,
    can_view_live_totals,
    can_view_results_early,
    organizations_for,
)
from representatives.models import IncidentReport, Representative
from tally import services as tally

logger = logging.getLogger(__name__)

LIVE_BOARD_REFRESH_SECONDS = 3

_STATE_BOARD_LABELS = {
    Election.STATE_DRAFT: ("Draft", "pending"),
    Election.STATE_REGISTRATION: ("Registration", "pending"),
    Election.STATE_CONFIG_REVIEW: ("Configuration review", "pending"),
    Election.STATE_APPROVED: ("Approved", "pending"),
    Election.STATE_SCHEDULED: ("Scheduled", "pending"),
    Election.STATE_OPEN: ("Voting open", "open"),
    Election.STATE_CLOSED: ("Voting closed", "closed"),
    Election.STATE_RECONCILIATION: ("Reconciliation", "closed"),
    Election.STATE_REVIEW: ("Results review", "closed"),
    Election.STATE_CERTIFIED: ("Certified", "certified"),
    Election.STATE_PUBLISHED: ("Published", "certified"),
    Election.STATE_ARCHIVED: ("Archived", "closed"),
}


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


def _board_state(election):
    """Apply schedule-driven transitions before rendering live totals.

    A board left open on a wall display must roll into "voting closed" (and
    the resulting visibility policy) without requiring a manual refresh.
    """
    try:
        return sync_election_state(election)
    except LifecycleError as exc:
        logger.warning("live board could not sync election %s: %s", election.pk, exc)
        return election


def _photo_url(candidate) -> str:
    """Return a usable media URL, or an empty string for the initials fallback."""
    if candidate is None or not candidate.photo:
        return ""
    try:
        return candidate.photo.url
    except (ValueError, AttributeError):
        return ""


def _schedule_label(election) -> str:
    if not election.start_at or not election.end_at:
        return ""
    start = timezone.localtime(election.start_at)
    end = timezone.localtime(election.end_at)
    if start.date() == end.date():
        return f"{start:%d %b %Y · %H:%M}–{end:%H:%M}"
    return f"{start:%d %b %Y · %H:%M} – {end:%d %b %Y · %H:%M}"


def _health(count: dict) -> dict:
    totals = count["totals"]
    live = count.get("live") or {}
    invalid = totals["ballots_invalid"]
    accepted = totals["ballots_accepted"]
    box_seq = totals["box_last_seq"]
    count_ms = live.get("count_ms")
    latency = f"{count_ms} ms" if count_ms is not None else "n/a"
    last_ballot_at = live.get("last_ballot_at")
    last_ballot_label = "No ballots yet"
    if last_ballot_at:
        try:
            last_ballot_label = timezone.localtime(
                datetime.fromisoformat(last_ballot_at)
            ).strftime("%H:%M:%S")
        except (TypeError, ValueError):
            last_ballot_label = "Unavailable"

    if not totals["reconciliation_ok"]:
        status = "attention"
        label = "Reconciliation check failed"
        detail = f"{accepted} accepted · ballot box #{box_seq}"
    elif invalid:
        status = "attention"
        label = f"{invalid} spoiled ballot{'' if invalid == 1 else 's'} excluded"
        detail = f"Excluded from totals · count {latency}"
    elif accepted == 0:
        status = "idle"
        label = "Ready for the first ballot"
        detail = "Ballot box is empty · waiting for votes"
    else:
        status = "healthy"
        label = "System healthy"
        detail = f"Ballot-box reconciliation passed · count {latency}"

    return {
        "status": status,
        "label": label,
        "detail": detail,
        "reconciliation_ok": totals["reconciliation_ok"],
        "ballots_invalid": invalid,
        "box_seq": box_seq,
        "count_ms": count_ms,
        "last_ballot_at": last_ballot_at,
        "last_ballot_label": last_ballot_label,
        "generated_at": live.get("generated_at"),
    }


def _build_board(election) -> dict:
    """Assemble the projection board payload from an aggregate live count.

    Only candidate names, photographs and vote totals are included.  Ballot
    payloads, receipt codes, voter identifiers and polling-station identities
    never enter this payload.
    """
    count = tally.live_count(election)

    candidate_ids = [
        row["id"]
        for position in count["positions"]
        for row in position["candidates"]
    ]
    candidate_map = {
        str(candidate.pk): candidate
        for candidate in Candidate.objects.filter(
            position__election=election, pk__in=candidate_ids
        ).only("id", "full_name", "photo", "order")
    }

    positions = []
    candidate_total = 0
    for position in count["positions"]:
        total_votes = position["totals"]["votes_counted"]
        rows = []
        for row in position["candidates"]:
            candidate = candidate_map.get(row["id"])
            votes = row["votes"]
            rows.append(
                {
                    "id": row["id"],
                    "name": row["full_name"],
                    "initials": candidate.initials if candidate else "",
                    "photo_url": _photo_url(candidate),
                    "votes": votes,
                    "percent": round(votes * 100 / total_votes, 1)
                    if total_votes
                    else 0.0,
                }
            )

        # Ties share a rank; the raw live count already orders by votes then
        # canonical ballot order, which keeps movement predictable.
        rank = 0
        previous_votes = None
        for index, row in enumerate(rows):
            if row["votes"] != previous_votes:
                rank = index + 1
                previous_votes = row["votes"]
            row["rank"] = rank
        top_votes = rows[0]["votes"] if rows else 0
        top_count = sum(1 for row in rows if row["votes"] == top_votes)
        for row in rows:
            row["leading"] = top_votes > 0 and row["votes"] == top_votes
            row["tied"] = row["leading"] and top_count > 1

        candidate_total += len(rows)
        positions.append(
            {
                "id": position["id"],
                "title": position["title"],
                "max_choices": position["max_choices"],
                "total_votes": total_votes,
                "undervotes": position["totals"]["undervotes"],
                "overvotes": position["totals"]["overvotes"],
                "candidates": rows,
            }
        )

    totals = dict(count["totals"])
    totals["positions"] = len(positions)
    totals["candidates"] = candidate_total
    state_label, state_tone = _STATE_BOARD_LABELS.get(
        election.state, (election.state.title(), "pending")
    )
    health = _health(count)
    fingerprint = count.get("config_hash") or (
        f"{election.pk}:{candidate_total}:{len(positions)}"
    )
    organization = getattr(election, "organization", None)

    return {
        "election_id": str(election.pk),
        "election_title": election.title,
        "organization_name": organization.name if organization else "",
        "schedule_label": _schedule_label(election),
        "state": election.state,
        "state_label": state_label,
        "state_tone": state_tone,
        "is_open": election.state == Election.STATE_OPEN,
        "is_final": election.state
        in {
            Election.STATE_CERTIFIED,
            Election.STATE_PUBLISHED,
            Election.STATE_ARCHIVED,
        },
        "fingerprint": fingerprint,
        "positions": positions,
        "totals": totals,
        "health": health,
        "generated_at": (count.get("live") or {}).get("generated_at")
        or timezone.now().isoformat(),
        "server_time": timezone.now().isoformat(),
        "refresh_ms": LIVE_BOARD_REFRESH_SECONDS * 1000,
    }


@never_cache
@require_http_methods(["GET"])
def live_board(request, pk):
    """Full-screen, projection-first candidate results board.

    Staff can always project it.  Representatives/observers may open it only
    when the election's ``results_visibility`` policy allows candidate totals.
    The board is aggregate-only and never exposes a ballot or voter identity.
    """
    election = get_object_or_404(
        Election.objects.select_related("organization"), pk=pk
    )
    election = _board_state(election)
    if not can_view_live_totals(request.user, election):
        raise PermissionDenied()
    board = _build_board(election)
    response = render(
        request,
        "monitoring/live_board.html",
        {
            "election": election,
            "board": board,
            "data_url": reverse("live_board_data", args=[election.pk]),
            "refresh_seconds": LIVE_BOARD_REFRESH_SECONDS,
        },
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@never_cache
@require_http_methods(["GET"])
def live_board_data(request, pk):
    """JSON feed for the projection board (aggregate candidate totals only)."""
    election = get_object_or_404(
        Election.objects.select_related("organization"), pk=pk
    )
    election = _board_state(election)
    if not can_view_live_totals(request.user, election):
        raise PermissionDenied()
    response = JsonResponse(_build_board(election))
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


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
            "live_url": reverse("live_status", args=[election.pk]),
            "privileged": can_view_results_early(request.user, election),
            "board_available": can_view_live_totals(request.user, election),
            "board_url": reverse("live_board", args=[election.pk]),
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
