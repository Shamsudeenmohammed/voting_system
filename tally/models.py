"""Versioned, signed tally snapshots.

A tally snapshot is the only artefact ever published as "the result".  Each
snapshot is a deterministic function of the sealed ballots plus the frozen
configuration, is hash-linked to the previous version and is signed, so an
independent reviewer can re-run the count and detect tampering.
"""

import uuid

from django.conf import settings
from django.db import models


class TallySnapshot(models.Model):
    STATUS_INTERIM = "interim"
    STATUS_CERTIFIED = "certified"
    STATUS_SUPERSEDED = "superseded"

    STATUS_CHOICES = [
        (STATUS_INTERIM, "Interim (uncertified)"),
        (STATUS_CERTIFIED, "Certified"),
        (STATUS_SUPERSEDED, "Superseded by recount/correction"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="tally_snapshots", on_delete=models.CASCADE
    )
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_INTERIM)
    payload = models.JSONField()  # deterministic per-position results structure
    config_hash = models.CharField(max_length=64, default="")
    ballots_accepted = models.PositiveIntegerField(default=0)
    ballots_counted = models.PositiveIntegerField(default=0)
    ballots_invalid = models.PositiveIntegerField(default=0)
    reconciliation_ok = models.BooleanField(default=False)
    computed_at = models.DateTimeField()
    computed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    reason = models.TextField(blank=True, default="")
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, related_name="successors", on_delete=models.SET_NULL
    )
    prev_snapshot_hash = models.CharField(max_length=64, default="0" * 64)
    snapshot_hash = models.CharField(max_length=64, blank=True, default="")
    signature = models.TextField(blank=True, default="")
    signature_algorithm = models.CharField(max_length=16, blank=True, default="")
    certified_at = models.DateTimeField(null=True, blank=True)
    certified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["election", "version"], name="uniq_tally_version"),
        ]
        indexes = [models.Index(fields=["election", "status"])]

    @property
    def is_certified(self) -> bool:
        return self.status == self.STATUS_CERTIFIED

    def __str__(self) -> str:
        return f"{self.election} tally v{self.version} ({self.status})"
