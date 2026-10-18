"""Tallying models: result versions, counts, certification artifacts.

Result immutability: once a ResultVersion reaches CALCULATED its rows are
frozen; corrections create a NEW version referencing the previous one
(``supersedes``) — certified results are never silently overwritten.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel


class ResultVersion(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CALCULATED = "calculated", "Calculated"
        SUPERSEDED = "superseded", "Superseded (recount)"
        CERTIFIED = "certified", "Certified"
        PUBLISHED = "published", "Published"

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="result_versions"
    )
    version_no = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by"
    )
    recount_reason = models.TextField(blank=True)
    # Reconciliation totals
    ballots_accepted = models.PositiveBigIntegerField(default=0)
    ballots_counted = models.PositiveBigIntegerField(default=0)
    ballots_invalid = models.PositiveBigIntegerField(default=0)
    ballots_spoiled = models.PositiveBigIntegerField(default=0)
    undervotes_total = models.PositiveBigIntegerField(default=0)
    overvotes_blocked = models.PositiveBigIntegerField(default=0)
    reconciled = models.BooleanField(default=False)
    # Deterministic artifact: sha256 over canonical tally content
    tally_hash = models.CharField(max_length=64, blank=True, editable=False)
    calculated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    calculated_at = models.DateTimeField(null=True, blank=True)
    certified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    certified_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-version_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "version_no"], name="uniq_result_version_per_election"
            ),
        ]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self):
        return f"{self.election.slug} v{self.version_no} ({self.status})"

    def freeze_if_calculated(self):
        if self.pk and self.status in (
            self.Status.CALCULATED,
            self.Status.CERTIFIED,
            self.Status.PUBLISHED,
            self.Status.SUPERSEDED,
        ):
            existing = ResultVersion.objects.filter(pk=self.pk).first()
            if existing and existing.status == self.status:
                # block casual modification of frozen rows
                raise ValidationError("Calculated result versions are immutable.")


class PositionResult(TimeStampedModel):
    """Per-position counts for one result version. Immutable after calc."""

    result_version = models.ForeignKey(
        ResultVersion, on_delete=models.CASCADE, related_name="position_results"
    )
    position = models.ForeignKey(
        "elections.Position", on_delete=models.PROTECT, related_name="+"
    )
    votes_for = models.JSONField(
        default=dict, help_text="{candidate_slug: votes} — aggregated counts only."
    )
    abstain_count = models.PositiveIntegerField(default=0)
    undervote_count = models.PositiveIntegerField(default=0)
    total_valid = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("result_version", "position")]
        indexes = [models.Index(fields=["result_version"])]

    def __str__(self):
        return f"{self.position.name} @ v{self.result_version_id}"

    def save(self, *args, **kwargs):
        if self.pk and self.result_version_id:
            rv = ResultVersion.objects.filter(pk=self.result_version_id).values_list(
                "status", flat=True
            ).first()
            if rv in (
                ResultVersion.Status.CERTIFIED,
                ResultVersion.Status.PUBLISHED,
                ResultVersion.Status.SUPERSEDED,
            ):
                raise ValidationError("Result rows are frozen for this version.")
        super().save(*args, **kwargs)


class StationTally(TimeStampedModel):
    """Per-station breakdown for reconciliation (channel-level reporting)."""

    result_version = models.ForeignKey(
        ResultVersion, on_delete=models.CASCADE, related_name="station_tallies"
    )
    station = models.ForeignKey(
        "elections.PollingStation", on_delete=models.PROTECT, related_name="+"
    )
    ballots_accepted = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("result_version", "station")]

    def __str__(self):
        return f"{self.station.code}: {self.ballots_accepted} @ v{self.result_version_id}"


class ReceiptRegister(TimeStampedModel):
    """Published receipt hashes (written at publication time) enabling voters
    to confirm their receipt is registered without revealing selections."""

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="receipt_register"
    )
    receipt_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        unique_together = [("election", "receipt_hash")]
        indexes = [models.Index(fields=["election", "receipt_hash"])]
