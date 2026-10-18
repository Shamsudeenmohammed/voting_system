"""Election domain models.

Evolution notes (see docs/ARCHITECTURE.md):

* Every election belongs to an :class:`organizations.Organization` (tenant).
* :class:`Election.state` is the backend-enforced lifecycle state machine
  (``elections.lifecycle``); the old ``is_published`` flag is retained as a
  legacy *public visibility* switch for existing data.
* The legacy ``Vote`` model (voter <-> selections link) was removed: ballots
  now live in the ``ballot`` app with identity and selection separated.
* ``Voter.token`` was replaced by single-use hashed ballot authorizations in
  the ``ballot`` app.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Election(models.Model):
    CATEGORY_CHOICES = [
        ("general", "General Election"),
        ("tertiary", "Tertiary / University"),
        ("senior_high", "Senior High School"),
        ("primary", "Primary / Junior High"),
        ("local", "Local / Community"),
        ("corporate", "Corporate / Organization"),
    ]
    TYPE_CHOICES = [
        ("single", "Single choice"),
        ("multiple", "Multiple choice"),
    ]

    # --- lifecycle states -------------------------------------------------
    STATE_DRAFT = "draft"
    STATE_REGISTRATION = "registration"
    STATE_CONFIG_REVIEW = "config_review"
    STATE_APPROVED = "approved"
    STATE_SCHEDULED = "scheduled"
    STATE_OPEN = "open"
    STATE_CLOSED = "closed"
    STATE_RECONCILIATION = "reconciliation"
    STATE_REVIEW = "review"
    STATE_CERTIFIED = "certified"
    STATE_PUBLISHED = "published"
    STATE_ARCHIVED = "archived"

    STATE_CHOICES = [
        (STATE_DRAFT, "Draft"),
        (STATE_REGISTRATION, "Registration"),
        (STATE_CONFIG_REVIEW, "Configuration review"),
        (STATE_APPROVED, "Approved"),
        (STATE_SCHEDULED, "Scheduled"),
        (STATE_OPEN, "Open"),
        (STATE_CLOSED, "Closed"),
        (STATE_RECONCILIATION, "Reconciliation"),
        (STATE_REVIEW, "Review"),
        (STATE_CERTIFIED, "Certified"),
        (STATE_PUBLISHED, "Published"),
        (STATE_ARCHIVED, "Archived"),
    ]

    # --- result visibility policy ----------------------------------------
    VISIBILITY_AFTER_CLOSE = "after_close"
    VISIBILITY_AFTER_CERT = "after_certification"
    VISIBILITY_LIVE = "live_totals"
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_CHOICES = [
        (VISIBILITY_AFTER_CLOSE, "Candidate totals after closing"),
        (VISIBILITY_AFTER_CERT, "Candidate totals after certification"),
        (VISIBILITY_LIVE, "Live candidate totals while voting"),
        (VISIBILITY_PRIVATE, "Never public (officials/auditors only)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # nullable only until migration 0002 backfills existing rows with the
    # default organization; tightened to NOT NULL at the end of that migration.
    organization = models.ForeignKey(
        "organizations.Organization",
        related_name="elections",
        on_delete=models.PROTECT,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, default="general")
    ballot_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default="single")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()

    # Lifecycle
    state = models.CharField(
        max_length=24, choices=STATE_CHOICES, default=STATE_DRAFT, db_index=True
    )
    state_updated_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    certified_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    registry_frozen_at = models.DateTimeField(null=True, blank=True)
    config_frozen_at = models.DateTimeField(null=True, blank=True)
    config_hash = models.CharField(max_length=64, blank=True, default="")
    reopened_count = models.PositiveSmallIntegerField(default=0)

    # Rules & visibility
    results_visibility = models.CharField(
        max_length=24,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_AFTER_CLOSE,
    )
    allow_blank = models.BooleanField(default=True)
    eligibility_units = models.JSONField(default=list, blank=True)  # [] = all units
    rules = models.JSONField(default=dict, blank=True)

    # Legacy switches kept for existing data / simple public listing
    is_published = models.BooleanField(default=False)  # publicly listed
    show_results = models.BooleanField(default=True)   # legacy visibility hint

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "state"])]

    # -- convenience -------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.state == self.STATE_OPEN

    def is_open_legacy(self) -> bool:
        now = timezone.now()
        return self.is_published and self.start_at <= now <= self.end_at

    def total_voters(self) -> int:
        return self.voters.filter(status=Voter.STATUS_ACTIVE).count()

    def voted_count(self) -> int:
        return self.voters.filter(status=Voter.STATUS_ACTIVE, has_voted=True).count()

    @property
    def registration_open(self) -> bool:
        return self.state == self.STATE_REGISTRATION

    @property
    def frozen(self) -> bool:
        return self.config_frozen_at is not None

    @property
    def accepts_ballots(self) -> bool:
        return self.state == self.STATE_OPEN

    @property
    def results_published(self) -> bool:
        return self.state in (self.STATE_PUBLISHED, self.STATE_ARCHIVED)

    @property
    def state_badge(self) -> str:
        return {
            self.STATE_DRAFT: "badge-closed",
            self.STATE_REGISTRATION: "badge-category",
            self.STATE_CONFIG_REVIEW: "badge-category",
            self.STATE_APPROVED: "badge-category",
            self.STATE_SCHEDULED: "badge-category",
            self.STATE_OPEN: "badge-open",
            self.STATE_CLOSED: "badge-closed",
            self.STATE_RECONCILIATION: "badge-closed",
            self.STATE_REVIEW: "badge-closed",
            self.STATE_CERTIFIED: "badge-open",
            self.STATE_PUBLISHED: "badge-open",
            self.STATE_ARCHIVED: "badge-closed",
        }.get(self.state, "badge-closed")

    def __str__(self) -> str:
        return self.title


class PollingStation(models.Model):
    """Physical/online voting station (or counting unit)."""

    STATUS_CHOICES = [
        ("planned", "Planned"),
        ("open", "Open"),
        ("closed", "Closed"),
        ("issue", "Reported issue"),
    ]
    CHANNEL_CHOICES = [
        ("onsite", "On-site (polling station)"),
        ("online", "Online (remote)"),
        ("hybrid", "Hybrid"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="stations", on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=64, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, default="onsite")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="planned")
    notes = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["election", "code"],
                condition=~models.Q(code=""),
                name="uniq_station_code_per_election",
            )
        ]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.election})"


class Position(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="positions", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    order = models.IntegerField(default=0)
    max_choices = models.PositiveSmallIntegerField(
        default=1, help_text="How many candidates a voter may pick for this position."
    )

    class Meta:
        ordering = ["order"]

    def __str__(self) -> str:
        return f"{self.election.title} — {self.title}"


class ElectionApproval(models.Model):
    """Persisted four-eyes approval records for sensitive transitions.

    * ``approve_config`` - configuration review -> approved (an election may
      not open without an approved configuration).
    * ``reopen``         - documented exception procedure to reopen a closed
      election (mandatory reason + independent approver).
    * ``decertify``      - correction / recount request after certification
      or publication.

    The approver must be a *different* user than the requester; this is
    enforced in ``elections.lifecycle.decide_approval``.
    """

    KIND_CHOICES = [
        ("approve_config", "Configuration approval"),
        ("reopen", "Reopen closed election"),
        ("decertify", "Correction / decertify results"),
    ]
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        Election, related_name="approvals", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(help_text="Documented justification (mandatory).")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="+",
        on_delete=models.SET_NULL,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["election", "status"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.election} ({self.status})"


class Candidate(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_NOMINATED = "nominated"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_NOMINATED, "Nominated (pending review)"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    position = models.ForeignKey(Position, related_name="candidates", on_delete=models.CASCADE)
    full_name = models.CharField(max_length=255)
    bio = models.TextField(blank=True)
    manifesto = models.TextField(blank=True, default="")
    photo = models.ImageField(upload_to="candidates/", null=True, blank=True)
    order = models.IntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_APPROVED, db_index=True
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order"]

    @property
    def is_on_ballot(self) -> bool:
        return self.status == self.STATUS_APPROVED

    def __str__(self) -> str:
        return self.full_name


class Voter(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_DISPUTED = "disputed"
    STATUS_SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_WITHDRAWN, "Withdrawn"),
        (STATUS_DISPUTED, "Eligibility disputed"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, related_name="voters", on_delete=models.CASCADE)
    voter_id = models.CharField(max_length=255, verbose_name="Voter ID")
    full_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(null=True, blank=True)
    organization_unit = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Faculty / hall / constituency / department this voter belongs to.",
    )
    station = models.ForeignKey(
        PollingStation, null=True, blank=True, related_name="voters",
        on_delete=models.SET_NULL,
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True
    )
    # Voting status (turnout) - intentionally coarse: no ballot linkage.
    has_voted = models.BooleanField(default=False, db_index=True)
    voted_at = models.DateTimeField(null=True, blank=True)
    credentials_issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # Blank legacy identifiers are excluded so historical rows keep
            # importing cleanly; real duplicates are rejected.
            models.UniqueConstraint(
                fields=["election", "voter_id"],
                condition=~models.Q(voter_id=""),
                name="uniq_voter_id_per_election",
            ),
        ]
        indexes = [
            models.Index(fields=["election", "status"]),
            models.Index(fields=["election", "has_voted"]),
        ]

    def eligible_for(self, election: Election) -> bool:
        if self.status != self.STATUS_ACTIVE:
            return False
        if election.eligibility_units and self.organization_unit not in election.eligibility_units:
            return False
        return True

    @property
    def credentials_issued(self) -> bool:
        return self.credentials_issued_at is not None

    def __str__(self) -> str:
        return self.full_name or self.voter_id


# ---------------------------------------------------------------------------
# NOTE: the legacy ``Vote`` (voter <-> selections) and ``AuditLog`` models
# were removed by migration elections/0003 after their rows had been
# converted to sealed ballots (ballot/0002) and hash-chained audit events
# (audit/0002).
# ---------------------------------------------------------------------------
