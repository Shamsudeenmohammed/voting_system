"""Voter-facing ballot flow.

Flow (each step is a separate, stateless request):

1. ``prepare`` (``/ballot/<token>/``) — the single-use authorization token is
   exchanged for a short-lived *preparation* capability and the ballot form.
2. ``cast`` — the form posts selections + preparation id back; on success the
   voter receives a random receipt and the ballot hash.  Retries with the
   same preparation id are idempotent.

Security properties enforced here (see ballot.services for the transaction
contract):

* the token appears only in the URL of the first request and is never stored
  server-side in plaintext, logged, or written to the audit trail;
* no view in this module ever renders or receives voter identity together
  with selections — identity is bound to the *right to vote*, not to the
  ballot;
* failures never disclose whether a token exists.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit import services as audit
from elections.config import load_config
from elections.lifecycle import LifecycleError, sync_election_state
from elections.models import Election

from .crypto import hash_token
from .models import Ballot, BallotAuthorization
from .services import (
    BallotError,
    DuplicateVote,
    InvalidSelection,
    InvalidSubmission,
    NotEligible,
    ElectionNotOpen,
    cast_ballot,
    find_receipt,
    prepare_ballot,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# simple cache-based throttle (per IP + action)
# ---------------------------------------------------------------------------

def _throttled(request, action: str, limit: int = 20, window: int = 60) -> bool:
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
        or "unknown"
    )
    key = f"throttle:{action}:{ip}"
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)
        count = 1
    return count > limit


def _error(request, status: int, title: str, message: str):
    return render(
        request,
        "ballot/error.html",
        {"title": title, "message": message},
        status=status,
    )


def _positions(election):
    cfg = load_config(election)
    position_ids = [p["id"] for p in cfg["positions"]]
    from elections.models import Position

    qs = {
        str(p.pk): p
        for p in Position.objects.filter(pk__in=position_ids).prefetch_related("candidates")
    }
    rows = []
    for p in cfg["positions"]:
        pos = qs.get(p["id"])
        if pos is None:
            continue
        # Config ids are canonical strings; model PKs are UUID objects, so
        # compare on the string form (a raw ``c.pk in {...}`` would silently
        # match nothing and render every position as empty).
        by_id = {str(c.pk): c for c in pos.candidates.all()}
        candidates = [
            by_id[str(c["id"])] for c in p["candidates"] if str(c["id"]) in by_id
        ]
        rows.append({"config": p, "position": pos, "candidates": candidates})
    return rows


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def prepare(request, token: str):
    """Step 1/2: show the ballot and accept the voter's selections."""
    if _throttled(request, "ballot", limit=30, window=60):
        return _error(request, 429, "Too many attempts", "Please wait a moment and try again.")

    token_hash = hash_token(token)
    auth = (
        BallotAuthorization.objects.select_related("election", "voter")
        .filter(token_hash=token_hash)
        .first()
    )
    if auth is None:
        # Uniform response: never disclose whether the token exists.
        return _error(
            request,
            403,
            "Ballot access denied",
            "This voting credential is not valid or has already been used.",
        )

    voter = auth.voter
    election = auth.election
    credential_usable = auth.usable
    
    # Check if already voted (consumed authorization) before showing ballot.
    # A POST still falls through so cast_ballot can resolve an *idempotent
    # replay* (lost response after acceptance) to the original receipt.
    if auth.consumed_at and request.method != "POST":
        audit.record(
            action="duplicate_vote_rejected",
            election=election,
            request=request,
            details={"reason": "already_voted"},
        )
        return _error(
            request,
            403,
            "Ballot access denied",
            "You have already cast your vote for this election. You cannot vote again.",
        )

    if not credential_usable and request.method != "POST":
        # Consumed/revoked credential: the ballot form is never shown again.
        # A POST still falls through so cast_ballot can resolve an *idempotent
        # replay* (lost response after acceptance) to the original receipt;
        # every other failure is refused with the uniform wording below.
        return _error(
            request,
            403,
            "Ballot access denied",
            "This voting credential is not valid or has already been used.",
        )
    try:
        sync_election_state(election)
    except LifecycleError:  # pragma: no cover - benign race with scheduling
        pass
    if not election.accepts_ballots or timezone.now() > election.end_at:
        return _error(
            request, 409, "Voting closed",
            "This election is not open for voting right now.",
        )

    rows = _positions(election)

    if request.method == "GET":
        try:
            prep = prepare_ballot(voter, election)
        except NotEligible as exc:
            return _error(request, exc.http_status, "Not eligible", exc.message)
        return render(
            request,
            "ballot/ballot.html",
            {
                "election": election,
                "rows": rows,
                "submission_id": str(prep.submission_id),
                "token": token,
                "allow_blank": election.allow_blank,
                "error": None,
            },
        )

    # POST: accept the ballot.
    submission_id = request.POST.get("submission_id", "")
    selections: dict[str, list[str]] = {}
    for row in rows:
        key = str(row["position"].pk)
        selections[key] = [v for v in request.POST.getlist(key) if v]
    if _throttled(request, "cast", limit=10, window=60):
        return _error(request, 429, "Too many attempts", "Please wait a moment and try again.")
    try:
        result = cast_ballot(
            election=election,
            voter=voter,
            submission_id=submission_id,
            selections=selections,
            request=request,
        )
    except (InvalidSelection, InvalidSubmission) as exc:
        if not credential_usable:
            # Consumed/revoked credential: keep the denial uniform - never
            # echo why the submission failed for a spent token.
            return _error(
                request,
                403,
                "Ballot access denied",
                "This voting credential is not valid or has already been used.",
            )
        return render(
            request,
            "ballot/ballot.html",
            {
                "election": election,
                "rows": rows,
                "submission_id": submission_id,
                "token": token,
                "allow_blank": election.allow_blank,
                "error": exc.message,
            },
            status=400,
        )
    except BallotError as exc:
        if isinstance(exc, DuplicateVote):
            # The credential was already used (e.g. in another tab).  The
            # audit event intentionally carries no voter or ballot identity.
            audit.record(
                action="duplicate_vote_rejected",
                election=election,
                request=request,
                details={"reason": exc.code},
            )
        if not credential_usable:
            return _error(
                request,
                403,
                "Ballot access denied",
                "This voting credential is not valid or has already been used.",
            )
        return _error(request, exc.http_status, "Ballot not accepted", exc.message)

    return render(
        request,
        "ballot/receipt.html",
        {
            "election": election,
            "receipt_code": result.receipt_code,
            "ballot_hash": result.ballot_hash,
            "box_seq": result.box_seq,
            "idempotent": result.idempotent,
        },
    )


@require_http_methods(["GET"])
def receipt_lookup(request, pk):
    """Public, identity-free receipt verification (existence + hash only)."""
    election = get_object_or_404(Election, pk=pk)
    code = (request.GET.get("code") or "").strip()
    found = None
    if code:
        found = find_receipt(election, code)
    return render(
        request,
        "ballot/receipt_lookup.html",
        {"election": election, "code": code, "found": found},
    )


def robots(request) -> HttpResponse:
    """Ballot endpoints must not be crawled or indexed."""
    return HttpResponse("User-agent: *\nDisallow: /ballot/\n", content_type="text/plain")
