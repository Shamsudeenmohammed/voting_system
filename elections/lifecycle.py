"""Backend-enforced election lifecycle state machine.

::

    DRAFT -> REGISTRATION -> CONFIG_REVIEW -> APPROVED -> SCHEDULED -> OPEN
        -> CLOSED -> RECONCILIATION -> REVIEW -> CERTIFIED -> PUBLISHED
        -> ARCHIVED      (+ documented exception: CLOSED -> OPEN reopen)

Enforcement rules (all also covered by tests):

* Only edges listed in :data:`TRANSITIONS` are possible - hiding buttons in
  the UI is never the enforcement mechanism.
* ``APPROVED``, ``REOPEN`` and ``DECERTIFY`` need an :class:`ElectionApproval`
  record decided by a user *different* from the requester (four-eyes).
* An election cannot reach ``OPEN`` (nor ``SCHEDULED``) without a complete,
  frozen configuration (positions + approved candidates + schedule).
* ``OPEN -> CLOSED`` is forced at ``end_at``; the backend rejects ballots for
  any election that is not in state ``OPEN``.
* Certified snapshots are superseded - never silently overwritten - whenever
  results go back into ``REVIEW``.
* Every transition appends an identity-audited, hash-chained event.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .config import compute_config_hash
from .models import Election, ElectionApproval

logger = logging.getLogger("votesys.lifecycle")

TRANSITIONS: dict[str, set[str]] = {
    Election.STATE_DRAFT: {Election.STATE_REGISTRATION, Election.STATE_ARCHIVED},
    Election.STATE_REGISTRATION: {Election.STATE_CONFIG_REVIEW, Election.STATE_DRAFT},
    Election.STATE_CONFIG_REVIEW: {Election.STATE_APPROVED, Election.STATE_REGISTRATION},
    Election.STATE_APPROVED: {Election.STATE_SCHEDULED, Election.STATE_CONFIG_REVIEW},
    Election.STATE_SCHEDULED: {Election.STATE_OPEN, Election.STATE_ARCHIVED},
    Election.STATE_OPEN: {Election.STATE_CLOSED},
    Election.STATE_CLOSED: {Election.STATE_RECONCILIATION, Election.STATE_OPEN},
    Election.STATE_RECONCILIATION: {Election.STATE_REVIEW},
    Election.STATE_REVIEW: {Election.STATE_CERTIFIED, Election.STATE_RECONCILIATION},
    Election.STATE_CERTIFIED: {Election.STATE_PUBLISHED, Election.STATE_REVIEW},
    Election.STATE_PUBLISHED: {Election.STATE_ARCHIVED, Election.STATE_REVIEW},
    Election.STATE_ARCHIVED: set(),
}

# Edges that require a persisted four-edges approval record.
DUAL_CONTROL: dict[tuple[str, str], str] = {
    (Election.STATE_CONFIG_REVIEW, Election.STATE_APPROVED): "approve_config",
    (Election.STATE_CLOSED, Election.STATE_OPEN): "reopen",
    (Election.STATE_CERTIFIED, Election.STATE_REVIEW): "decertify",
    (Election.STATE_PUBLISHED, Election.STATE_REVIEW): "decertify",
}

# Edges the system may take by itself (schedule driven).
AUTO_EDGES = {
    (Election.STATE_SCHEDULED, Election.STATE_OPEN),
    (Election.STATE_OPEN, Election.STATE_CLOSED),
}

# Terminal state requiring elevated rights to leave/archived from a
# certified/published state.
APPROVE_CAPABLE_TARGETS = {Election.STATE_ARCHIVED}


class LifecycleError(Exception):
    """Domain failure (safe message for the UI)."""

    code = "lifecycle_error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


def is_allowed(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------


def _complete_config(election) -> str | None:
    """Return an error message if the configuration cannot be frozen."""
    positions = list(election.positions.prefetch_related("candidates"))
    if not positions:
        return "Add at least one position before freezing the configuration."
    for pos in positions:
        if not any(c.status == "approved" for c in pos.candidates.all()):
            return f"Position '{pos.title}' has no approved candidate."
    return None


def freeze_config(election, *, actor=None, request=None) -> Election:
    """Freeze rules/schedule/registry and issue missing credentials."""
    from audit import services as audit
    from ballot import services as ballot_services

    if election.config_frozen_at:
        return election
    error = _complete_config(election)
    if error:
        raise LifecycleError(error, code="incomplete_config")
    now = timezone.now()
    election.config_hash = compute_config_hash(election)
    election.config_frozen_at = now
    election.registry_frozen_at = election.registry_frozen_at or now
    election.save(update_fields=["config_hash", "config_frozen_at", "registry_frozen_at"])
    from .config import invalidate_config_cache

    invalidate_config_cache(election)
    audit.record(
        action="configuration_frozen",
        actor=actor,
        election=election,
        request=request,
        details={"config_hash": election.config_hash},
        reason="Configuration frozen before voting opens.",
    )
    result = ballot_services.issue_missing_credentials(election, actor=actor, request=request)
    if result["issued"]:
        audit.record(
            action="credentials_issued",
            actor=actor,
            election=election,
            request=request,
            details={"issued": result["issued"], "file": result["file"]},
        )
    return election


def _maybe_enqueue_tally(election, *, actor=None, reason: str = "") -> None:
    """Compute an interim tally snapshot after the transition commits."""
    from django.db import transaction as db_transaction

    def _run():
        try:
            from tally import services as tally_services

            tally_services.compute_snapshot(election.pk, actor=actor, reason=reason)
        except Exception:  # pragma: no cover - surfaced via snapshot absence
            logger.exception("tally computation failed for election %s", election.pk)

    db_transaction.on_commit(_run)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _lock_ballot_box(election):
    from ballot.services import _lock_box

    return _lock_box(election, increment=False)


def _check_capability(election, actor, dst: str) -> None:
    from organizations import roles

    if dst in APPROVE_CAPABLE_TARGETS and election.state in (
        Election.STATE_CERTIFIED,
        Election.STATE_PUBLISHED,
    ):
        allowed = roles.can_approve_election(actor, election)
    else:
        allowed = roles.can_manage_election(actor, election)
    if not allowed:
        raise LifecycleError(
            "Your role is not permitted to perform this lifecycle change.",
            code="forbidden",
        )


def transition(
    election: Election,
    target: str,
    *,
    actor=None,
    reason: str = "",
    request=None,
    approval: ElectionApproval | None = None,
    allow_system: bool = False,
) -> Election:
    """Perform one state transition atomically (or raise :class:`LifecycleError`)."""
    from audit import services as audit

    edge: tuple[str, str] | None = None
    with transaction.atomic():
        # Lock order: ballot box -> election row (same as ballot acceptance),
        # so closing/reopening can never race an in-flight ballot.
        _lock_ballot_box(election)
        locked = Election.objects.select_for_update().get(pk=election.pk)
        src, dst = locked.state, target
        edge = (src, dst)

        if dst == src:
            raise LifecycleError(f"Election is already in state '{dst}'.", code="noop")
        if not is_allowed(src, dst):
            raise LifecycleError(
                f"Transition {src} -> {dst} is not permitted.",
                code="illegal_transition",
            )

        if actor is None:
            if not (allow_system and edge in AUTO_EDGES):
                raise LifecycleError(
                    "This transition requires an authenticated actor.",
                    code="forbidden",
                )
        else:
            _check_capability(locked, actor, dst)

        dual_kind = DUAL_CONTROL.get(edge)
        if dual_kind:
            if approval is None:
                raise LifecycleError(
                    "This transition requires an approved approval record.",
                    code="approval_required",
                )
            if (
                approval.status != ElectionApproval.STATUS_APPROVED
                or approval.kind != dual_kind
                or approval.election_id != locked.pk
                or approval.decided_by_id != (actor.pk if actor else None)
            ):
                raise LifecycleError(
                    "Approval record is not valid for this transition.",
                    code="approval_invalid",
                )
            if approval.requested_by_id and approval.requested_by_id == approval.decided_by_id:
                raise LifecycleError(
                    "Four-eyes rule: a different user must decide this approval.",
                    code="four_eyes",
                )
            if not approval.reason.strip():
                raise LifecycleError("A documented reason is mandatory.", code="reason_required")
            reason = approval.reason.strip() if not reason else reason

        now = timezone.now()
        previous = locked.state
        locked.state = dst
        locked.state_updated_at = now
        update_fields = ["state", "state_updated_at"]

        # --- side effects -------------------------------------------------
        if dst == Election.STATE_SCHEDULED:
            freeze_config(locked, actor=actor, request=request)
        elif dst == Election.STATE_OPEN:
            if not locked.config_frozen_at:
                # Safety net for grandfathered/legacy elections.
                freeze_config(locked, actor=actor, request=request)
            if locked.opened_at is None:
                locked.opened_at = now
                update_fields.append("opened_at")
            if previous == Election.STATE_CLOSED:
                locked.reopened_count += 1
                update_fields.append("reopened_count")
        elif dst == Election.STATE_CLOSED:
            locked.closed_at = now
            update_fields.append("closed_at")
        elif dst == Election.STATE_RECONCILIATION:
            # In tests or when running in non-production eager mode, compute
            # the tally synchronously so subsequent operations (certify/review)
            # can observe the snapshot within the same transaction context.
            from django.conf import settings

            compute_sync = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(
                settings, "TESTING", False
            )
            if compute_sync:
                from tally import services as tally_services

                tally_services.compute_snapshot(
                    locked.pk, actor=actor, reason=reason or "Initial count"
                )
            else:
                _maybe_enqueue_tally(
                    locked, actor=actor, reason=reason or "Initial count"
                )
        elif dst == Election.STATE_CERTIFIED:
            from tally import services as tally_services

            tally_services.certify_latest(locked, actor=actor, request=request)
            locked.certified_at = now
            update_fields.append("certified_at")
        elif dst == Election.STATE_PUBLISHED:
            locked.published_at = now
            update_fields.append("published_at")
            if not locked.is_published:
                locked.is_published = True
                update_fields.append("is_published")
        elif dst == Election.STATE_REVIEW and previous in (
            Election.STATE_CERTIFIED,
            Election.STATE_PUBLISHED,
        ):
            from tally import services as tally_services

            tally_services.supersede_certified(
                locked, actor=actor, reason=reason or "Correction requested"
            )

        if approval is not None:
            approval.status = ElectionApproval.STATUS_APPROVED
            approval.decided_at = now
            approval.save(update_fields=["status", "decided_at"])

        locked.save(update_fields=update_fields)
        audit.record(
            action="election_transition",
            actor=actor,
            election=locked,
            organization=locked.organization,
            request=request,
            reason=reason,
            details={"from": previous, "to": dst, "config_hash": locked.config_hash},
        )
    election.state = locked.state
    election.state_updated_at = locked.state_updated_at
    election.refresh_from_db()
    return election


def sync_election_state(election: Election) -> Election:
    """Apply schedule-driven transitions; safe to call on every request."""
    election = Election.objects.get(pk=election.pk)
    now = timezone.now()
    if election.state == Election.STATE_SCHEDULED and now >= election.start_at:
        transition(
            election,
            Election.STATE_OPEN,
            actor=None,
            reason="Scheduled start time reached (automatic).",
            allow_system=True,
        )
    elif election.state == Election.STATE_OPEN and now > election.end_at:
        transition(
            election,
            Election.STATE_CLOSED,
            actor=None,
            reason="Scheduled end time reached (automatic).",
            allow_system=True,
        )
    elif election.state == Election.STATE_OPEN and not election.config_frozen_at:
        freeze_config(election)
    return election


# ---------------------------------------------------------------------------
# Approval records
# ---------------------------------------------------------------------------


def request_approval(election: Election, kind: str, *, actor, reason: str, request=None):
    from audit import services as audit

    reason = (reason or "").strip()
    if not reason:
        raise LifecycleError("A documented reason is mandatory.", code="reason_required")
    required_state = {
        "approve_config": Election.STATE_CONFIG_REVIEW,
        "reopen": Election.STATE_CLOSED,
        "decertify": None,  # certified or published
    }[kind]
    current = Election.objects.get(pk=election.pk).state
    if kind == "decertify":
        if current not in (Election.STATE_CERTIFIED, Election.STATE_PUBLISHED):
            raise LifecycleError(
                "Corrections are only requested after certification.",
                code="illegal_transition",
            )
    elif current != required_state:
        raise LifecycleError(
            f"A '{kind}' request is not valid from state '{current}'.",
            code="illegal_transition",
        )
    approval = ElectionApproval.objects.create(
        election=election, kind=kind, requested_by=actor, reason=reason
    )
    audit.record(
        action="approval_requested",
        actor=actor,
        election=election,
        request=request,
        object_type="approval",
        object_id=str(approval.pk),
        reason=reason,
        details={"kind": kind},
    )
    return approval


def decide_approval(
    approval_id, *, actor, approve: bool, note: str = "", request=None
) -> ElectionApproval:
    """Approve or reject a pending record; approval performs the transition."""
    from audit import services as audit
    from organizations import roles

    with transaction.atomic():
        approval = ElectionApproval.objects.select_for_update().get(pk=approval_id)
        if approval.status != ElectionApproval.STATUS_PENDING:
            raise LifecycleError("This request has already been decided.", code="already_decided")
        # NOTE: the election row is deliberately *not* locked here - the
        # subsequent transition() locks ballot box -> election in the same
        # order as ballot acceptance (avoids a lock-order inversion).
        election = Election.objects.get(pk=approval.election_id)
        if not roles.can_approve_election(actor, election):
            raise LifecycleError(
                "Only an organization administrator may decide this request.",
                code="forbidden",
            )
        if approval.requested_by_id and approval.requested_by_id == actor.pk:
            raise LifecycleError(
                "Four-eyes rule: you cannot decide a request you submitted.",
                code="four_eyes",
            )
        if not approve:
            approval.status = ElectionApproval.STATUS_REJECTED
            approval.decided_by = actor
            approval.decided_at = timezone.now()
            approval.decision_note = note or "Rejected"
            approval.save(
                update_fields=["status", "decided_by", "decided_at", "decision_note"]
            )
            audit.record(
                action="approval_rejected",
                actor=actor,
                election=election,
                request=request,
                object_type="approval",
                object_id=str(approval.pk),
                reason=note,
                details={"kind": approval.kind},
            )
            return approval

        approval.status = ElectionApproval.STATUS_APPROVED
        approval.decided_by = actor
        approval.decided_at = timezone.now()
        approval.decision_note = note or "Approved"
        approval.save(
            update_fields=["status", "decided_by", "decided_at", "decision_note"]
        )
        target = {
            "approve_config": Election.STATE_APPROVED,
            "reopen": Election.STATE_OPEN,
            "decertify": Election.STATE_REVIEW,
        }[approval.kind]
        transition(
            election,
            target,
            actor=actor,
            approval=approval,
            request=request,
            reason=approval.reason,
        )
        approval.refresh_from_db()
        return approval
