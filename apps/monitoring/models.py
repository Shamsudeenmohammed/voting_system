"""Monitoring models: notices and station health snapshots (read-models).

Aggregate dashboards read directly from source tables with annotated queries
(see apps.monitoring.queries) — no denormalized fake statistics anywhere.
"""
from django.db import models

from apps.core.models import TimeStampedModel


class OperationalNotice(TimeStampedModel):
    """Notice broadcast to monitoring rooms (officials + representatives).

    Contains operational text only — never voter data, never selections.
    """

    election = models.ForeignKey(
        "elections.Election", on_delete=models.CASCADE, related_name="notices"
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_active = models.BooleanField(default=True, db_index=True)
    audience = models.CharField(
        max_length=20,
        choices=[
            ("all", "Everyone"),
            ("officials", "Election officials"),
            ("reps", "Candidate representatives"),
        ],
        default="all",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["election", "is_active"])]

    def __str__(self):
        return self.title
