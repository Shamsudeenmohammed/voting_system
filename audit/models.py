"""Append-only, hash-chained audit log.

Design notes
------------
* Events are grouped into *chains* (``election:<uuid>``, ``org:<uuid>`` or
  ``global``).  Each chain is strictly ordered by ``seq`` and each event's
  ``event_hash`` covers the previous hash, making truncation or in-place
  edits detectable by :func:`audit.services.verify_chain`.
* Append goes through ``SELECT ... FOR UPDATE`` on the single
  :class:`AuditChain` head row, so concurrent appends serialise correctly on
  PostgreSQL (SQLite serialises writers globally).
* :class:`AuditCheckpoint` stores signed snapshots of a chain head.  When an
  external/ independently protected checkpoint is exported the signature can
  be verified offline against the published public key.
* Ballot *selections* must never be written into audit events; the append
  service rejects known-sensitive keys as a defensive measure.
"""

import uuid

from django.conf import settings
from django.db import models


class AuditChain(models.Model):
    """Head of a hash chain (single row per chain, updated under lock)."""

    chain_key = models.CharField(max_length=128, unique=True)
    head_hash = models.CharField(max_length=64, default="0" * 64)
    last_seq = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.chain_key} @ seq {self.last_seq}"


class AuditEvent(models.Model):
    """Append-only audit record.  Never contains ballot selections."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain_key = models.CharField(max_length=128, db_index=True)
    seq = models.PositiveBigIntegerField()
    created_at = models.DateTimeField()
    action = models.CharField(max_length=100, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    actor_label = models.CharField(max_length=255, blank=True, default="")
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    election = models.ForeignKey(
        "elections.Election",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    object_type = models.CharField(max_length=64, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    ip_hash = models.CharField(max_length=32, blank=True, default="")
    prev_hash = models.CharField(max_length=64)
    event_hash = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at", "-seq"]
        constraints = [
            models.UniqueConstraint(fields=["chain_key", "seq"], name="uniq_chain_seq"),
        ]
        indexes = [
            models.Index(fields=["election", "-seq"]),
            models.Index(fields=["chain_key", "action"]),
        ]

    def __str__(self) -> str:
        return f"[{self.chain_key}#{self.seq}] {self.action}"


class AuditCheckpoint(models.Model):
    """Signed snapshot of a chain head for independent verification."""

    ALGORITHM_CHOICES = [("ed25519", "Ed25519"), ("hmac-sha256", "HMAC-SHA256")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain_key = models.CharField(max_length=128, db_index=True)
    seq = models.PositiveBigIntegerField()
    head_hash = models.CharField(max_length=64)
    algorithm = models.CharField(max_length=16, choices=ALGORITHM_CHOICES, default="ed25519")
    signature = models.TextField()
    public_key = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_checkpoints",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["chain_key", "-created_at"])]

    def signed_payload(self) -> bytes:
        return f"audit-checkpoint|{self.chain_key}|{self.seq}|{self.head_hash}".encode()

    def __str__(self) -> str:
        return f"{self.chain_key} checkpoint @ {self.seq}"
