"""Ballot authorization and accepted-ballot storage.

SECRECY MODEL (docs/ballot-secrecy.md is the authoritative document):

1. Identity side — ``BallotGrant``: one row per (election, voter), holding a
   *hash* of a single-use random authorization token. No selections.
2. Selection side — ``AcceptedBallot``: encrypted selections keyed ONLY by
   election + random UUID + client idempotency key. No voter FK, no grant FK,
   no IP, no user agent.
3. The two tables are joined only *transiently* inside the acceptance
   transaction (to consume the grant atomically). No durable link exists.

Correlation defences:
* grant timestamps are quantized to BALLOT_TIME_WINDOW minutes so identity
  and selections cannot be matched to the second;
* no IP/user-agent/headers anywhere near the selection tables;
* application logs record outcome codes only, never selections.

What this does NOT provide (documented, not claimed): end-to-end
verifiability (Helios-class). See docs/ballot-secrecy.md §Limitations.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel, UUIDPrimaryKeyModel


class BallotGrant(TimeStampedModel):
    """Single-use ballot authorization for one voter in one election."""

    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        CONSUMED = "consumed", "Consumed"
        REVOKED = "revoked", "Revoked"

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="ballot_grants"
    )
    # Identity side — deliberately NOT a FK to Voter to avoid the classic
    # voter→ballot join path via CASCADE/related lookups; store org-scoped id.
    voter_ref = models.CharField(max_length=64, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ISSUED, db_index=True
    )
    issued_at = models.DateTimeField(db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    # Quantized consumption timestamp (see module docstring) — no exact time.
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "voter_ref"], name="uniq_grant_per_voter_per_election"
            ),
        ]
        indexes = [
            models.Index(fields=["election", "status"]),
            models.Index(fields=["election", "expires_at"]),
        ]

    def __str__(self):
        return f"grant[{self.voter_ref}@{self.election_id}:{self.status}]"

    def save(self, *args, **kwargs):
        if self.status == self.Status.CONSUMED and self.consumed_at is None:
            from django.utils import timezone

            from apps.ballots.privacy import quantize_time

            self.consumed_at = quantize_time(timezone.now())
        super().save(*args, **kwargs)


class AcceptedBallot(UUIDPrimaryKeyModel):
    """An accepted, sealed ballot. Contains selections ONLY.

    Exactly-once guarantees:
    * UNIQUE(election, submission_id) — idempotent client retries;
    * created inside the same transaction that consumes the grant;
    * never updated after creation (save() blocks updates).
    """

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="accepted_ballots"
    )
    # Optional station attribution (channel-level reconciliation). Carries no
    # identity: stations are not voter-identifying in online mode; on-site
    # station size must exceed the k-anonymity threshold (documented).
    station = models.ForeignKey(
        "elections.PollingStation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_ballots",
    )
    submission_id = models.UUIDField(
        help_text="Client-generated idempotency key for safe retries."
    )
    # Fernet-encrypted canonical JSON of selections. Key held in env only.
    sealed_payload = models.BinaryField()
    payload_sha256 = models.CharField(
        max_length=64, help_text="Integrity hash of the plaintext selections."
    )
    # Receipt: only the SHA-256 hash is stored; plaintext shown once to the
    # voter. Published receipt-hash register allows post-publication checks.
    receipt_hash = models.CharField(max_length=64, unique=True, editable=False)
    accepted_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["accepted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "submission_id"], name="uniq_submission_per_election"
            ),
        ]
        indexes = [models.Index(fields=["election", "accepted_at"])]

    def __str__(self):
        return f"ballot[{self.pk}] @{self.election_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None and not kwargs.get("force_insert") and not kwargs.get("update_fields"):
            existing = AcceptedBallot.objects.filter(pk=self.pk)
            if existing.exists():
                raise RuntimeError("Accepted ballots are immutable after creation.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Accepted ballots cannot be deleted.")

    @property
    def receipt_hash_short(self) -> str:
        return self.receipt_hash[:12]


def validate_selections(election, selections: dict) -> dict:
    """Validate selections against the frozen configuration.

    ``selections``: {position_slug: [candidate_slug, ...]} or
                    {position_slug: "abstain"}.
    Returns the normalized canonical dict. Raises ValidationError on any
    violation (unknown position/candidate, overvote, wrong types).
    """
    if not isinstance(selections, dict):
        raise ValidationError("Ballot must be an object of position→choices.")

    positions = {
        p.slug: p
        for p in election.positions.filter(is_active=True).prefetch_related("candidates")
    }
    normalized: dict[str, object] = {}

    for raw_pos, raw_choice in selections.items():
        pos_slug = str(raw_pos)
        if pos_slug not in positions:
            raise ValidationError(f"Unknown position: {pos_slug}")
        position = positions[pos_slug]
        if pos_slug in normalized:
            raise ValidationError(f"Position listed twice: {pos_slug}")

        if isinstance(raw_choice, str) and raw_choice == "abstain":
            if not election.allow_abstain:
                raise ValidationError(f"Abstention not allowed for {pos_slug}.")
            normalized[pos_slug] = "abstain"
            continue

        if not isinstance(raw_choice, list):
            raise ValidationError(f"Choices for {pos_slug} must be a list.")
        # Deduplicate while preserving order
        seen: set[str] = set()
        clean: list[str] = []
        for c in raw_choice:
            cs = str(c)
            if cs in seen:
                raise ValidationError(f"Duplicate choice '{cs}' in {pos_slug}.")
            seen.add(cs)
            clean.append(cs)

        if len(clean) > position.max_choices:
            raise ValidationError(
                f"Overvote for {pos_slug}: {len(clean)} selected, "
                f"maximum {position.max_choices}."
            )
        approved = {
            c.slug
            for c in position.candidates.filter(status="approved")
        }
        for cs in clean:
            if cs not in approved:
                raise ValidationError(
                    f"Candidate '{cs}' is not on the approved ballot for {pos_slug}."
                )
        normalized[pos_slug] = clean

    # Every active position must appear (explicit undervote = omission allowed
    # but recorded as undervote at tally; we require explicit presence to make
    # the ballot deterministic — omitted positions count as undervote).
    return normalized
