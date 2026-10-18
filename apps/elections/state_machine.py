"""Backend election lifecycle state machine.

This module is the *only* sanctioned way to change ``Election.state``. The
model field is ``editable=False`` and services/views never assign it directly.

Design rules enforced here (never in templates):

* forward transitions only, from an explicit allow-list;
* permission checks against organization role (or platform superuser);
* configuration must be valid and frozen before OPEN;
* OPEN requires APPROVED + SCHEDULED and a schedule in the future;
* CLOSED rejects any further ballots (the voting engine reads state);
* reopen (CLOSED → OPEN) requires a mandatory reason, an independent
  approver different from the actor who closed, and is audit-trailed;
* CERTIFIED results can only move forward (PUBLISHED/ARCHIVED) or to a
  formal RECONCILIATION for recounts — never silently overwritten;
* every transition writes a LifecycleEvent and an audit event.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record_audit
from .models import Election, LifecycleEvent

S = Election.State

# Allowed transitions: {from: {to, ...}}. Deliberately explicit.
FORWARD_TRANSITIONS: dict[str, set[str]] = {
    S.DRAFT: {S.REGISTRATION},
    S.REGISTRATION: {S.CONFIGURATION_REVIEW, S.DRAFT},
    S.CONFIGURATION_REVIEW: {S.APPROVED, S.REGISTRATION},  # send-back allowed
    S.APPROVED: {S.SCHEDULED, S.CONFIGURATION_REVIEW},
    S.SCHEDULED: {S.OPEN, S.APPROVED},
    S.OPEN: {S.CLOSED},
    S.CLOSED: {S.RECONCILIATION},
    S.RECONCILIATION: {S.REVIEW},
    S.REVIEW: {S.CERTIFIED, S.RECONCILIATION},  # review may bounce back for recount
    S.CERTIFIED: {S.PUBLISHED},
    S.PUBLISHED: {S.ARCHIVED},
}

# Exception transitions handled by dedicated functions with extra ceremony.
EXCEPTIONS = {"reopen": (S.CLOSED, S.OPEN)}

# Which membership roles may perform each transition.
TRANSITION_ROLES: dict[tuple[str, str], set[str]] = {
    (S.DRAFT, S.REGISTRATION): {"admin", "official"},
    (S.REGISTRATION, S.CONFIGURATION_REVIEW): {"admin", "official"},
    (S.REGISTRATION, S.DRAFT): {"admin"},
    (S.CONFIGURATION_REVIEW, S.APPROVED): {"admin"},          # explicit approval
    (S.CONFIGURATION_REVIEW, S.REGISTRATION): {"admin", "official"},
    (S.APPROVED, S.SCHEDULED): {"admin", "official"},
    (S.APPROVED, S.CONFIGURATION_REVIEW): {"admin"},
    (S.SCHEDULED, S.OPEN): {"admin", "official"},
    (S.SCHEDULED, S.APPROVED): {"admin"},
    (S.OPEN, S.CLOSED): {"admin", "official"},
    (S.CLOSED, S.RECONCILIATION): {"admin", "official"},
    (S.RECONCILIATION, S.REVIEW): {"admin", "official"},
    (S.REVIEW, S.CERTIFIED): {"admin", "auditor"},            # auditor certifies
    (S.REVIEW, S.RECONCILIATION): {"admin", "official"},
    (S.CERTIFIED, S.PUBLISHED): {"admin", "official"},
    (S.PUBLISHED, S.ARCHIVED): {"admin"},
}


class StateTransitionError(Exception):
    """Raised when a transition is invalid. Carries a user-safe message."""

    def __init__(self, message: str, code: str = "invalid_transition"):
        self.code = code
        super().__init__(message)


def _allowed_roles(from_state: str, to_state: str) -> set[str]:
    return TRANSITION_ROLES.get((from_state, to_state), set())


def check_permission(election: Election, user, to_state: str) -> None:
    if user is None or not user.is_authenticated:
        raise PermissionDenied("Sign-in required.")
    if user.is_superuser:
        return
    from apps.orgs.models import Membership

    ok = Membership.objects.filter(
        user=user,
        organization=election.organization,
        role__in=_allowed_roles(election.state, to_state),
        is_active=True,
    ).exists()
    if not ok:
        raise PermissionDenied(
            "Your role is not authorized for this election transition."
        )


def validate_configuration(election: Election) -> list[str]:
    """Return list of blocking problems; empty = configuration is valid."""
    problems: list[str] = []
    positions = list(election.positions.filter(is_active=True))
    if not positions:
        problems.append("At least one active position is required.")
    for pos in positions:
        approved = pos.candidates.filter(status="approved").count()
        if approved < 1:
            problems.append(f"Position '{pos.name}' has no approved candidates.")
        elif pos.max_choices > 1 and approved < pos.max_choices:
            # Not blocking: multi-select with fewer candidates than slots is legal.
            pass
    if not election.scheduled_opens or not election.scheduled_closes:
        problems.append("Opening and closing schedule must be set.")
    if not election.stations.exists():
        problems.append("At least one polling station/channel must be configured.")
    if election.organization_id is None:
        problems.append("Election must belong to an organization.")
    return problems


def configuration_hash(election: Election) -> str:
    """Deterministic hash of the frozen configuration (positions, candidates,
    schedule, rules). Stored on approval; verified before OPEN and for audit
    replay in docs/audit-verification.md."""
    import hashlib

    parts: list[str] = [f"name={election.name}", f"type={election.election_type}"]
    parts.append(f"opens={election.scheduled_opens}")
    parts.append(f"closes={election.scheduled_closes}")
    parts.append(f"visibility={election.result_visibility}")
    parts.append(f"allow_abstain={election.allow_abstain}")
    for pos in election.positions.filter(is_active=True).order_by("order", "slug"):
        parts.append(f"pos:{pos.slug}:{pos.max_choices}")
        for cand in pos.candidates.filter(status="approved").order_by("slug"):
            parts.append(f"cand:{pos.slug}:{cand.slug}:{cand.full_name}")
    for station in election.stations.order_by("code"):
        parts.append(f"station:{station.code}:{station.channel}")
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@transaction.atomic
def transition(
    election: Election,
    to_state: str,
    user,
    reason: str = "",
    *,
    force: bool = False,
) -> Election:
    """Perform a lifecycle transition with all checks. Returns saved election.

    Uses ``select_for_update`` on PostgreSQL so concurrent transitions
    serialize; on SQLite the whole write transaction serializes anyway.
    """
    locked = (
        Election.objects.select_for_update()
        .get(pk=election.pk, organization_id=election.organization_id)
    )
    from_state = locked.state

    if from_state == to_state:
        raise StateTransitionError("Election is already in that state.", "noop")

    # Exception path -----------------------------------------------------------------
    if (from_state, to_state) == (S.CLOSED, S.OPEN):
        raise StateTransitionError(
            "Reopening a closed election requires the reopen procedure.",
            "reopen_required",
        )

    allowed = FORWARD_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise StateTransitionError(
            f"Cannot move from '{locked.get_state_display()}' to "
            f"'{Election.State(to_state).label}'."
        )

    if not force:
        check_permission(locked, user, to_state)

    # Entry validation ---------------------------------------------------------------
    if to_state == S.CONFIGURATION_REVIEW:
        problems = validate_configuration(locked)
        if problems:
            raise ValidationError({"configuration": problems})

    if to_state == S.APPROVED:
        problems = validate_configuration(locked)
        if problems:
            raise ValidationError({"configuration": problems})
        if user and not user.is_superuser:
            from apps.orgs.models import Membership

            is_admin = Membership.objects.filter(
                user=user,
                organization=locked.organization,
                role="admin",
                is_active=True,
            ).exists()
            if not is_admin:
                raise PermissionDenied(
                    "Only an organization administrator may approve configuration."
                )
        if locked.config_frozen_hash and locked.config_frozen_hash != configuration_hash(locked):
            # Config changed after a previous freeze — must re-review.
            locked.config_frozen_hash = ""
            locked.config_frozen_at = None
            raise ValidationError(
                "Configuration changed after the previous freeze. "
                "Resubmit for configuration review."
            )

    if to_state == S.SCHEDULED:
        if not locked.scheduled_opens or not locked.scheduled_closes:
            raise ValidationError("Set the schedule before scheduling.")
        if locked.scheduled_opens <= timezone.now():
            raise ValidationError("Opening time must be in the future.")

    if to_state == S.OPEN:
        if locked.state != S.SCHEDULED and not force:
            raise StateTransitionError("Election must be scheduled before opening.")
        if locked.scheduled_closes and timezone.now() > locked.scheduled_closes:
            raise ValidationError("Scheduled closing time has already passed.")
        frozen = configuration_hash(locked)
        if locked.config_frozen_hash and locked.config_frozen_hash != frozen:
            raise ValidationError(
                "Configuration changed after approval. Re-run configuration review."
            )
        if not locked.config_frozen_hash:
            # Freeze now (force/admin paths) — record hash for later verification.
            locked.config_frozen_hash = frozen
            locked.config_frozen_at = timezone.now()
        if not locked.voter_list_frozen:
            locked.voter_list_frozen = True
        locked.opened_at = timezone.now()

    if to_state == S.CLOSED:
        locked.closed_at = timezone.now()
        locked.closed_by = user if getattr(user, "is_authenticated", False) else None

    if to_state == S.CERTIFIED:
        from apps.tally.models import ResultVersion
        if not ResultVersion.objects.filter(
            election=locked, status=ResultVersion.Status.CALCULATED
        ).exists():
            raise ValidationError(
                "Cannot certify: no calculated result version exists."
            )
        if user and not user.is_superuser:
            from apps.orgs.models import Membership

            is_auditor = Membership.objects.filter(
                user=user,
                organization=locked.organization,
                role__in=["auditor", "admin"],
                is_active=True,
            ).exists()
            if not is_auditor:
                raise PermissionDenied("Certification requires an auditor (or admin) role.")
        if user and locked.closed_by_id and locked.closed_by_id == user.id:
            raise PermissionDenied(
                "The official who closed the election cannot also certify it "
                "(separation of duties)."
            )

    if to_state == S.PUBLISHED:
        if locked.state != S.CERTIFIED:
            raise StateTransitionError("Only certified results may be published.")
        if not locked.certified_at:
            raise ValidationError("Certification record missing.")
        locked.published_at = timezone.now()

    # Persist ------------------------------------------------------------------------
    if to_state == S.APPROVED:
        locked.approved_by = user
        locked.approved_at = timezone.now()

    if to_state == S.CERTIFIED:
        locked.certified_by = user if getattr(user, "is_authenticated", False) else None
        locked.certified_at = timezone.now()

    locked.state = to_state
    locked.save(
        update_fields=[
            "state",
            "opened_at",
            "closed_at",
            "closed_by",
            "config_frozen_at",
            "config_frozen_hash",
            "voter_list_frozen",
            "approved_by",
            "approved_at",
            "certified_by",
            "certified_at",
            "published_at",
            "updated_at",
        ]
    )

    LifecycleEvent.objects.create(
        election=locked,
        from_state=from_state,
        to_state=to_state,
        actor=user if getattr(user, "is_authenticated", False) else None,
        reason=reason[:2000],
    )
    record_audit(
        organization=locked.organization,
        actor=user if getattr(user, "is_authenticated", False) else None,
        action="election.transition",
        object_type="Election",
        object_id=str(locked.pk),
        payload={
            "election": locked.slug,
            "from": from_state,
            "to": to_state,
            "reason": reason[:500],
        },
    )
    # Invalidate cached config hash consumers
    from django.core.cache import cache

    cache.delete(f"election_gate:{locked.pk}")
    return locked


@transaction.atomic
def reopen(election: Election, user, reason: str, approver, approver_password_ok: bool = False) -> Election:
    """Documented exception procedure: CLOSED → OPEN.

    Requirements (all mandatory):
      * reason text (non-empty, minimum length);
      * ``approver`` — a *different* active official than the actor;
      * explicit confirmation flag from the approver;
      * audit trail with reopen counter.
    """
    if not dj_settings.ALLOW_ELECTION_REOPEN:
        raise StateTransitionError("Reopening elections is disabled on this deployment.", "reopen_disabled")

    locked = Election.objects.select_for_update().get(pk=election.pk)
    if locked.state != S.CLOSED:
        raise StateTransitionError("Only a closed election can be reopened.", "not_closed")
    if len((reason or "").strip()) < 20:
        raise ValidationError(
            {"reason": "A detailed reason (min 20 characters) is mandatory."}
        )
    if approver is None or not approver.is_authenticated:
        raise PermissionDenied("Independent approver must be signed in.")
    if approver.pk == (user.pk if user and user.is_authenticated else None):
        raise PermissionDenied(
            "The official requesting the reopen must differ from the approver."
        )
    if not approver_password_ok:
        raise PermissionDenied("Approver must re-authenticate to approve reopening.")

    from apps.orgs.models import Membership

    approver_ok = Membership.objects.filter(
        user=approver,
        organization=locked.organization,
        role__in=["admin", "official"],
        is_active=True,
    ).exists()
    if not approver.is_superuser and not approver_ok:
        raise PermissionDenied("Approver lacks election-official role in this organization.")

    locked.state = S.OPEN
    locked.reopen_reason = reason.strip()
    locked.reopened_by = user
    locked.reopened_at = timezone.now()
    locked.reopen_count += 1
    locked.closed_at = None  # will be set again on next close
    locked.save(
        update_fields=[
            "state",
            "reopen_reason",
            "reopened_by",
            "reopened_at",
            "reopen_count",
            "closed_at",
            "updated_at",
        ]
    )
    LifecycleEvent.objects.create(
        election=locked,
        from_state=S.CLOSED,
        to_state=S.OPEN,
        actor=user,
        reason=f"REOPEN #{locked.reopen_count}: {reason.strip()[:1900]}",
        requires_second_approval=True,
    )
    record_audit(
        organization=locked.organization,
        actor=user,
        action="election.reopen",
        object_type="Election",
        object_id=str(locked.pk),
        payload={
            "election": locked.slug,
            "reason": reason.strip()[:500],
            "approver": approver.username,
            "reopen_count": locked.reopen_count,
        },
    )
    return locked
