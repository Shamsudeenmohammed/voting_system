"""Multi-tenant organization models.

Every domain object (voter, candidate, election, ballot, audit event) carries
an ``organization`` FK. All querysets in views/services are scoped by it —
see docs/architecture.md §Tenancy.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from apps.core.models import TimeStampedModel


class Organization(TimeStampedModel):
    class OrgType(models.TextChoices):
        TERTIARY = "tertiary", "Tertiary institution"
        COMMUNITY = "community", "Community"
        ASSOCIATION = "association", "Professional association"
        OTHER = "other", "Other"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=80, unique=True)
    org_type = models.CharField(max_length=20, choices=OrgType.choices, db_index=True)
    description = models.TextField(blank=True)
    contact_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "organizations"
        indexes = [models.Index(fields=["org_type", "is_active"])]

    def __str__(self):
        return self.name


class Unit(TimeStampedModel):
    """Structural unit inside an organization.

    Tertiary: campus/faculty/department/hall/association.
    Community: constituency/ward/polling-area.
    Generic: any grouping used by eligibility rules.
    """

    class UnitType(models.TextChoices):
        CAMPUS = "campus", "Campus"
        FACULTY = "faculty", "Faculty"
        DEPARTMENT = "department", "Department"
        HALL = "hall", "Hall"
        ASSOCIATION = "association", "Association"
        CONSTITUENCY = "constituency", "Constituency"
        WARD = "ward", "Ward"
        POLLING_AREA = "polling_area", "Polling area"
        OTHER = "other", "Other"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="units"
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    name = models.CharField(max_length=200)
    unit_type = models.CharField(max_length=20, choices=UnitType.choices, db_index=True)

    class Meta:
        ordering = ["unit_type", "name"]
        unique_together = [("organization", "name", "unit_type")]
        indexes = [models.Index(fields=["organization", "unit_type"])]

    def __str__(self):
        return f"{self.get_unit_type_display()}: {self.name}"

    def clean(self):
        if self.parent_id and self.parent.organization_id != self.organization_id:
            raise ValidationError("Parent unit must belong to the same organization.")


class Membership(TimeStampedModel):
    """Staff association between a user and an organization with a role."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Organization administrator"
        ELECTION_OFFICIAL = "official", "Election official"
        AUDITOR = "auditor", "Independent auditor"
        OBSERVER = "observer", "Observer"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        unique_together = [("user", "organization")]
        indexes = [models.Index(fields=["organization", "role", "is_active"])]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} @ {self.organization}"

    @property
    def role_rank(self) -> int:
        # Higher number = more authority; used for ordering only.
        return {"admin": 4, "official": 3, "auditor": 2, "observer": 1}.get(self.role, 0)
