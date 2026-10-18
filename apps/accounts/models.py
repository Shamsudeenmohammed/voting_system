"""Custom user model.

Staff/admin users live here. Voter accounts are separate (apps.voters) and
cannot log into admin surfaces; see docs/ballot-secrecy.md for why voter
identity is kept out of the accounts table.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Meta:
        indexes = [models.Index(fields=["email"])]

    email = models.EmailField("email address", blank=True, db_index=True)
    display_name = models.CharField(max_length=150, blank=True)

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def role_label(self) -> str:
        if self.is_superuser:
            return "System administrator"
        from apps.orgs.models import Membership

        role = (
            Membership.objects.filter(user=self, is_active=True)
            .values_list("role", flat=True)
            .first()
        )
        return Membership.Role(role).label if role else "User"
