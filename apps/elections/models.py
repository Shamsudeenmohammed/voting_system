"""Election domain models: elections, positions, polling stations, schedules.

The lifecycle state machine lives in apps.elections.state_machine and is the
*only* place transitions are allowed to happen.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel, UUIDPrimaryKeyModel
from apps.orgs.models import Organization, Unit


class Election(TimeStampedModel):
    class State(models.TextChoices):
        DRAFT = "draft", "Draft"
        REGISTRATION = "registration", "Voter registration"
        CONFIGURATION_REVIEW = "config_review", "Configuration review"
        APPROVED = "approved", "Approved"
        SCHEDULED = "scheduled", "Scheduled"
        OPEN = "open", "Open for voting"
        CLOSED = "closed", "Closed"
        RECONCILIATION = "reconciliation", "Reconciliation"
        REVIEW = "review", "Review"
        CERTIFIED = "certified", "Certified"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    class ElectionType(models.TextChoices):
        SINGLE = "single", "Single-choice election"
        MULTI_POSITION = "multi_position", "Multiple-position election"

    class ResultVisibility(models.TextChoices):
        LIVE_AGGREGATE = "live_aggregate", "Live aggregate totals during voting"
        WITHHELD_UNTIL_CLOSE = "withheld_until_close", "Withheld until close"
        WITHHELD_UNTIL_PUBLISHED = "withheld_until_published", "Withheld until publication"

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="elections"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    election_type = models.CharField(
        max_length=20, choices=ElectionType.choices, default=ElectionType.SINGLE
    )
    state = models.CharField(
        max_length=20,
        choices=State.choices,
        default=State.DRAFT,
        db_index=True,
        editable=False,  # only the state machine may change this
    )

    # Scheduling
    registration_opens = models.DateTimeField(null=True, blank=True)
    registration_closes = models.DateTimeField(null=True, blank=True)
    scheduled_opens = models.DateTimeField(null=True, blank=True)
    scheduled_closes = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )

    # Frozen-config tracking (set by state machine on approval)
    config_frozen_at = models.DateTimeField(null=True, blank=True, editable=False)
    config_frozen_hash = models.CharField(max_length=64, blank=True, editable=False)

    # Result policy
    result_visibility = models.CharField(
        max_length=30,
        choices=ResultVisibility.choices,
        default=ResultVisibility.WITHHELD_UNTIL_CLOSE,
    )
    show_party_on_ballot = models.BooleanField(default=False)

    # Approvals — separate actors required for open/close vs certification
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    certified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    certified_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    # Reopen exception procedure (audit trail fields; see state_machine.reopen)
    reopen_reason = models.TextField(blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopen_count = models.PositiveSmallIntegerField(default=0)

    # Eligibility: freeze voter list when configuration is frozen
    voter_list_frozen = models.BooleanField(default=False, db_index=True)

    # Operational flags
    allow_abstain = models.BooleanField(default=True)
    min_turnout_pct = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Quorum: 0 disables the check.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "state"]),
            models.Index(fields=["state", "scheduled_opens"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scheduled_closes__isnull=True)
                | models.Q(scheduled_opens__isnull=True)
                | models.Q(scheduled_closes__gt=models.F("scheduled_opens")),
                name="election_close_after_open",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_state_display()})"

    @property
    def is_open_for_voting(self) -> bool:
        return self.state == self.State.OPEN

    @property
    def can_accept_ballots(self) -> bool:
        """Authoritative gate used by the voting engine."""
        now = timezone.now()
        if self.state != self.State.OPEN:
            return False
        if self.scheduled_closes and now > self.scheduled_closes:
            return False
        return True

    def clean(self):
        if self.scheduled_opens and self.scheduled_closes:
            if self.scheduled_closes <= self.scheduled_opens:
                raise ValidationError(
                    {"scheduled_closes": "Closing time must be after opening time."}
                )


class Position(TimeStampedModel):
    """Contested office. Multiple positions = multiple-position election."""

    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="positions")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80)
    description = models.TextField(blank=True)
    max_choices = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="1 = single choice; >1 = multi-select (approval-style) position.",
    )
    order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["order", "name"]
        unique_together = [("election", "slug")]
        indexes = [models.Index(fields=["election", "is_active", "order"])]

    def __str__(self):
        return f"{self.name} @ {self.election.name}"

    @property
    def is_single_choice(self) -> bool:
        return self.max_choices == 1


class PollingStation(TimeStampedModel):
    """Physical/online polling station or voting channel.

    Online-only elections still get one virtual station so that turnout can be
    reported per channel.
    """

    class Channel(models.TextChoices):
        ONLINE = "online", "Online (web)"
        ONSITE = "onsite", "On-site station"
        HYBRID = "hybrid", "Hybrid"

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        SUSPENDED = "suspended", "Suspended"

    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="stations")
    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=40)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.ONLINE)
    location_unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.SET_NULL, related_name="polling_stations"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PLANNED, db_index=True
    )
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("election", "code")]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self):
        return f"{self.name} [{self.get_channel_display()}]"


class ElectionMilestone(TimeStampedModel):
    """Operational milestone / notice shown on monitoring dashboards."""

    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_public = models.BooleanField(
        default=True, help_text="Public milestones also appear on voter-facing pages."
    )

    class Meta:
        ordering = ["due_at", "created_at"]
        indexes = [models.Index(fields=["election", "due_at"])]

    def __str__(self):
        return self.title


class LifecycleEvent(TimeStampedModel):
    """Append-only record of every state transition (mirrors audit app but
    scoped to the election for fast timeline rendering)."""

    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="lifecycle_events")
    from_state = models.CharField(max_length=20)
    to_state = models.CharField(max_length=20)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.TextField(blank=True)
    requires_second_approval = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["election", "created_at"])]

    def __str__(self):
        return f"{self.election}: {self.from_state} → {self.to_state}"
