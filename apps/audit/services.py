"""Audit recording + chain verification."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AuditCheckpoint, AuditEvent


def _sign(material: str) -> str:
    key = (settings.AUDIT_SIGNING_KEY or "unsigned-dev-key").encode("utf-8")
    return hmac.new(key, material.encode("utf-8"), hashlib.sha256).hexdigest()


def record_audit(
    *,
    action: str,
    organization=None,
    actor=None,
    object_type: str = "",
    object_id: str = "",
    payload: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    """Append an audit event. Safe to call inside or outside transactions;
    chain integrity is maintained under ``select_for_update`` of the last
    event row when the backend supports it (PostgreSQL), serialized by the
    per-org sequence unique constraint otherwise.
    """
    payload = payload or {}
    # Defensive scrub: never persist anything resembling ballot choices.
    for banned in ("selections", "choices", "ballot_choices", "password", "token"):
        if banned in payload:
            raise ValueError(f"audit payload must not contain '{banned}'")

    with transaction.atomic():
        last = (
            AuditEvent.objects.filter(organization=organization)
            .order_by("-seq")
            .first()
        )
        if last is not None:
            AuditEvent.objects.select_for_update().filter(pk=last.pk).first()
            prev_hash = last.event_hash
            seq = last.seq + 1
        else:
            prev_hash = ""
            seq = 1

        event = AuditEvent(
            seq=seq,
            organization=organization,
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            actor_repr=str(actor) if actor is not None and getattr(actor, "is_authenticated", False) else "",
            action=action,
            object_type=object_type,
            object_id=object_id,
            payload=payload,
            prev_hash=prev_hash,
            ip_address=ip_address,
        )
        event.created_at = event.created_at or timezone.now()
        event.event_hash = event.compute_hash()
        event.signature = _sign(event.event_hash + ":" + prev_hash)
        event.save(force_insert=True)
    return event


def verify_chain(organization=None) -> dict:
    """Recompute the whole chain. Returns {'ok': bool, 'broken_at': seq|None, ...}"""
    qs = AuditEvent.objects.all()
    if organization is not None:
        qs = qs.filter(organization=organization)
    qs = qs.order_by("seq")
    prev = ""
    expected_seq = 1
    checked = 0
    for ev in qs.iterator():
        if ev.seq != expected_seq or ev.prev_hash != prev:
            return {"ok": False, "broken_at": ev.seq, "reason": "sequence/hash-link mismatch", "checked": checked}
        if ev.compute_hash() != ev.event_hash:
            return {"ok": False, "broken_at": ev.seq, "reason": "content hash mismatch", "checked": checked}
        expected_material = _sign(ev.event_hash + ":" + prev)
        if not hmac.compare_digest(expected_material, ev.signature):
            return {"ok": False, "broken_at": ev.seq, "reason": "signature mismatch", "checked": checked}
        prev = ev.event_hash
        expected_seq += 1
        checked += 1
    return {"ok": True, "broken_at": None, "checked": checked, "head": prev}


def create_checkpoint(organization=None, user=None) -> AuditCheckpoint:
    last = AuditEvent.objects.filter(organization=organization).order_by("-seq").first()
    if last is None:
        raise ValueError("No audit events to checkpoint.")
    sig = _sign(f"checkpoint:{last.seq}:{last.event_hash}")
    return AuditCheckpoint.objects.create(
        organization=organization,
        last_seq=last.seq,
        head_hash=last.event_hash,
        signature=sig,
        created_by=user,
    )


def verify_checkpoint(cp: AuditCheckpoint) -> bool:
    last = AuditEvent.objects.filter(organization=cp.organization).order_by("-seq").first()
    if last is None:
        return False
    if last.seq != cp.last_seq or last.event_hash != cp.head_hash:
        # Checkpoint still valid if it anchors an *earlier* point in a valid chain
        ev = AuditEvent.objects.filter(organization=cp.organization, seq=cp.last_seq).first()
        if ev is None or ev.event_hash != cp.head_hash:
            return False
    expected = _sign(f"checkpoint:{cp.last_seq}:{cp.head_hash}")
    return hmac.compare_digest(expected, cp.signature)
