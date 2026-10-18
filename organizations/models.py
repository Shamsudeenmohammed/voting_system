"""Multi-tenancy primitives: organizations and role memberships.

Every election belongs to exactly one :class:`Organization`.  Access to
election data is always scoped through the requesting user's membership in
that organization (see ``organizations.roles``).
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify


class Organization(models.Model):
    """A tenant: institution, community, association, or a sub-unit of one.

    ``parent`` models hierarchies such as institution -> campus -> faculty ->
    department, or community -> constituency.  Tenancy isolation is enforced
    per *root* organization via :func:`organizations.roles.root_organization`.
    """

    KIND_CHOICES = [
        ("institution", "Tertiary institution"),
        ("campus", "Campus"),
        ("faculty", "Faculty / School"),
        ("department", "Department"),
        ("hall", "Hall / Residence"),
        ("association", "Student / Professional association"),
        ("community", "Community"),
        ("constituency", "Constituency / Ward"),
        ("committee", "Election committee"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, default="institution")
    parent = models.ForeignKey(
        "self", null=True, blank=True, related_name="children", on_delete=models.CASCADE
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "org"
            slug, n = base, 2
            while Organization.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def root(self) -> "Organization":
        node = self
        seen = set()
        while node.parent_id and node.pk not in seen:
            seen.add(node.pk)
            node = node.parent
        return node

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"


class OrganizationMembership(models.Model):
    """Role assignment of a Django user inside one organization."""

    ROLE_CHOICES = [
        ("org_admin", "Organization administrator"),
        ("election_official", "Election official"),
        ("auditor", "Independent auditor"),
        ("observer", "Observer (read-only, public data)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="org_memberships", on_delete=models.CASCADE
    )
    organization = models.ForeignKey(
        Organization, related_name="memberships", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=32, choices=ROLE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization", "role"], name="uniq_org_membership"
            )
        ]
        indexes = [models.Index(fields=["organization", "role"])]

    def __str__(self) -> str:
        return f"{self.user} · {self.get_role_display()} @ {self.organization}"
