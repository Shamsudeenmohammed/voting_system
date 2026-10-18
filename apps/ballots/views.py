"""Voting booth views.

Flow: 1) voter session (voters app) → 2) GET /vote/<slug>/ ballot form →
3) POST authorization issuance (returns raw token once, in-session only) →
4) POST ballot submission with token + selections + submission_id →
5) confirmation page showing ONLY acceptance + receipt (never choices).

Session hygiene: the token and selections are held only in the voter's
server-side session for the duration of the ballot — cleared on success.
They are never written to logs or URLs.
"""
from __future__ import annotations

import logging
import uuid

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.elections.models import Election, PollingStation
from .models import validate_selections
from .services import BallotError, issue_grant, submit_ballot
from apps.voters.views import get_voter

logger = logging.getLogger("ballots")

TOKEN_SESSION_KEY = "ballot_token"
SUBMISSION_SESSION_KEY = "ballot_submission"


def _require_voter(request, slug):
    voter = get_voter(request)
    if voter is None:
        request.session["next_vote"] = slug
        return None, redirect("voters:voter-login")
    election = get_object_or_404(
        Election.objects.select_related("organization"), slug=slug,
        organization=voter.organization,
    )
    return (voter, election), None


@never_cache
@require_http_methods(["GET", "POST"])
def booth(request, slug):
    """Ballot form. GET renders; POST issues an authorization token."""
    ctx, redir = _require_voter(request, slug)
    if redir:
        return redir
    voter, election = ctx

    from apps.voters.policy import is_eligible

    eligible, reason = is_eligible(election, voter)
    if not eligible:
        return render(
            request,
            "ballots/blocked.html",
            {"election": election, "reason": reason},
        )
    if not election.can_accept_ballots:
        return render(request, "ballots/closed.html", {"election": election})

    positions = list(
        election.positions.filter(is_active=True).prefetch_related(
            "candidates"
        ).order_by("order", "slug")
    )
    for pos in positions:
        pos.approved_candidates = list(
            pos.candidates.filter(status="approved").order_by("order", "full_name")
        )

    if request.method == "POST":
        # Issue (or rotate) authorization — identity side only.
        try:
            grant, raw_token = issue_grant(election, voter)
        except BallotError as exc:
            if exc.code == "already_voted":
                return render(
                    request,
                    "ballots/already_voted.html",
                    {"election": election},
                )
            messages.error(request, str(exc))
            return render(
                request,
                "ballots/booth.html",
                {"election": election, "positions": positions},
            )
        request.session[TOKEN_SESSION_KEY] = raw_token
        request.session[SUBMISSION_SESSION_KEY] = str(uuid.uuid4())
        request.session.modified = True
        return render(
            request,
            "ballots/booth.html",
            {
                "election": election,
                "positions": positions,
                "issued": True,
            },
        )

    return render(
        request,
        "ballots/booth.html",
        {"election": election, "positions": positions, "issued": False},
    )


@require_POST
def submit(request, slug):
    ctx, redir = _require_voter(request, slug)
    if redir:
        return redirect("voters:voter-login")
    voter, election = ctx

    raw_token = request.session.pop(TOKEN_SESSION_KEY, "")
    submission_id = request.session.pop(SUBMISSION_SESSION_KEY, "")
    request.session.modified = True
    if not raw_token or not submission_id:
        messages.error(
            request,
            "Your ballot session expired before submission. Request a new ballot.",
        )
        return redirect("ballots:booth", slug=slug)

    # Collect selections from the posted form (never from URL query strings).
    selections: dict = {}
    for pos in election.positions.filter(is_active=True):
        key = f"pos_{pos.slug}"
        if pos.max_choices == 1:
            val = request.POST.get(key)
            if val:
                selections[pos.slug] = [val]
        else:
            vals = request.POST.getlist(key)
            if vals:
                selections[pos.slug] = vals
    if request.POST.get("abstain_all") == "1" and election.allow_abstain:
        selections = {
            p.slug: "abstain" for p in election.positions.filter(is_active=True)
        }

    station = None
    station_id = request.POST.get("station")
    if station_id:
        station = PollingStation.objects.filter(
            pk=station_id, election=election
        ).first()

    try:
        result = submit_ballot(
            election=election,
            raw_token=raw_token,
            selections=selections,
            submission_id=submission_id,
            station=station,
        )
    except BallotError as exc:
        logger.info("ballot.rejected code=%s election=%s", exc.code, election.pk)
        messages.error(request, str(exc))
        return redirect("ballots:booth", slug=slug)

    return render(
        request,
        "ballots/confirmation.html",
        {
            "election": election,
            "receipt": result["receipt"],
            "idempotent": result.get("idempotent", False),
        },
    )


@require_POST
def revoke(request, slug):
    """Voter may invalidate an un-submitted (issued, unconsumed) token —
    e.g. after a suspected shoulder-surf — then request a fresh ballot."""
    ctx, redir = _require_voter(request, slug)
    if redir:
        return redirect("voters:voter-login")
    voter, election = ctx
    from apps.ballots.models import BallotGrant
    from apps.ballots.services import revoke_grant
    from .privacy import hash_token

    raw_token = request.session.get(TOKEN_SESSION_KEY, "")
    if not raw_token:
        messages.info(request, "No active ballot token to invalidate.")
        return redirect("ballots:booth", slug=slug)
    grant = BallotGrant.objects.filter(
        token_hash=hash_token(raw_token), election=election
    ).first()
    if grant:
        revoke_grant(grant, request.user if request.user.is_authenticated else None,
                     "voter-initiated invalidation")
    request.session.pop(TOKEN_SESSION_KEY, None)
    request.session.pop(SUBMISSION_SESSION_KEY, None)
    messages.success(request, "Authorization invalidated. Request a new ballot.")
    return redirect("ballots:booth", slug=slug)
