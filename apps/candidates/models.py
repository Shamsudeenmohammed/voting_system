"""Candidate registration/approval and the Candidate Representatives module."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel


def candidate_photo_path(instance, filename):
    return f"elections/{instance.election_id}/candidates/{instance.pk}/{filename}"


class Candidate(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="candidates"
    )
    position = models.ForeignKey(
        "elections.Position", on_delete=models.CASCADE, related_name="candidates"
    )
    full_name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80)
    manifesto = models.TextField(blank=True)
    photo = models.ImageField(upload_to=candidate_photo_path, blank=True, null=True)
    slate_or_party = models.CharField(max_length=120, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    review_note = models.TextField(blank=True)
    withdrawal_note = models.TextField(blank=True)

    class Meta:
        ordering = ["position__order", "order", "full_name"]
        unique_together = [("election", "slug")]
        indexes = [
            models.Index(fields=["election", "position", "status"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.position.name})"

    def clean(self):
        if self.position_id and self.election_id and self.position.election_id != self.election_id:
            raise ValidationError("Position belongs to a different election.")

    @property
    def approved(self) -> bool:
        return self.status == self.Status.APPROVED


class Representative(TimeStampedModel):
    """Candidate Representatives Management module core record.

    Authorisation boundary: a representative may read *aggregate* monitoring
    data for their own election, and nothing identity- or ballot-related.
    Enforcement lives in apps.monitoring.views + apps.core.permissions and is
    covered by tests (tests/test_representative_permissions.py).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"

    class Attendance(models.TextChoices):
        NOT_MARKED = "not_marked", "Not marked"
        PRESENT = "present", "Present"
        ABSENT = "absent", "Absent"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="representations"
    )
    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="representatives"
    )
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="representatives"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    accreditation_code_hash = models.CharField(max_length=128, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    attendance = models.CharField(
        max_length=12, choices=Attendance.choices, default=Attendance.NOT_MARKED
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    session_epoch = models.PositiveIntegerField(
        default=0, help_text="Incrementing this invalidates all active sessions."
    )
    note = models.TextField(blank=True)

    class Meta:
        unique_together = [("user", "election", "candidate")]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self):
        return f"{self.user} for {self.candidate.full_name} @ {self.election.slug}"

    @property
    def can_monitor(self) -> bool:
        return self.status == self.Status.APPROVED


class Incident(TimeStampedModel):
    """Irregularity report / dispute filed by officials or representatives."""

    class Category(models.TextChoices):
        OPERATIONAL = "operational", "Operational issue"
        ELIGIBILITY = "eligibility", "Eligibility dispute"
        BALLOT = "ballot", "Ballot handling"
        COUNT = "count", "Count/reconciliation objection"
        SECURITY = "security", "Security incident"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        INVESTIGATING = "investigating", "Investigating"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"
        ESCALATED = "escalated", "Escalated"

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="incidents"
    )
    organization = models.ForeignKey(
        "orgs.Organization", on_delete=models.CASCADE, related_name="incidents"
    )
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    representative = models.ForeignKey(
        Representative, null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.OTHER)
    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    # Evidence attachments: names/sizes only in list views; access controlled.
    evidence = models.FileField(upload_to="incidents/%Y/%m/", blank=True)
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self):
        return f"[{self.get_status_display()}] {self.title}"
