"""Candidate representatives: accreditation, monitoring, incidents.

A representative acts for exactly one candidate in exactly one election and
sees only aggregated, policy-authorized information.  They can never read
ballots, voter credentials, or another candidate's restricted data - this is
enforced in ``representatives.views`` / ``monitoring.views`` and tested in
``representatives/tests``.
"""

import uuid

from django.conf import settings
from django.db import models


class Representative(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REVOKED = "revoked"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending approval"),
        (STATUS_APPROVED, "Approved / accredited"),
        (STATUS_REVOKED, "Revoked"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="representatives", on_delete=models.CASCADE
    )
    candidate = models.ForeignKey(
        "elections.Candidate", related_name="representatives", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="representative_assignments",
        on_delete=models.SET_NULL,
    )
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    accredited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "candidate", "email"],
                condition=~models.Q(email=""),
                name="uniq_rep_per_candidate_email",
            ),
        ]
        indexes = [models.Index(fields=["election", "status"])]

    @property
    def is_accredited(self) -> bool:
        return self.status == self.STATUS_APPROVED

    def __str__(self) -> str:
        return f"{self.full_name} for {self.candidate} ({self.election})"


class AttendanceRecord(models.Model):
    """Accreditation / presence log for representatives (no ballot data)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    representative = models.ForeignKey(
        Representative, related_name="attendance", on_delete=models.CASCADE
    )
    arrived_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-arrived_at"]
        indexes = [models.Index(fields=["representative", "-arrived_at"])]

    def __str__(self) -> str:
        return f"{self.representative} · {self.arrived_at:%Y-%m-%d %H:%M}"


class IncidentReport(models.Model):
    """Irregularity / objection / dispute raised by reps or officials."""

    CATEGORY_CHOICES = [
        ("incident", "Operational incident"),
        ("objection", "Objection"),
        ("dispute", "Dispute / appeal"),
        ("eligibility", "Eligibility challenge"),
        ("other", "Other"),
    ]
    SEVERITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
    ]
    STATUS_CHOICES = [
        ("open", "Open"),
        ("acknowledged", "Acknowledged"),
        ("resolved", "Resolved"),
        ("rejected", "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "elections.Election", related_name="incidents", on_delete=models.CASCADE
    )
    station = models.ForeignKey(
        "elections.PollingStation", null=True, blank=True, related_name="incidents",
        on_delete=models.SET_NULL,
    )
    reporter = models.ForeignKey(
        Representative, null=True, blank=True, related_name="incidents",
        on_delete=models.SET_NULL,
    )
    reporter_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    category = models.CharField(max_length=16, choices=CATEGORY_CHOICES, default="incident")
    severity = models.CharField(max_length=8, choices=SEVERITY_CHOICES, default="medium")
    title = models.CharField(max_length=255)
    description = models.TextField()
    evidence = models.FileField(upload_to="incidents/%Y/%m/", null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open")
    resolution_note = models.TextField(blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["election", "status"])]

    @property
    def is_visible_to_reporter(self) -> bool:
        return True  # reporters always see the status of their own report

    def __str__(self) -> str:
        return f"[{self.get_category_display()}] {self.title}"
