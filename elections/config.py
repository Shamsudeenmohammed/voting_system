"""Canonical election configuration: hashing and ballot validation.

The configuration hash binds the approved rules (schedule, positions,
candidates, visibility, blank-vote policy) to the frozen state of an
election.  Ballots are validated against this canonical configuration both
at acceptance time and again during tallying.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.core.cache import cache


class SelectionError(ValueError):
    """Raised when a submitted ballot does not match the configuration."""


def _canonical_positions(election) -> list[dict[str, Any]]:
    positions = []
    for pos in election.positions.prefetch_related("candidates").order_by("order", "id"):
        candidates = [
            {
                "id": str(c.id),
                "full_name": c.full_name,
                "order": c.order,
            }
            for c in pos.candidates.all()
            if c.status == "approved"
        ]
        candidates.sort(key=lambda c: (c["order"], c["id"]))
        positions.append(
            {
                "id": str(pos.id),
                "title": pos.title,
                "order": pos.order,
                "max_choices": pos.max_choices,
                "candidates": candidates,
            }
        )
    return positions


def election_config(election) -> dict[str, Any]:
    """Full canonical configuration used for hashing and validation."""
    return {
        "election_id": str(election.pk),
        "title": election.title,
        "ballot_type": election.ballot_type,
        "start_at": election.start_at.isoformat(),
        "end_at": election.end_at.isoformat(),
        "allow_blank": bool(election.allow_blank),
        "results_visibility": election.results_visibility,
        "eligibility_units": sorted(election.eligibility_units or []),
        "positions": _canonical_positions(election),
    }


def compute_config_hash(election) -> str:
    """Deterministic SHA-256 over the canonical configuration."""
    material = json.dumps(election_config(election), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def _cache_key(election) -> str:
    return f"config:{election.pk}:{election.config_hash or 'unfrozen'}"


def load_config(election) -> dict[str, Any]:
    """Configuration for validation.

    Frozen configurations are immutable, so they are cached (safe even with
    several workers).  Unfrozen configurations are read fresh from the
    database - they may still change.
    """
    if election.config_hash:
        cached = cache.get(_cache_key(election))
        if cached is not None:
            return cached
    config = election_config(election)
    if election.config_hash:
        cache.set(_cache_key(election), config, timeout=None)
    return config


def validate_selections(election, selections: Any) -> dict[str, list[str]]:
    """Validate raw ballot input against the configuration.

    Returns a canonical ``{position_id: [candidate_ids]}`` mapping (blank
    positions included as empty lists).  Raises :class:`SelectionError` for
    unknown positions/candidates or over-voting - in those cases **no**
    ballot is accepted.
    """
    config = load_config(election)
    positions = config["positions"]

    if not isinstance(selections, dict):
        raise SelectionError("Ballot payload must be an object of position selections.")

    known = {p["id"] for p in positions}
    submitted_keys = {str(k) for k in selections}
    unknown = submitted_keys - known
    if unknown:
        raise SelectionError("Ballot contains unknown positions.")

    result: dict[str, list[str]] = {}
    for pos in positions:
        pos_id = pos["id"]
        raw = selections.get(pos_id, selections.get(str(pos_id)))
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = [raw] if raw else []
        if not isinstance(raw, (list, tuple)):
            raise SelectionError(f"Invalid selection format for position {pos_id}.")
        ids: list[str] = []
        for item in raw:
            item = str(item)
            if not item:
                continue
            if item in ids:
                continue  # duplicate picks collapse; not an over-vote
            ids.append(item)

        if len(ids) > pos["max_choices"]:
            raise SelectionError(
                f"Too many choices for one of the positions "
                f"(maximum {pos['max_choices']})."
            )
        allowed = {c["id"] for c in pos["candidates"]}
        bad = [i for i in ids if i not in allowed]
        if bad:
            raise SelectionError("Ballot contains an unknown candidate for a position.")
        if not ids and not election.allow_blank:
            raise SelectionError("Blank selections are not allowed in this election.")
        # Canonical order: as listed on the ballot (order, id).
        order = {c["id"]: n for n, c in enumerate(pos["candidates"])}
        ids.sort(key=lambda i: order[i])
        result[pos_id] = ids
    return result


def invalidate_config_cache(election) -> None:
    """Drop cached config entries for an (unfrozen) election."""
    cache.delete(f"config:{election.pk}:unfrozen")
    cache.delete(f"config:{election.pk}:{election.config_hash or 'unfrozen'}")
