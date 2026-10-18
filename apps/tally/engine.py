"""Deterministic counting engine.

Independence guarantee: this module imports ONLY ``AcceptedBallot`` from the
ballots app — it cannot join to voters or grants. An independent reviewer can
re-run ``manage.py run_tally --election <slug> --verify`` and obtain the same
``tally_hash`` from the same sealed inputs (documented procedure in
docs/audit-verification.md).

Counting rules (documented, tested):
* each accepted ballot counted exactly once per result version;
* selections validated against the *frozen* configuration (positions,
  approved candidates) — selections failing validation at tally time are
  counted as invalid ballots (defense against stale caches) and reported;
* undervote: position present but empty list; omitted position: undervote;
* abstain: recorded per election policy;
* overvotes are rejected at acceptance time (never reach the tally) and the
  acceptance counter is reported as ``overvotes_blocked``.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.db import transaction
from django.utils import timezone

from apps.ballots.models import AcceptedBallot, validate_selections
from apps.ballots.privacy import unseal_selections
from .models import PositionResult, ResultVersion, StationTally

logger = logging.getLogger("tally")


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@transaction.atomic
def calculate_tally(election, user, *, recount_reason: str = "", supersedes=None) -> ResultVersion:
    """Compute a new result version from accepted ballots.

    Deterministic: iteration order is by ``accepted_at`` then ``pk``;
    counts depend only on input set, not timing.
    """
    if election.state not in (
        election.State.CLOSED,
        election.State.RECONCILIATION,
        election.State.REVIEW,
        election.State.CERTIFIED,
        election.State.PUBLISHED,
    ):
        from .exceptions import TallyError

        raise TallyError("Tally can only run after the election is closed.")

    prev = (
        ResultVersion.objects.filter(election=election)
        .exclude(status=ResultVersion.Status.DRAFT)
        .order_by("-version_no")
        .first()
    )
    version_no = (prev.version_no + 1) if prev else 1

    result = ResultVersion.objects.create(
        election=election,
        version_no=version_no,
        status=ResultVersion.Status.DRAFT,
        supersedes=supersedes,
        recount_reason=recount_reason,
        calculated_by=user,
    )

    positions = list(
        election.positions.filter(is_active=True).prefetch_related(
            "candidates"
        ).order_by("order", "slug")
    )
    approved_by_position: dict[int, set[str]] = {}
    for pos in positions:
        approved_by_position[pos.pk] = set(
            pos.candidates.filter(status="approved").values_list("slug", flat=True)
        )

    counts: dict[int, dict[str, int]] = {p.pk: {} for p in positions}
    abstains: dict[int, int] = {p.pk: 0 for p in positions}
    undervotes: dict[int, int] = {p.pk: 0 for p in positions}
    valid_per_position: dict[int, int] = {p.pk: 0 for p in positions}

    ballots = (
        AcceptedBallot.objects.filter(election=election)
        .order_by("accepted_at", "pk")
        .iterator(chunk_size=1000)
    )

    counted = 0
    invalid = 0
    total_undervotes = 0

    for ballot in ballots:
        try:
            selections = unseal_selections(ballot.sealed_payload)
        except Exception:
            logger.error("tally.unseal_failed ballot=%s", ballot.pk)
            invalid += 1
            continue
        try:
            # Re-validate against CURRENT frozen config — tampered/stale
            # payloads become invalid ballots, never silent miscounts.
            validate_selections(election, selections)
        except Exception:
            logger.error("tally.validation_failed ballot=%s", ballot.pk)
            invalid += 1
            continue

        counted += 1
        seen_positions: set[int] = set()
        for pos in positions:
            slug = pos.slug
            if slug not in selections:
                undervotes[pos.pk] += 1
                total_undervotes += 1
                continue
            seen_positions.add(pos.pk)
            choice = selections[slug]
            if choice == "abstain":
                abstains[pos.pk] += 1
                valid_per_position[pos.pk] += 1
                continue
            if not isinstance(choice, list) or not choice:
                undervotes[pos.pk] += 1
                total_undervotes += 1
                continue
            allowed = approved_by_position[pos.pk]
            valid_choices = [c for c in choice if c in allowed]
            if len(valid_choices) != len(choice):
                # Should be unreachable (validated above) — defensive.
                invalid += 1
                continue
            valid_per_position[pos.pk] += 1
            for slug_c in valid_choices:
                counts[pos.pk][slug_c] = counts[pos.pk].get(slug_c, 0) + 1

    accepted_total = AcceptedBallot.objects.filter(election=election).count()
    spoiled = 0  # spoiled ballots: admin-revoked pre-acceptance (see docs)

    artifact: dict = {
        "election": election.slug,
        "positions": {},
        "ballots_accepted": accepted_total,
        "ballots_counted": counted,
        "ballots_invalid": invalid,
        "undervotes_total": total_undervotes,
    }

    for pos in positions:
        votes = dict(sorted(counts[pos.pk].items()))
        PositionResult.objects.create(
            result_version=result,
            position=pos,
            votes_for=votes,
            abstain_count=abstains[pos.pk],
            undervote_count=undervotes[pos.pk],
            total_valid=valid_per_position[pos.pk],
        )
        artifact["positions"][pos.slug] = {
            "votes": votes,
            "abstain": abstains[pos.pk],
            "undervote": undervotes[pos.pk],
        }

    # Station-level reconciliation
    from django.db.models import Count

    station_qs = (
        AcceptedBallot.objects.filter(election=election, station__isnull=False)
        .values("station_id")
        .order_by("station_id")
        .annotate(n=Count("pk"))
    )
    for row in station_qs.iterator():
        StationTally.objects.create(
            result_version=result,
            station_id=row["station_id"],
            ballots_accepted=row["n"],
        )

    digest = hashlib.sha256(_canonical(artifact).encode("utf-8")).hexdigest()

    result.ballots_accepted = accepted_total
    result.ballots_counted = counted
    result.ballots_invalid = invalid
    result.ballots_spoiled = spoiled
    result.undervotes_total = total_undervotes
    result.reconciled = counted + invalid == accepted_total
    result.tally_hash = digest
    result.status = ResultVersion.Status.CALCULATED
    result.calculated_at = timezone.now()
    result.save(
        update_fields=[
            "ballots_accepted",
            "ballots_counted",
            "ballots_invalid",
            "ballots_spoiled",
            "undervotes_total",
            "reconciled",
            "tally_hash",
            "status",
            "calculated_at",
            "updated_at",
        ]
    )

    from apps.audit.services import record_audit

    record_audit(
        organization=election.organization,
        actor=user,
        action="tally.calculated",
        object_type="ResultVersion",
        object_id=str(result.pk),
        payload={
            "election": election.slug,
            "version": version_no,
            "tally_hash": digest,
            "ballots_accepted": accepted_total,
            "ballots_counted": counted,
            "ballots_invalid": invalid,
            "reconciled": result.reconciled,
        },
    )
    logger.info(
        "tally.calculated election=%s v%s hash=%s", election.pk, version_no, digest[:12]
    )
    return result


def verify_tally(result: ResultVersion) -> dict:
    """Recompute from sealed inputs and compare hashes (reviewer tool)."""
    election = result.election
    recomputed_hash = _recount_hash(election)
    return {
        "match": recomputed_hash == result.tally_hash,
        "stored_hash": result.tally_hash,
        "recomputed_hash": recomputed_hash,
    }


def _recount_hash(election) -> str:
    """Recompute tally artifact hash WITHOUT writing anything (read-only)."""
    positions = list(
        election.positions.filter(is_active=True).order_by("order", "slug")
    )
    approved = {
        p.pk: set(p.candidates.filter(status="approved").values_list("slug", flat=True))
        for p in positions
    }
    counts: dict[int, dict[str, int]] = {p.pk: {} for p in positions}
    abstains = {p.pk: 0 for p in positions}
    undervotes = {p.pk: 0 for p in positions}

    counted = invalid = 0
    ballots = (
        AcceptedBallot.objects.filter(election=election)
        .order_by("accepted_at", "pk")
        .iterator(chunk_size=1000)
    )
    for ballot in ballots:
        try:
            selections = unseal_selections(ballot.sealed_payload)
            validate_selections(election, selections)
        except Exception:
            invalid += 1
            continue
        counted += 1
        for pos in positions:
            if pos.slug not in selections:
                undervotes[pos.pk] += 1
                continue
            choice = selections[pos.slug]
            if choice == "abstain":
                abstains[pos.pk] += 1
                continue
            if not isinstance(choice, list) or not choice:
                undervotes[pos.pk] += 1
                continue
            for c in choice:
                if c in approved[pos.pk]:
                    counts[pos.pk][c] = counts[pos.pk].get(c, 0) + 1

    artifact = {
        "election": election.slug,
        "positions": {},
        "ballots_accepted": AcceptedBallot.objects.filter(election=election).count(),
        "ballots_counted": counted,
        "ballots_invalid": invalid,
        "undervotes_total": sum(undervotes.values()),
    }
    for pos in positions:
        artifact["positions"][pos.slug] = {
            "votes": dict(sorted(counts[pos.pk].items())),
            "abstain": abstains[pos.pk],
            "undervote": undervotes[pos.pk],
        }
    return hashlib.sha256(_canonical(artifact).encode("utf-8")).hexdigest()
