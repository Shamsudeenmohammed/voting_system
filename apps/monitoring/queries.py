"""Aggregate queries for monitoring dashboards.

All figures derive from real application data — no cached fake numbers.
Each function returns plain dicts safe to serialize; none of them touch
ballot selections (only counts).
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from apps.ballots.models import AcceptedBallot, BallotGrant
from apps.candidates.models import Incident, Representative
from apps.elections.models import Election, PollingStation
from apps.voters.policy import eligible_count


def election_snapshot(election: Election) -> dict:
    """Turnout + station status + milestone counters for one election."""
    eligible = eligible_count(election)
    accepted = AcceptedBallot.objects.filter(election=election).count()
    consumed = BallotGrant.objects.filter(
        election=election, status=BallotGrant.Status.CONSUMED
    ).count()
    issued = BallotGrant.objects.filter(
        election=election, status=BallotGrant.Status.ISSUED
    ).count()

    stations = list(
        PollingStation.objects.filter(election=election)
        .order_by("name")
        .values("id", "name", "code", "channel", "status")
    )
    station_ballots = dict(
        AcceptedBallot.objects.filter(election=election, station__isnull=False)
        .values_list("station_id")
        .annotate(n=Count("pk"))
    )
    for st in stations:
        st["ballots_accepted"] = station_ballots.get(st["id"], 0)

    milestones = list(
        election.milestones.order_by("due_at", "created_at").values(
            "id", "title", "body", "due_at", "completed_at", "is_public"
        )[:50]
    )
    open_incidents = Incident.objects.filter(election=election).exclude(
        status__in=["resolved", "dismissed"]
    ).count()
    reps = Representative.objects.filter(election=election)
    now = timezone.now()
    return {
        "state": election.state,
        "state_display": election.get_state_display(),
        "eligible_voters": eligible,
        "accepted_ballots": accepted,
        "grants_consumed": consumed,
        "grants_outstanding": issued,
        "turnout_pct": round(100.0 * accepted / eligible, 2) if eligible else 0.0,
        "reconciled_grants": consumed == accepted,
        "stations": stations,
        "station_count": len(stations),
        "stations_open": sum(1 for s in stations if s["status"] == "open"),
        "milestones": milestones,
        "open_incidents": open_incidents,
        "reps_total": reps.count(),
        "reps_approved": reps.filter(status=Representative.Status.APPROVED).count(),
        "reps_present": reps.filter(attendance=Representative.Attendance.PRESENT).count(),
        "scheduled_opens": election.scheduled_opens.isoformat()
        if election.scheduled_opens
        else None,
        "scheduled_closes": election.scheduled_closes.isoformat()
        if election.scheduled_closes
        else None,
        "server_time": now.isoformat(),
        "result_visibility": election.result_visibility,
    }


def visibility_gate(election: Election) -> dict:
    """What may be shown for candidate-by-candidate totals right now."""
    state = election.state
    vis = election.result_visibility
    if state in (Election.State.PUBLISHED,):
        return {"show_totals": True, "show_candidate_breakdown": True, "reason": "published"}
    if state == Election.State.ARCHIVED:
        return {"show_totals": True, "show_candidate_breakdown": True, "reason": "archived"}
    if vis == Election.ResultVisibility.LIVE_AGGREGATE and state in (
        Election.State.OPEN,
        Election.State.CLOSED,
        Election.State.RECONCILIATION,
        Election.State.REVIEW,
        Election.State.CERTIFIED,
    ):
        return {"show_totals": True, "show_candidate_breakdown": True, "reason": "live_aggregate_policy"}
    if vis == Election.ResultVisibility.WITHHELD_UNTIL_CLOSE and state == Election.State.OPEN:
        return {"show_totals": True, "show_candidate_breakdown": False, "reason": "withheld_until_close"}
    if state in (Election.State.CLOSED, Election.State.RECONCILIATION, Election.State.REVIEW):
        # Pre-publication, breakdown visible only to officials/auditors —
        # the view layer applies role checks; this flags policy position.
        return {"show_totals": True, "show_candidate_breakdown": False, "reason": "pre_publication"}
    if state == Election.State.CERTIFIED:
        return {"show_totals": True, "show_candidate_breakdown": False, "reason": "certified_not_published"}
    return {"show_totals": False, "show_candidate_breakdown": False, "reason": "withheld"}
