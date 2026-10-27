"""Ballot authorization, preparation and acceptance services.

Transaction / concurrency contract (tested in ``ballot/tests``):

* Acceptance runs in **one** database transaction that (a) locks the
  per-election :class:`BallotBox` row, (b) re-checks the election state,
  (c) consumes the authorization with a conditional ``UPDATE ... WHERE
  consumed_at IS NULL``, (d) inserts the sealed ballot, (e) deletes the
  preparation capability, (f) marks the voter as having voted and (g) appends
  an identity-free audit event.
* If anything fails the transaction rolls back completely: an authorization
  can never be consumed without its ballot, and a ballot can never exist
  without its state changes (exactly-once).
* The ballot row holds **no** voter / authorization foreign key.  The only
  link (authorization <-> submission id) is destroyed when the ballot is
  accepted, so no durable path from a voter to their selections exists.
* Retries with the same ``submission_id`` after a lost response return the
  original receipt without creating a second ballot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger("votesys.ballot")


class BallotError(Exception):
    """Base class for ballot flow failures (safe to show to the voter)."""

    code = "ballot_error"
    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ElectionNotOpen(BallotError):
    code = "election_not_open"
    http_status = 409


class NotEligible(BallotError):
    code = "not_eligible"
    http_status = 403


class InvalidSubmission(BallotError):
    code = "invalid_submission"
    http_status = 400


class DuplicateVote(BallotError):
    code = "duplicate_vote"
    http_status = 409


class TokenIssuanceError(BallotError):
    code = "token_issuance_blocked"
    http_status = 409


class InvalidSelection(BallotError):
    code = "invalid_selection"
    http_status = 400


@dataclass
class CastResult:
    receipt_code: str
    ballot_hash: str
    box_seq: int
    idempotent: bool = False


# ---------------------------------------------------------------------------
# Credential issuance
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass
class TokenIssuanceResult:
    status: str
    voter: object
    authorization: object
    token: str | None
    message: str


@dataclass
class TokenRevocationResult:
    status: str
    voter: object
    authorization: object
    revoked: int
    message: str


ALREADY_VOTED_MESSAGE = "This voter has already cast their vote. No new token can be issued."


def _actor_is_authenticated(actor) -> bool:
    return bool(getattr(actor, "is_authenticated", False))


def issue_voter_token(
    voter,
    *,
    election=None,
    actor=None,
    request=None,
    allow_reissue: bool = False,
    reason: str = "",
    require_open: bool = True,
):
    """Shared, server-side token-issuance service.

    Enforces, in order: voter exists; eligibility; open election; correct
    assignment/scope; not already voted; no active valid token.  Returns a
    :class:`TokenIssuanceResult`.  A plaintext token is returned only when a
    new authorization is actually issued.
    """
    from audit import services as audit
    from ballot import crypto
    from ballot.models import BallotAuthorization
    from elections.models import Election, Voter

    now = timezone.now()

    if voter is None:
        return TokenIssuanceResult(
            status="unregistered",
            voter=None,
            authorization=None,
            token=None,
            message="This voter is not registered for this election.",
        )

    election = election or getattr(voter, "election", None)
    if election is None:
        return TokenIssuanceResult(
            status="unregistered",
            voter=voter,
            authorization=None,
            token=None,
            message="This voter is not registered for this election.",
        )

    with transaction.atomic():
        try:
            voter_row = Voter.objects.select_for_update().get(pk=voter.pk)
        except Voter.DoesNotExist:
            return TokenIssuanceResult(
                status="unregistered",
                voter=None,
                authorization=None,
                token=None,
                message="This voter is not registered for this election.",
            )
        election_row = Election.objects.select_for_update().get(pk=election.pk)

        # 1. voter exists (handled above)
        # 2. eligible
        if voter_row.status != Voter.STATUS_ACTIVE or not voter_row.eligible_for(election_row):
            return TokenIssuanceResult(
                status="ineligible",
                voter=voter_row,
                authorization=None,
                token=None,
                message="This voter is not eligible for this election.",
            )

        # 3. election open and permits voting (when required)
        if require_open and (not election_row.accepts_ballots or now > election_row.end_at):
            return TokenIssuanceResult(
                status="election_closed",
                voter=voter_row,
                authorization=None,
                token=None,
                message="This election is not open for voting.",
            )

        # 4. correct institution/election/scope
        if voter_row.election_id != election_row.pk:
            return TokenIssuanceResult(
                status="ineligible",
                voter=voter_row,
                authorization=None,
                token=None,
                message="This voter is not assigned to this election.",
            )

        # 5. has not already cast an accepted ballot
        already_voted = voter_row.has_voted or BallotAuthorization.objects.filter(
            voter=voter_row, consumed_at__isnull=False
        ).exists()
        if already_voted:
            auth = BallotAuthorization.objects.filter(
                voter=voter_row, consumed_at__isnull=False
            ).order_by("-consumed_at").first()
            return TokenIssuanceResult(
                status="already_voted",
                voter=voter_row,
                authorization=auth,
                token=None,
                message=ALREADY_VOTED_MESSAGE,
            )

        # Existing authorizations
        active_qs = BallotAuthorization.objects.filter(
            voter=voter_row,
            election=election_row,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
        )
        active_usable = [a for a in active_qs if a.usable]

        # 6. no valid/active token already exists (unless explicit reissue)
        if active_usable and not (allow_reissue and reason and reason.strip()):
            auth = active_usable[0]
            return TokenIssuanceResult(
                status="active_token",
                voter=voter_row,
                authorization=auth,
                token=None,
                message="A valid voting token is already active for this voter. No duplicate token generated.",
            )

        # Explicit, audited revocation-and-reissue path.
        if allow_reissue and reason and reason.strip():
            revoked = active_qs.update(revoked_at=now)
            audit.record(
                action="credential_revoked",
                actor=actor if _actor_is_authenticated(actor) else None,
                election=election_row,
                organization=getattr(election_row, "organization", None),
                request=request,
                object_type="voter",
                object_id=str(voter_row.pk),
                reason=reason,
                details={"revoked": revoked, "reissue": True},
            )

        # Revoke any expired/lingering unused authorizations before issuing.
        BallotAuthorization.objects.filter(
            voter=voter_row,
            election=election_row,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__lt=now,
        ).update(revoked_at=now)
        BallotAuthorization.objects.filter(
            voter=voter_row,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
        ).update(revoked_at=now)

        token = crypto.new_voter_token(election_row.token_format)
        token_hash = crypto.hash_token(token)
        auth = BallotAuthorization.objects.create(
            election=election_row,
            voter=voter_row,
            token_hash=token_hash,
            issued_by=actor if _actor_is_authenticated(actor) else None,
            expires_at=_compute_token_expiry(election_row),
        )
        voter_row.credentials_issued_at = now
        voter_row.save(update_fields=["credentials_issued_at"])

        audit.record(
            action="credential_issued",
            actor=actor if _actor_is_authenticated(actor) else None,
            election=election_row,
            organization=getattr(election_row, "organization", None),
            request=request,
            object_type="voter",
            object_id=str(voter_row.pk),
            details={"voter_id_hash": audit.hash_ip(voter_row.voter_id)},
        )
        reissued = bool(allow_reissue and reason and reason.strip())
        return TokenIssuanceResult(
            status="reissued" if reissued else "issued",
            voter=voter_row,
            authorization=auth,
            token=token,
            message=(
                "Token replaced — the previous token is no longer valid."
                if reissued
                else "Eligible — token issued."
            ),
        )


def issue_authorization(voter, *, actor=None, token: str | None = None, election=None, request=None, **kwargs):
    """Backwards-compatible wrapper around :func:`issue_voter_token`.

    Raises :class:`TokenIssuanceError` when issuance is blocked, so existing
    callers that expect a usable token keep their contract.
    """
    result = issue_voter_token(
        voter,
        election=election,
        actor=actor,
        request=request,
        allow_reissue=kwargs.pop("allow_reissue", False),
        reason=kwargs.pop("reason", ""),
        require_open=kwargs.pop("require_open", True),
    )
    if result.token is None:
        raise TokenIssuanceError(result.message)
    return result.authorization, result.token


def _compute_token_expiry(election):
    """Compute token expiry based on election configuration."""
    if election.token_expiry_hours and election.token_expiry_hours > 0:
        return timezone.now() + timedelta(hours=election.token_expiry_hours)
    return None


def issue_missing_credentials(election, *, actor=None, request=None) -> dict:
    """Issue authorizations for every active voter without one.

    Returns ``{"issued": n, "file": path or None}``; tokens are appended to
    the election's private credentials file for out-of-band distribution.
    """
    from ballot.models import BallotAuthorization
    from ballot.credentials import append_credentials
    from elections.models import Voter

    issued_tokens: list[tuple[str, str, str]] = []
    missing = Voter.objects.filter(
        election=election, status=Voter.STATUS_ACTIVE, has_voted=False
    ).exclude(
        id__in=BallotAuthorization.objects.filter(
            election=election, consumed_at__isnull=True, revoked_at__isnull=True
        ).values("voter_id")
    )
    for voter in missing.iterator():
        result = issue_voter_token(voter, election=election, actor=actor, require_open=False)
        if result.token is not None:
            issued_tokens.append((voter.voter_id, result.token, voter.full_name))
    path = None
    if issued_tokens:
        path = append_credentials(election, issued_tokens)
    return {"issued": len(issued_tokens), "file": str(path) if path else None}


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def prepare_ballot(voter, election) -> "object":
    """Create the short-lived capability used for idempotent submission."""
    from ballot.models import BallotAuthorization, BallotPreparation

    auth = BallotAuthorization.objects.filter(
        voter=voter, election=election, consumed_at__isnull=True, revoked_at__isnull=True
    ).first()
    if auth is None:
        raise NotEligible("No valid voting credential was found for you.")
    if not auth.usable:
        raise NotEligible("Your voting credential is no longer valid.")
    if not voter.eligible_for(election):
        raise NotEligible("You are not eligible to vote in this election.")
    # Drop stale preparations of this voter (form left open too long).
    BallotPreparation.objects.filter(
        authorization__voter=voter, expires_at__lt=timezone.now()
    ).delete()
    from ballot import crypto

    return BallotPreparation.objects.create(
        election=election,
        authorization=auth,
        submission_id=crypto.new_submission_id(),
        expires_at=timezone.now() + timedelta(minutes=20),
    )


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def _lock_box(election, *, increment: bool = False):
    """Create (if needed) and row-lock the per-election ballot box.

    Locking the box serialises ballot acceptance and closing/reopening so the
    close boundary is exact.  ``last_seq`` is *not* advanced here: it is set
    atomically together with the ballot insert (see ``cast_ballot``), which
    keeps ``last_seq == number of accepted ballots`` even under retries.
    """
    from ballot.models import BallotBox

    try:
        with transaction.atomic():
            BallotBox.objects.create(election_id=election.pk)
    except IntegrityError:
        pass  # box already exists (savepoint rolled back)
    # Row-level lock held until the surrounding transaction ends.
    if increment:
        BallotBox.objects.filter(election_id=election.pk).update(
            last_seq=F("last_seq") + 1
        )
    else:
        BallotBox.objects.filter(election_id=election.pk).update(
            last_seq=F("last_seq")  # no-op update acquires the row lock
        )
    return BallotBox.objects.get(election_id=election.pk)


def _close_if_due(election) -> None:
    """Lazily enforce the scheduled closing boundary (system transition)."""
    from elections import lifecycle

    if election.state == election.STATE_OPEN and timezone.now() > election.end_at:
        lifecycle.transition(
            election,
            election.STATE_CLOSED,
            actor=None,
            reason="Scheduled end time reached (automatic).",
            allow_system=True,
        )
        election.refresh_from_db()
    elif election.state == election.STATE_SCHEDULED and timezone.now() >= election.start_at:
        lifecycle.transition(
            election,
            election.STATE_OPEN,
            actor=None,
            reason="Scheduled start time reached (automatic).",
            allow_system=True,
        )
        election.refresh_from_db()


def cast_ballot(
    *,
    election,
    voter,
    submission_id,
    selections: dict,
    station=None,
    request=None,
) -> CastResult:
    """Validate and durably accept one ballot.  See module docstring."""
    from audit import services as audit
    from ballot import crypto
    from ballot.models import Ballot, BallotAuthorization, BallotBox, BallotPreparation
    from elections import config as election_config
    from elections.models import Election, Voter

    _close_if_due(election)
    if election.state != Election.STATE_OPEN:
        raise ElectionNotOpen("This election is not open for voting.")
    if timezone.now() > election.end_at:
        raise ElectionNotOpen("Voting time has ended for this election.")
    if not voter.eligible_for(election):
        raise NotEligible("You are not eligible to vote in this election.")

    try:
        normalized = election_config.validate_selections(election, selections)
    except election_config.SelectionError as exc:
        raise InvalidSelection(str(exc)) from exc

    payload_blob = crypto.encrypt_payload(
        {
            "v": 1,
            "selections": normalized,
            "config_hash": election.config_hash,
        }
    )
    receipt = crypto.new_receipt_code()
    ballot_hash = crypto.ballot_hash(
        election_id=str(election.pk),
        submission_id=submission_id,
        receipt=receipt,
        payload=payload_blob,
        config_hash=election.config_hash,
    )

    with transaction.atomic():
        # Lock order: ballot box -> election -> authorization/preparation.
        # The box is locked WITHOUT advancing the sequence: last_seq only
        # advances when a ballot row is actually inserted, so retries and
        # rejected submissions can never create sequence drift.
        box = _lock_box(election, increment=False)
        fresh = (
            Election.objects.select_for_update().get(pk=election.pk)
        )
        if fresh.state != Election.STATE_OPEN or timezone.now() > fresh.end_at:
            raise ElectionNotOpen("This election is not open for voting.")
        if fresh.config_hash and fresh.config_hash != election.config_hash:
            # Extremely defensive: configuration changed underneath us.
            raise InvalidSelection("Election configuration changed; please reload the ballot.")

        prep = (
            BallotPreparation.objects.select_for_update()
            .filter(submission_id=submission_id, election_id=fresh.pk)
            .first()
        )
        if prep is None:
            # Retry after a committed-but-unacknowledged first attempt?
            existing = Ballot.objects.filter(
                election_id=fresh.pk, submission_id=submission_id
            ).first()
            auth_row = BallotAuthorization.objects.filter(
                voter_id=voter.pk, election_id=fresh.pk, consumed_at__isnull=False
            ).first()
            if existing is not None and auth_row is not None:
                return CastResult(
                    receipt_code=existing.receipt_code,
                    ballot_hash=existing.ballot_hash,
                    box_seq=existing.box_seq,
                    idempotent=True,
                )
            raise InvalidSubmission(
                "Your ballot session has expired. Please open the ballot again."
            )
        if prep.expires_at < timezone.now():
            # The short-lived preparation capability lapsed before submission.
            raise InvalidSubmission(
                "Your ballot session has expired. Please open the ballot again."
            )
        # Authoritative single-use guard (works on SQLite and PostgreSQL);
        # the voter/election match prevents cross-session misuse.
        consumed = BallotAuthorization.objects.filter(
            pk=prep.authorization_id,
            voter_id=voter.pk,
            election_id=fresh.pk,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
        ).update(consumed_at=timezone.now())
        if not consumed:
            existing = Ballot.objects.filter(
                election_id=fresh.pk, submission_id=submission_id
            ).first()
            if existing is not None:
                return CastResult(
                    receipt_code=existing.receipt_code,
                    ballot_hash=existing.ballot_hash,
                    box_seq=existing.box_seq,
                    idempotent=True,
                )
            raise DuplicateVote(
                "You have already cast your vote for this election. You cannot vote again."
            )

        voter_row = Voter.objects.select_for_update().get(pk=voter.pk)
        if voter_row.status != Voter.STATUS_ACTIVE or not voter_row.eligible_for(fresh):
            raise NotEligible("You are not eligible to vote in this election.")

        box_seq = box.last_seq + 1
        ballot = Ballot.objects.create(
            election_id=fresh.pk,
            submission_id=submission_id,
            receipt_code=receipt,
            payload=payload_blob,
            ballot_hash=ballot_hash,
            config_hash=fresh.config_hash,
            station_id=station.pk if station else voter_row.station_id,
            box_seq=box_seq,
            accepted_at=timezone.now(),
        )
        BallotBox.objects.filter(pk=box.pk).update(last_seq=box_seq)
        BallotPreparation.objects.filter(pk=prep.pk).delete()
        Voter.objects.filter(pk=voter_row.pk).update(
            has_voted=True, voted_at=timezone.now()
        )
        # NOTE: intentionally contains neither voter, authorization, receipt
        # nor submission identifiers - it must not correlate identity to a
        # specific ballot beyond aggregate counts.
        audit.record(
            action="ballot_accepted",
            election=fresh,
            request=request,
            # Explicitly no IP hash: this event must not create any channel
            # that correlates a network identity with a specific ballot.
            ip_hash="",
            details={
                "box_seq": box_seq,
                # NB: neither voter, receipt, submission id nor selections —
                # only the neutral row identifier and content hash.
                "ballot_id": str(ballot.pk),
                "ballot_hash": ballot_hash,
            },
        )
    logger.info(
        "ballot accepted election=%s box_seq=%s", election.pk, box_seq
    )
    return CastResult(receipt_code=receipt, ballot_hash=ballot_hash, box_seq=box_seq)


def find_receipt(election, receipt_code: str):
    """Public, identity-free receipt lookup (existence + hash only)."""
    from ballot.models import Ballot

    return (
        Ballot.objects.filter(election=election, receipt_code=receipt_code)
        .values("id", "receipt_code", "ballot_hash", "accepted_at", "box_seq")
        .first()
    )


def revoke_credential(voter, *, actor=None, reason: str = "", request=None):
    """Revoke a voter's *unused* credential (registry correction/dispute).

    Accepted ballots are never touched: revocation cannot cancel a vote.
    """
    from ballot.models import BallotAuthorization
    from audit import services as audit

    revoked = BallotAuthorization.objects.filter(
        voter=voter, consumed_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
    audit.record(
        action="credential_revoked",
        actor=actor,
        election=voter.election,
        object_type="voter",
        object_id=str(voter.pk),
        reason=reason,
        request=request,
        details={"revoked": revoked},
    )
    return revoked


def revoke_voter_token(
    voter, *, actor=None, reason: str = "", request=None
) -> TokenRevocationResult:
    """Revoke a voter's unused token from the verification workstation.

    The action is refused after the voter has cast a ballot, even if a stale
    unused authorization exists.  The voter row is locked so a reissue and a
    revocation cannot interleave, and the conditional update protects against
    a ballot being accepted concurrently.
    """
    from audit import services as audit
    from ballot.models import BallotAuthorization
    from elections.models import Voter

    if voter is None:
        return TokenRevocationResult(
            status="unregistered",
            voter=None,
            authorization=None,
            revoked=0,
            message="This voter is not registered for this election.",
        )

    with transaction.atomic():
        try:
            voter_row = Voter.objects.select_for_update().get(pk=voter.pk)
        except Voter.DoesNotExist:
            return TokenRevocationResult(
                status="unregistered",
                voter=None,
                authorization=None,
                revoked=0,
                message="This voter is not registered for this election.",
            )

        consumed = (
            BallotAuthorization.objects.filter(
                voter=voter_row,
                election=voter_row.election,
                consumed_at__isnull=False,
            )
            .order_by("-consumed_at")
            .first()
        )
        if voter_row.has_voted or consumed is not None:
            return TokenRevocationResult(
                status="already_voted",
                voter=voter_row,
                authorization=consumed,
                revoked=0,
                message=(
                    "This voter has already cast their vote. "
                    "The token cannot be revoked or replaced."
                ),
            )

        unused = BallotAuthorization.objects.filter(
            voter=voter_row,
            election=voter_row.election,
            consumed_at__isnull=True,
            revoked_at__isnull=True,
        )
        authorization = unused.order_by("-issued_at").first()
        if authorization is None:
            return TokenRevocationResult(
                status="no_token",
                voter=voter_row,
                authorization=None,
                revoked=0,
                message="There is no unused voting token to revoke for this voter.",
            )

        revoked = unused.update(revoked_at=timezone.now())
        if not revoked:
            # A ballot was accepted between the guard and the update, or the
            # row disappeared.  Re-check and report accurately.
            consumed = BallotAuthorization.objects.filter(
                voter=voter_row,
                election=voter_row.election,
                consumed_at__isnull=False,
            ).first()
            if consumed is not None:
                return TokenRevocationResult(
                    status="already_voted",
                    voter=voter_row,
                    authorization=consumed,
                    revoked=0,
                    message=(
                        "This voter has already cast their vote. "
                        "The token cannot be revoked or replaced."
                    ),
                )
            return TokenRevocationResult(
                status="no_token",
                voter=voter_row,
                authorization=None,
                revoked=0,
                message="There is no unused voting token to revoke for this voter.",
            )

        audit.record(
            action="credential_revoked",
            actor=actor if _actor_is_authenticated(actor) else None,
            election=voter_row.election,
            organization=getattr(voter_row.election, "organization", None),
            request=request,
            object_type="voter",
            object_id=str(voter_row.pk),
            reason=reason,
            details={"revoked": revoked},
        )
        return TokenRevocationResult(
            status="revoked",
            voter=voter_row,
            authorization=authorization,
            revoked=revoked,
            message="Unused voting token revoked. No ballot was cast.",
        )
