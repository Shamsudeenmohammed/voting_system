"""Deterministic, independently reviewable counting engine.

Properties:

* Counting is a pure function of (sealed ballots, frozen configuration).
  :func:`verify_snapshot` re-runs the count and compares byte-for-byte with
  the canonical stored payload - an independent reviewer with database
  access to the ballots (and the decryption key) can reproduce the result.
* Reconciliation: ``ballots_accepted == ballots_valid + ballots_invalid``
  and ``box.last_seq == ballots_accepted`` must both hold; otherwise the
  snapshot is marked not reconcilable and cannot be certified.
* Per-position statistics include undervotes (blank) and overvotes (more
  picks than ``max_choices`` - defensive; the acceptance layer rejects
  these).  Ballots with unknown candidates/positions or undecryptable
  payloads are *spoiled* (invalid) and excluded from all position counts.
* Snapshots are versioned and hash-linked; certification never overwrites a
  previous snapshot (it is marked ``superseded`` with an audit event).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit import keys
from ballot import crypto
from elections.config import load_config
from elections.models import Election

logger = logging.getLogger("votesys.tally")

GENESIS = "0" * 64


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _count(election: Election) -> tuple[dict, dict]:
    """Iterate every sealed ballot once and build the results payload."""
    from ballot.models import Ballot, BallotBox

    config = load_config(election)
    positions = config["positions"]

    ballots_accepted = 0
    ballots_invalid = 0
    counts: dict[str, dict[str, int]] = {
        p["id"]: {c["id"]: 0 for c in p["candidates"]} for p in positions
    }
    undervotes: dict[str, int] = {p["id"]: 0 for p in positions}
    overvotes: dict[str, int] = {p["id"]: 0 for p in positions}
    votes_counted: dict[str, int] = {p["id"]: 0 for p in positions}

    for ballot in Ballot.objects.filter(election_id=election.pk).order_by("box_seq").iterator():
        ballots_accepted += 1
        try:
            data = crypto.decrypt_payload(ballot.payload)
            selections = data.get("selections")
            if not isinstance(selections, dict):
                raise ValueError("payload is not a selections object")
        except Exception:
            ballots_invalid += 1
            logger.warning("ballot %s could not be decrypted (spoiled)", ballot.pk)
            continue

        if (
            ballot.config_hash
            and election.config_hash
            and ballot.config_hash != election.config_hash
        ):
            # Configuration drift between acceptance and counting.
            ballots_invalid += 1
            continue

        # Pass 1: validate the whole ballot before counting anything.
        submitted_keys = {str(k) for k in selections}
        if submitted_keys != set(counts):
            # Unknown or missing position block -> tamper/structure failure.
            ballots_invalid += 1
            continue
        parsed: dict[str, list[str]] = {}
        ballot_valid = True
        for pos in positions:
            pos_id = pos["id"]
            raw = selections.get(pos_id, selections.get(str(pos_id)))
            if raw is None:
                raw = []
            if isinstance(raw, str):
                raw = [raw] if raw else []
            if not isinstance(raw, (list, tuple)):
                ballot_valid = False
                break
            ids: list[str] = []
            for item in raw:
                item = str(item)
                if item and item not in ids:
                    ids.append(item)
            allowed = {c["id"] for c in pos["candidates"]}
            if any(i not in allowed for i in ids):
                ballot_valid = False
                break
            parsed[pos_id] = ids
        if not ballot_valid:
            ballots_invalid += 1  # spoiled ballot - excluded from all tallies
            continue

        # Pass 2: apply counts (valid ballot).
        for pos in positions:
            pos_id = pos["id"]
            ids = parsed[pos_id]
            if len(ids) > pos["max_choices"]:
                overvotes[pos_id] += 1
                continue
            if not ids:
                undervotes[pos_id] += 1
                continue
            for cid in ids:
                counts[pos_id][cid] += 1
            votes_counted[pos_id] += len(ids)

    box = BallotBox.objects.filter(election_id=election.pk).first()
    box_seq = box.last_seq if box else 0
    ballots_valid = ballots_accepted - ballots_invalid

    results_positions = []
    for pos in positions:
        pos_id = pos["id"]
        cand_rows = [
            {"id": c["id"], "full_name": c["full_name"], "votes": counts[pos_id][c["id"]]}
            for c in pos["candidates"]
        ]
        # Deterministic display order: most votes first, ties broken by the
        # canonical ballot order (stable across recomputations).
        order_index = {c["id"]: n for n, c in enumerate(pos["candidates"])}
        cand_rows.sort(key=lambda r: (-r["votes"], order_index[r["id"]]))
        results_positions.append(
            {
                "id": pos_id,
                "title": pos["title"],
                "max_choices": pos["max_choices"],
                "candidates": cand_rows,
                "totals": {
                    "votes_counted": votes_counted[pos_id],
                    "undervotes": undervotes[pos_id],
                    "overvotes": overvotes[pos_id],
                    "registered_choices": sum(r["votes"] for r in cand_rows),
                },
            }
        )

    reconciliation_ok = (
        ballots_accepted == ballots_valid + ballots_invalid
        and box_seq == ballots_accepted
    )
    payload = {
        "election_id": str(election.pk),
        "election_title": election.title,
        "config_hash": election.config_hash,
        "positions": results_positions,
        "totals": {
            "ballots_accepted": ballots_accepted,
            "ballots_valid": ballots_valid,
            "ballots_invalid": ballots_invalid,
            "ballots_spoiled": ballots_invalid,
            "box_last_seq": box_seq,
            "reconciliation_ok": reconciliation_ok,
        },
    }
    stats = {
        "ballots_accepted": ballots_accepted,
        "ballots_valid": ballots_valid,
        "ballots_invalid": ballots_invalid,
        "reconciliation_ok": reconciliation_ok,
    }
    return payload, stats


def _snapshot_hash(payload: dict, *, config_hash: str, prev_hash: str, version: int) -> str:
    material = (
        canonical(payload) + "|" + config_hash + "|" + prev_hash + "|" + str(version)
    )
    return hashlib.sha256(material.encode()).hexdigest()


def compute_snapshot(election_id, *, actor=None, reason: str = ""):
    """Compute and persist a new tally snapshot version (idempotent per run)."""
    from tally.models import TallySnapshot

    election = Election.objects.get(pk=election_id)
    payload, stats = _count(election)
    with transaction.atomic():
        latest = (
            TallySnapshot.objects.select_for_update()
            .filter(election_id=election.pk)
            .order_by("-version")
            .first()
        )
        if (
            latest is not None
            and latest.status == TallySnapshot.STATUS_INTERIM
            and not reason
            and canonical(latest.payload) == canonical(payload)
        ):
            # Nothing changed since the last interim count - reuse it.
            return latest
        version = (latest.version if latest else 0) + 1
        prev_hash = latest.snapshot_hash if latest else GENESIS
        snap_hash = _snapshot_hash(
            payload, config_hash=election.config_hash, prev_hash=prev_hash, version=version
        )
        signature_payload = f"tally|{election.pk}|{version}|{snap_hash}".encode()
        snapshot = TallySnapshot.objects.create(
            election=election,
            version=version,
            status=TallySnapshot.STATUS_INTERIM,
            payload=payload,
            config_hash=election.config_hash,
            ballots_accepted=stats["ballots_accepted"],
            ballots_counted=stats["ballots_valid"],
            ballots_invalid=stats["ballots_invalid"],
            reconciliation_ok=stats["reconciliation_ok"],
            computed_at=timezone.now(),
            computed_by=actor if getattr(actor, "is_authenticated", False) else None,
            reason=reason,
            supersedes=latest if (latest and reason) else None,
            prev_snapshot_hash=prev_hash,
            snapshot_hash=snap_hash,
            signature=keys.sign(signature_payload),
            signature_algorithm=keys.algorithm(),
        )
        from audit import services as audit

        audit.record(
            action="tally_computed",
            actor=actor,
            election=election,
            request=None,
            object_type="tally_snapshot",
            object_id=str(snapshot.pk),
            reason=reason,
            details={
                "version": version,
                "ballots_accepted": stats["ballots_accepted"],
                "ballots_invalid": stats["ballots_invalid"],
                "reconciliation_ok": stats["reconciliation_ok"],
                "snapshot_hash": snap_hash,
            },
        )
    return snapshot


LIVE_COUNT_TTL = 15  # seconds; new ballots change the cache key immediately


def live_count(election):
    """Aggregate live count for projection/monitoring surfaces.

    This deliberately does **not** create a tally snapshot: it is an
    uncertified, read-only view of the sealed ballots.  The result contains
    only per-candidate totals and reconciliation counters - never ballot
    payloads, receipt codes or voter identity.

    The cache key includes the ballot-box sequence, so a new accepted ballot
    invalidates the result immediately even before the short TTL expires.
    ``cache.get``/``set`` failures are ignored: a cold cache only costs a
    recomputation, it must never break the board.
    """
    from django.core.cache import cache
    from django.db.models import Max

    from ballot.models import Ballot, BallotBox

    seq = (
        BallotBox.objects.filter(election_id=election.pk)
        .values_list("last_seq", flat=True)
        .first()
        or 0
    )
    key = f"live-count:{election.pk}:{election.config_hash or 'unfrozen'}:{seq}"
    try:
        cached = cache.get(key)
    except Exception:  # pragma: no cover - cache backend outage guard
        cached = None
    if cached is not None:
        return cached

    started = time.perf_counter()
    payload, _stats = _count(election)
    last_ballot_at = (
        Ballot.objects.filter(election_id=election.pk)
        .aggregate(last=Max("accepted_at"))["last"]
    )
    payload["live"] = {
        "generated_at": timezone.now().isoformat(),
        "count_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "last_ballot_at": last_ballot_at.isoformat() if last_ballot_at else None,
    }
    try:
        cache.set(key, payload, LIVE_COUNT_TTL)
    except Exception:  # pragma: no cover - cache backend outage guard
        pass
    return payload


def latest_snapshot(election):
    from tally.models import TallySnapshot

    return (
        TallySnapshot.objects.filter(election_id=election.pk)
        .order_by("-version")
        .first()
    )


def certify_latest(election, *, actor, request=None):
    """Certify the newest interim snapshot (called from the state machine)."""
    from audit import services as audit
    from tally.models import TallySnapshot

    snapshot = latest_snapshot(election)
    if snapshot is None:
        from elections.lifecycle import LifecycleError

        raise LifecycleError(
            "No tally snapshot exists yet - run reconciliation first.",
            code="tally_missing",
        )
    if not snapshot.reconciliation_ok:
        from elections.lifecycle import LifecycleError

        raise LifecycleError(
            "Ballot reconciliation failed; certification is blocked.",
            code="reconciliation_failed",
        )
    # Never certify a stale count: re-verify the ballot box against the
    # snapshot at certification time.  The box may have moved since the
    # snapshot was computed (manual tampering, late writes).
    live_payload, live_stats = _count(election)
    if not live_stats["reconciliation_ok"]:
        from elections.lifecycle import LifecycleError

        raise LifecycleError(
            "Ballot reconciliation failed; certification is blocked.",
            code="reconciliation_failed",
        )
    if canonical(live_payload) != canonical(snapshot.payload):
        from elections.lifecycle import LifecycleError

        raise LifecycleError(
            "The latest tally no longer matches the ballot box; recompute the count.",
            code="snapshot_stale",
        )
    if snapshot.status == TallySnapshot.STATUS_SUPERSEDED:
        from elections.lifecycle import LifecycleError

        raise LifecycleError(
            "The latest snapshot was superseded; compute a fresh count first.",
            code="snapshot_superseded",
        )
    snapshot.status = TallySnapshot.STATUS_CERTIFIED
    snapshot.certified_at = timezone.now()
    snapshot.certified_by = actor if getattr(actor, "is_authenticated", False) else None
    snapshot.save(update_fields=["status", "certified_at", "certified_by"])
    audit.record(
        action="results_certified",
        actor=actor,
        election=election,
        request=request,
        object_type="tally_snapshot",
        object_id=str(snapshot.pk),
        details={"version": snapshot.version, "snapshot_hash": snapshot.snapshot_hash},
    )
    return snapshot


def supersede_certified(election, *, actor, reason: str):
    """Mark every certified snapshot as superseded (never overwrite)."""
    from audit import services as audit
    from tally.models import TallySnapshot

    changed = []
    for snapshot in TallySnapshot.objects.filter(
        election_id=election.pk, status=TallySnapshot.STATUS_CERTIFIED
    ):
        snapshot.status = TallySnapshot.STATUS_SUPERSEDED
        snapshot.save(update_fields=["status"])
        changed.append(snapshot.pk)
        audit.record(
            action="results_superseded",
            actor=actor,
            election=election,
            reason=reason,
            object_type="tally_snapshot",
            object_id=str(snapshot.pk),
            details={"version": snapshot.version, "snapshot_hash": snapshot.snapshot_hash},
        )
    return changed


def verify_snapshot(snapshot) -> dict:
    """Independently recompute the count and compare with the stored result."""
    election = snapshot.election
    payload, stats = _count(election)
    stored = canonical(snapshot.payload)
    recomputed = canonical(payload)
    expected_hash = _snapshot_hash(
        snapshot.payload,
        config_hash=snapshot.config_hash,
        prev_hash=snapshot.prev_snapshot_hash,
        version=snapshot.version,
    )
    signature_ok = keys.verify(
        f"tally|{election.pk}|{snapshot.version}|{snapshot.snapshot_hash}".encode(),
        snapshot.signature,
        "",  # verify with the local signing key (or published public key)
    )
    return {
        "payload_matches": stored == recomputed,
        "hash_matches": expected_hash == snapshot.snapshot_hash,
        "signature_valid": signature_ok,
        "stats": stats,
        "recomputed_totals": payload["totals"],
        "stored_totals": snapshot.payload.get("totals", {}),
    }
