"""Ballot issuance and acceptance — the transactional voting engine.

Guarantees implemented here (tested in apps/ballots/tests):

* election-state gate read inside the locked transaction (ballots rejected
  the moment the election closes, even mid-flight);
* single-use authorization: one grant row per (election, voter) with a
  single-use token; consumption happens in the same transaction as ballot
  creation — a failed transaction leaves the grant ISSUED (retryable) and
  no ballot;
* exactly-once acceptance: UNIQUE(election, submission_id) + locked grant
  status transitions prevent duplicate ballots under concurrent requests;
* idempotent retries: repeating a completed submission returns the original
  receipt (derived deterministically from the client-held submission_id)
  without revealing selections;
* selections never logged; only outcome codes.

The tallying component reads ONLY ``AcceptedBallot`` — it never joins to
``BallotGrant`` or voter tables (enforced in apps.tally.engine).
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AcceptedBallot, BallotGrant, validate_selections
from .privacy import expires_at, hash_receipt, hash_token, new_token, quantize_time, seal_selections

logger = logging.getLogger("ballots")


class BallotError(Exception):
    """Domain error with a safe public message and a machine code."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


def _derive_receipt(submission_id) -> str:
    """Receipt = HMAC(key, submission_id) — reproducible on idempotent retry
    without storing plaintext receipts or linking voter identity."""
    import hmac as hmac_mod

    from django.conf import settings as dj_settings

    key = (dj_settings.BALLOT_ENCRYPTION_KEY or "dev").encode()
    return hmac_mod.new(key, f"receipt:{submission_id}".encode(), "sha256").hexdigest()


def issue_grant(election, voter) -> tuple[BallotGrant, str]:
    """Issue (or rotate) the single-use ballot authorization.

    Exactly one grant row exists per (election, voter) — rotation replaces
    the token hash, instantly invalidating any previously issued token.
    Returns (grant, raw_token); the raw token is shown once, never stored.
    """
    from apps.voters.policy import is_eligible

    if not election.can_accept_ballots:
        raise BallotError("not_open", "This election is not currently open for voting.", 403)

    eligible, reason = is_eligible(election, voter)
    if not eligible:
        logger.info("grant.denied code=%s election=%s", reason, election.pk)
        raise BallotError("ineligible", "You are not eligible to vote in this election.", 403)

    voter_ref = str(voter.pk)
    raw_token, token_hash = new_token()
    now = timezone.now()

    with transaction.atomic():
        grant = (
            BallotGrant.objects.select_for_update()
            .filter(election=election, voter_ref=voter_ref)
            .first()
        )
        if grant is None:
            grant = BallotGrant.objects.create(
                election=election,
                voter_ref=voter_ref,
                token_hash=token_hash,
                issued_at=quantize_time(now),
                expires_at=expires_at(),
            )
        else:
            if grant.status == BallotGrant.Status.CONSUMED:
                raise BallotError(
                    "already_voted", "You have already voted in this election.", 403
                )
            if grant.status == BallotGrant.Status.REVOKED:
                raise BallotError(
                    "authorization_revoked",
                    "Your ballot authorization was revoked. Contact an election official.",
                    403,
                )
            # ISSUED → rotate token (lost/re-requested token case).
            grant.token_hash = token_hash
            grant.issued_at = quantize_time(now)
            grant.expires_at = expires_at()
            grant.save(update_fields=["token_hash", "issued_at", "expires_at", "updated_at"])

        from apps.audit.services import record_audit

        record_audit(
            organization=election.organization,
            action="ballot.grant_issued",
            object_type="Election",
            object_id=str(election.pk),
            payload={"election": election.slug, "voter_ref": voter_ref},
        )
    return grant, raw_token


def revoke_grant(grant, actor, reason: str) -> None:
    if grant.status != BallotGrant.Status.ISSUED:
        raise BallotError("not_revocable", "Grant is no longer active.", 400)
    with transaction.atomic():
        locked = BallotGrant.objects.select_for_update().get(pk=grant.pk)
        if locked.status != BallotGrant.Status.ISSUED:
            raise BallotError("not_revocable", "Grant is no longer active.", 400)
        locked.status = BallotGrant.Status.REVOKED
        locked.save(update_fields=["status", "updated_at"])
        from apps.audit.services import record_audit

        record_audit(
            organization=locked.election.organization,
            actor=actor,
            action="ballot.grant_revoked",
            object_type="Election",
            object_id=str(locked.election_id),
            payload={"voter_ref": locked.voter_ref, "reason": reason[:200]},
        )


