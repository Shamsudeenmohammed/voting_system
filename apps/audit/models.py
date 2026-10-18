"""Append-only, hash-chained audit log.

Properties:
* rows are never updated or deleted (model save() blocks it; see below);
* each event stores the hash of the previous event (per-organization chain)
  plus an HMAC signature under ``AUDIT_SIGNING_KEY``;
* payload NEVER contains ballot selections or voter credentials — the
  ballots app only logs outcome codes (accepted/denied).

Limitations (documented in docs/audit-verification.md): an attacker with
both DB write access AND the signing key can rewrite history; the mitigation
is exporting checkpoints (``manage.py audit_checkpoint``) to an external
medium, which is described but requires operator discipline.
"""
import hashlib
import json

from django.conf import settings
from django.db import models, transaction

from apps.core.models import TimeStampedModel


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class AuditEvent(TimeStampedModel):
    seq = models.BigIntegerField(db_index=True)  # per-org sequence
    organization = models.ForeignKey(
        "orgs.Organization", null=True, blank=True,
        on_delete=models.CASCADE, related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    actor_repr = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    event_hash = models.CharField(max_length=64, editable=False)
    signature = models.CharField(max_length=128, blank=True, editable=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["seq"]
        verbose_name = "audit event"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "seq"], name="uniq_audit_seq_per_org"
            ),
        ]
        indexes = [models.Index(fields=["action", "created_at"])]

    def __str__(self):
        return f"#{self.seq} {self.action}"

    def compute_hash(self) -> str:
        material = _canonical(
            {
                "seq": self.seq,
                "org": self.organization_id,
                "actor": self.actor_id,
                "action": self.action,
                "object_type": self.object_type,
                "object_id": self.object_id,
                "payload": self.payload,
                "prev": self.prev_hash,
                "ts": self.created_at.isoformat() if self.created_at else None,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if self.pk is not None and not kwargs.get("force_insert"):
            raise RuntimeError("Audit events are append-only and cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Audit events are append-only and cannot be deleted.")


class AuditCheckpoint(TimeStampedModel):
    """Externally exportable checkpoint: last seq + chain head hash + HMAC.
    Operators must copy the export off-host (see docs/audit-verification.md)."""

    organization = models.ForeignKey(
        "orgs.Organization", null=True, blank=True,
        on_delete=models.CASCADE, related_name="audit_checkpoints",
    )
    last_seq = models.BigIntegerField()
    head_hash = models.CharField(max_length=64)
    signature = models.CharField(max_length=128)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"checkpoint org={self.organization_id} seq={self.last_seq}"
