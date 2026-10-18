"""Secret-ballot storage: identity and selections are separated.

Records
------
:class:`BallotAuthorization`
    Single-use, peppered-hashed voting credential.  Links a *voter* to the
    right to cast one ballot.  Contains **no** reference to any ballot.

:class:`BallotPreparation`
    Short-lived capability record created when the voter opens the ballot
    form.  Links authorization <-> ``submission_id`` only while no ballot has
    been accepted; it is deleted inside the accepting transaction, so no
    durable path from voter to ballot exists.

:class:`Ballot`
    The sealed ballot: encrypted selections + random receipt.  Carries **no**
    voter and **no** authorization reference.

:class:`BallotBox`
    Per-election counter row.  Row locking on it serialises ballot
    acceptance and closing/reopening so that the close boundary is exact.
"""

import uuid

from django.conf import settings
from django.db import models


class BallotAuthorization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="ballot_authorizations", on_delete=models.CASCADE
    )
    voter = models.ForeignKey(
        "elections.Voter", related_name="ballot_authorizations", on_delete=models.PROTECT
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+", on_delete=models.SET_NULL
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at"]
        indexes = [
            models.Index(fields=["election", "consumed_at"]),
            models.Index(fields=["election", "revoked_at"]),
        ]

    @property
    def usable(self) -> bool:
        from django.utils import timezone

        now = timezone.now()
        if self.consumed_at or self.revoked_at:
            return False
        if self.expires_at and self.expires_at < now:
            return False
        return True

    def __str__(self) -> str:
        state = "consumed" if self.consumed_at else ("revoked" if self.revoked_at else "issued")
        return f"auth {self.pk} ({state}) for {self.voter_id}"


class BallotPreparation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="ballot_preparations", on_delete=models.CASCADE
    )
    authorization = models.ForeignKey(
        BallotAuthorization, related_name="preparations", on_delete=models.CASCADE
    )
    submission_id = models.UUIDField(unique=True)
    prepared_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-prepared_at"]
        indexes = [models.Index(fields=["election", "expires_at"])]

    def __str__(self) -> str:
        return f"preparation {self.submission_id}"


class BallotBox(models.Model):
    """Singleton-per-election lock/counter row (primary key = election)."""

    election = models.OneToOneField(
        "elections.Election", primary_key=True, related_name="ballot_box",
        on_delete=models.CASCADE,
    )
    last_seq = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ballot box {self.election_id} @ {self.last_seq}"


class Ballot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="ballots", on_delete=models.CASCADE
    )
    # Client-generated capability for idempotent retries.  Unlinkable to any
    # voter: it exists only on this row (preparations are deleted at accept).
    submission_id = models.UUIDField()
    receipt_code = models.CharField(max_length=32, unique=True)
    payload = models.TextField()  # Fernet-encrypted selections
    ballot_hash = models.CharField(max_length=64)
    config_hash = models.CharField(max_length=64, default="")
    station = models.ForeignKey(
        "elections.PollingStation", null=True, blank=True, related_name="ballots",
        on_delete=models.SET_NULL,
    )
    box_seq = models.BigIntegerField(default=0)
    accepted_at = models.DateTimeField()

    class Meta:
        ordering = ["accepted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "submission_id"], name="uniq_submission_per_election"
            ),
        ]
        indexes = [
            models.Index(fields=["election", "box_seq"]),
            models.Index(fields=["election", "accepted_at"]),
        ]

    def __str__(self) -> str:
        return f"ballot {self.pk} ({self.election_id})"