def submit_ballot(*, election, raw_token: str, selections: dict, submission_id, station=None) -> dict:
    """Atomically accept a ballot.

    Returns {"receipt": ..., "ballot_id": ..., "idempotent": bool}.
    Raises BallotError on rejection — the grant is left untouched (ISSUED and
    retryable) for every rejection path.
    """
    if not raw_token or not isinstance(raw_token, str):
        raise BallotError("bad_token", "Ballot authorization token missing.", 400)

    token_hash = hash_token(raw_token)

    # Validate selections BEFORE consuming anything (no side effects yet).
    try:
        normalized = validate_selections(election, selections)
    except Exception as exc:  # django ValidationError → domain error
        from django.core.exceptions import ValidationError as DjangoValidationError

        if isinstance(exc, DjangoValidationError):
            msgs = exc.messages if hasattr(exc, "messages") else ["Invalid ballot."]
            raise BallotError("invalid_ballot", msgs[0], 400) from exc
        raise

    try:
        with transaction.atomic():
            grant = (
                BallotGrant.objects.select_for_update()
                .filter(token_hash=token_hash, election=election)
                .first()
            )
            if grant is None:
                raise BallotError("bad_token", "Invalid authorization token.", 401)

            # --- Idempotent retry path (grant already consumed) -------------
            if grant.status == BallotGrant.Status.CONSUMED:
                existing = AcceptedBallot.objects.filter(
                    election=election, submission_id=submission_id
                ).first()
                if existing is None:
                    raise BallotError(
                        "already_voted",
                        "This ballot authorization was already used.",
                        409,
                    )
                receipt = _derive_receipt(existing.submission_id)
                if hash_receipt(receipt) != existing.receipt_hash:
                    raise BallotError("integrity", "Receipt verification failed.", 500)
                return {"receipt": receipt, "ballot_id": str(existing.pk), "idempotent": True}

            if grant.status == BallotGrant.Status.REVOKED:
                raise BallotError(
                    "revoked", "This ballot authorization was revoked.", 403
                )
            if grant.expires_at <= timezone.now():
                raise BallotError(
                    "expired", "Authorization expired. Request a new ballot.", 403
                )

            # Re-read election state inside the transaction: closure during
            # this request must reject the ballot.
            from apps.elections.models import Election

            fresh = Election.objects.select_for_update().filter(pk=election.pk).first()
            if fresh is None or not fresh.can_accept_ballots:
                raise BallotError(
                    "closed", "This election is no longer accepting ballots.", 403
                )

            # submission_id collision with a different (unconsumed) ballot.
            if AcceptedBallot.objects.filter(
                election=fresh, submission_id=submission_id
            ).exists():
                raise BallotError(
                    "submission_conflict",
                    "Submission identifier conflict. Retry with a new one.",
                    409,
                )

            sealed, digest = seal_selections(normalized)
            receipt = _derive_receipt(submission_id)

            try:
                AcceptedBallot.objects.create(
                    election=fresh,
                    station=station,
                    submission_id=submission_id,
                    sealed_payload=sealed,
                    payload_sha256=digest,
                    receipt_hash=hash_receipt(receipt),
                    accepted_at=timezone.now(),
                )
            except IntegrityError as exc:
                logger.info("ballot.integrity_conflict election=%s", fresh.pk)
                raise BallotError(
                    "submission_conflict",
                    "Duplicate submission detected. No ballot was recorded.",
                    409,
                ) from exc

            # Consume authorization in the same transaction as the insert.
            grant.status = BallotGrant.Status.CONSUMED
            grant.save(update_fields=["status", "consumed_at", "updated_at"])

            from apps.audit.services import record_audit

            # Outcome-only audit — no selections, no receipt, no voter id.
            record_audit(
                organization=fresh.organization,
                action="ballot.accepted",
                object_type="Election",
                object_id=str(fresh.pk),
                payload={"election": fresh.slug},
            )
    except BallotError:
        raise
    except IntegrityError as exc:
        logger.warning("ballot.integrity_error election=%s", election.pk)
        raise BallotError(
            "conflict", "Your submission could not be processed. Please retry.", 409
        ) from exc

    logger.info(
        "ballot.accepted election=%s station=%s",
        election.pk,
        station.pk if station else None,
    )
    return {"receipt": receipt, "ballot_id": None, "idempotent": False}


def turnout_counts(election) -> dict:
    """Aggregate turnout (identity side only — safe for dashboards)."""
    grants = BallotGrant.objects.filter(election=election)
    consumed = grants.filter(status=BallotGrant.Status.CONSUMED).count()
    accepted = AcceptedBallot.objects.filter(election=election).count()
    from apps.voters.policy import eligible_count

    eligible = eligible_count(election)
    pct = round(100.0 * accepted / eligible, 2) if eligible else 0.0
    return {
        "eligible": eligible,
        "authorized": grants.filter(status=BallotGrant.Status.ISSUED).count(),
        "consumed_grants": consumed,
        "accepted_ballots": accepted,
        "turnout_pct": pct,
        # Reconciliation signal: consumed grants must equal accepted ballots.
        "reconciled": consumed == accepted,
    }
