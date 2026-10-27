"""Audit append / verification services."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import keys
from .models import AuditChain, AuditCheckpoint, AuditEvent

# Defensive: these keys indicate secret ballot content or credentials must
# never be persisted in an identity-linked audit record.
FORBIDDEN_DETAIL_KEYS = {
    "selections",
    "choices",
    "ballot",
    "ballot_choices",
    "payload",
    "token",
    "password",
    "secret",
    "receipt_code",
    "submission_id",
}

GENESIS = "0" * 64


def _pepper() -> str:
    from django.conf import settings

    return settings.VOTER_TOKEN_PEPPER or settings.SECRET_KEY


def hash_ip(ip: str | None) -> str:
    if not ip:
        return ""
    return hmac.new(_pepper().encode(), ip.encode(), hashlib.sha256).hexdigest()[:24]


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _event_hash(*, chain_key: str, seq: int, prev_hash: str, core: dict) -> str:
    material = canonical_json(
        {"chain": chain_key, "seq": seq, "prev": prev_hash, **core}
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _check_details(details: dict) -> None:
    lowered = {str(k).lower() for k in details}
    bad = lowered & FORBIDDEN_DETAIL_KEYS
    if bad:
        raise ValueError(
            f"Audit details must not contain sensitive ballot fields: {sorted(bad)}"
        )


def record(
    *,
    action: str,
    actor=None,
    election=None,
    organization=None,
    object_type: str = "",
    object_id: str = "",
    details: dict | None = None,
    reason: str = "",
    request=None,
    chain_key: str | None = None,
    ip_hash: str | None = None,
    using: str | None = None,
) -> AuditEvent:
    """Append one event to the appropriate hash chain and return it.

    The caller is responsible for being inside the business transaction that
    the event describes: if that transaction rolls back the audit event rolls
    back with it (no "phantom" audit trail).
    """
    details = dict(details or {})
    _check_details(details)

    if chain_key is None:
        if election is not None:
            chain_key = f"election:{election.pk}"
        elif organization is not None:
            chain_key = f"organization:{organization.pk}"
        else:
            chain_key = "global"

    if request is not None and ip_hash is None:
        from elections.utils import request_ip

        ip_hash = hash_ip(request_ip(request))

    actor_label = ""
    if actor is not None and actor.is_authenticated:
        actor_label = actor.email or getattr(actor, "username", "") or str(actor.pk)

    # Normalize: the stored column is non-null ("" for unknown), so the hashed
    # material must use the same value or verification would never match.
    ip_hash = ip_hash or ""

    core = {
        "action": action,
        "actor": str(actor.pk) if actor is not None and actor.is_authenticated else "",
        "actor_label": actor_label,
        "organization": str(organization.pk) if organization else "",
        "election": str(election.pk) if election else "",
        "object_type": object_type,
        "object_id": str(object_id),
        "reason": reason,
        "details": details,
        "ip_hash": ip_hash,
    }

    db = using or "default"
    for attempt in (1, 2):
        try:
            with transaction.atomic(using=db):
                chain, _created = AuditChain.objects.using(db).get_or_create(
                    chain_key=chain_key
                )
                chain = (
                    AuditChain.objects.using(db)
                    .select_for_update()
                    .get(pk=chain.pk)
                )
                seq = chain.last_seq + 1
                created_at = timezone.now()
                prev_hash = chain.head_hash
                event_hash = _event_hash(
                    chain_key=chain_key, seq=seq, prev_hash=prev_hash,
                    core={**core, "created_at": created_at.isoformat()},
                )
                actor_obj = actor if getattr(actor, "is_authenticated", False) else None
                event = AuditEvent.objects.using(db).create(
                    chain_key=chain_key,
                    seq=seq,
                    created_at=created_at,
                    prev_hash=prev_hash,
                    event_hash=event_hash,
                    action=action,
                    actor=actor_obj,
                    actor_label=actor_label,
                    organization=organization,
                    election=election,
                    object_type=object_type,
                    object_id=str(object_id),
                    reason=reason,
                    details=details,
                    ip_hash=ip_hash or "",
                )
                chain.last_seq = seq
                chain.head_hash = event_hash
                chain.save(using=db, update_fields=["last_seq", "head_hash", "updated_at"])
                return event
        except IntegrityError:
            if attempt == 1:
                # Another worker created the chain row concurrently; retry once.
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def chain_for(key: str, using: str | None = None) -> AuditChain | None:
    return AuditChain.objects.using(using or "default").filter(chain_key=key).first()


def verify_chain(key: str, using: str | None = None) -> dict:
    """Recompute every hash in a chain; report the first inconsistency."""
    db = using or "default"
    head = AuditChain.objects.using(db).filter(chain_key=key).first()
    expected_prev = GENESIS
    expected_seq = 1
    count = 0
    error: str | None = None
    first_bad = None
    events = AuditEvent.objects.using(db).filter(chain_key=key).order_by("seq")
    for ev in events.iterator():
        if ev.seq != expected_seq:
            error = f"sequence gap at seq {ev.seq} (expected {expected_seq})"
            first_bad = ev.seq
            break
        if ev.prev_hash != expected_prev:
            error = f"broken link at seq {ev.seq} (prev_hash mismatch)"
            first_bad = ev.seq
            break
        election_id_str = ""
        if ev.election_id:
            election_id_str = str(ev.election_id)
        else:
            # Recover election id from chain_key when FK was nulled (e.g. after election deletion)
            if ev.chain_key and ev.chain_key.startswith("election:"):
                election_id_str = ev.chain_key.split("election:", 1)[1] or ""
        recomputed = _event_hash(
            chain_key=ev.chain_key,
            seq=ev.seq,
            prev_hash=ev.prev_hash,
            core={
                "action": ev.action,
                "actor": str(ev.actor_id) if ev.actor_id else "",
                "actor_label": ev.actor_label,
                "organization": str(ev.organization_id) if ev.organization_id else "",
                "election": election_id_str,
                "object_type": ev.object_type,
                "object_id": ev.object_id,
                "reason": ev.reason,
                "details": ev.details,
                "ip_hash": ev.ip_hash,
                "created_at": ev.created_at.isoformat(),
            },
        )
        if recomputed != ev.event_hash:
            error = f"tampered event at seq {ev.seq}"
            first_bad = ev.seq
            break
        expected_prev = ev.event_hash
        expected_seq += 1
        count += 1
    else:
        if head and head.last_seq != count:
            error = (
                f"chain head claims seq {head.last_seq} but {count} events exist"
            )
            first_bad = count + 1
        elif head and head.head_hash != expected_prev:
            error = "chain head hash does not match last event"
            first_bad = count
    return {
        "chain_key": key,
        "ok": error is None,
        "events": count,
        "head_seq": head.last_seq if head else 0,
        "head_hash": head.head_hash if head else GENESIS,
        "error": error,
        "first_bad_seq": first_bad,
    }


def verify_all_chains(using: str | None = None) -> list[dict]:
    db = using or "default"
    return [verify_chain(k, using=db) for k in AuditChain.objects.using(db).values_list("chain_key", flat=True)]


def create_checkpoint(key: str, actor=None, using: str | None = None) -> AuditCheckpoint:
    db = using or "default"
    with transaction.atomic(using=db):
        head = AuditChain.objects.using(db).select_for_update().get(chain_key=key)
        cp = AuditCheckpoint(
            chain_key=key,
            seq=head.last_seq,
            head_hash=head.head_hash,
            algorithm=keys.algorithm(),
            created_by=actor if getattr(actor, "is_authenticated", False) else None,
        )
        cp.signature = keys.sign(cp.signed_payload())
        cp.public_key = keys.public_key_pem()
        cp.save(using=db)
    return cp


def verify_checkpoint(cp: AuditCheckpoint) -> bool:
    return keys.verify(cp.signed_payload(), cp.signature, cp.public_key)


def export_chain(key: str, using: str | None = None) -> dict:
    """Deterministic JSON export an independent reviewer can re-verify."""
    db = using or "default"
    events = [
        {
            "seq": ev.seq,
            "created_at": ev.created_at.isoformat(),
            "action": ev.action,
            "actor": ev.actor_label,
            "object_type": ev.object_type,
            "object_id": ev.object_id,
            "reason": ev.reason,
            "details": ev.details,
            "prev_hash": ev.prev_hash,
            "event_hash": ev.event_hash,
        }
        for ev in AuditEvent.objects.using(db).filter(chain_key=key).order_by("seq")
    ]
    head = AuditChain.objects.using(db).filter(chain_key=key).first()
    checkpoints = [
        {
            "seq": cp.seq,
            "head_hash": cp.head_hash,
            "algorithm": cp.algorithm,
            "signature": cp.signature,
            "public_key": cp.public_key,
            "created_at": cp.created_at.isoformat(),
        }
        for cp in AuditCheckpoint.objects.using(db)
        .filter(chain_key=key)
        .order_by("seq")
    ]
    return {
        "chain_key": key,
        "head": {
            "seq": head.last_seq if head else 0,
            "hash": head.head_hash if head else GENESIS,
        },
        "events": events,
        "checkpoints": checkpoints,
    }
