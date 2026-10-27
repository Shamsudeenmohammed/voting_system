"""Authority tests: Super Admin, Election commission chair and scope.

These lock in the *server-side* authority model described in
``organizations/roles.py``:

* the **Super Admin** (Django ``is_superuser``) can complete every admin task
  without another person's approval, is exempt from institution scoping, and
  has a dedicated control centre + audited recovery actions;
* an **Election commission chair** oversees an institution's elections (and may
  appoint officials/observers) but must not administer the tenant lifecycle or
  escalate roles;
* an **institution administrator** stays inside its own organization subtree;
* ordinary users cannot escalate through request parameters or by calling a
  restricted URL directly.

The ``ec_chair`` role is a choices-only addition (``OrganizationMembership``
``ROLE_CHOICES``); a migration is intentionally NOT generated.  The first test
documents that memberships of that role are representable and usable without a
schema change (choices are validated by forms, not by the database).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from elections.models import Election
from elections.tests import BaseVotingTestCase

from .models import Organization, OrganizationMembership
from .roles import (
    access_flags,
    can_manage_officials,
    chair_organizations_for,
    grantable_roles,
    has_system_wide_scope,
    is_ec_chair,
)

User = get_user_model()


class AuthorityFixture(BaseVotingTestCase):
    """Adds an election-commission chair and a second tenant's election."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser("root", "root@example.org", "x")
        cls.chair = User.objects.create_user("chair", password="x", is_staff=True)
        OrganizationMembership.objects.create(
            user=cls.chair, organization=cls.org, role="ec_chair"
        )
        now = timezone.now()
        common = dict(
            start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=3)
        )
        cls.election = Election.objects.create(
            organization=cls.org,
            title="Uni election",
            created_by=cls.official,
            **common,
        )
        cls.other_election = Election.objects.create(
            organization=cls.other_org,
            title="Other election",
            created_by=cls.outsider,
            **common,
        )


class RepresentationTests(AuthorityFixture):
    def test_ec_chair_membership_works_without_migration(self):
        # choices-only addition: no DB constraint, so this needs no migration
        membership = OrganizationMembership.objects.get(user=self.chair)
        self.assertEqual(membership.role, "ec_chair")
        self.assertEqual(membership.get_role_display(), "Election commission chair")
        self.assertTrue(is_ec_chair(self.chair, self.org))
        self.assertTrue(chair_organizations_for(self.chair).filter(pk=self.org.pk).exists())


class SuperAdminAuthorityTests(AuthorityFixture):
    def test_superadmin_reaches_every_console_page(self):
        self.client.force_login(self.superuser)
        for name in (
            "dashboard",
            "chair_dashboard",
            "operations",
            "approvals_inbox",
            "manage_organizations",
            "manage_people",
            "control_center",
            "control_center_search",
            "control_center_overrides",
            "control_center_recovery",
            "settings_overview",
        ):
            with self.subTest(page=name):
                r = self.client.get(reverse(name))
                self.assertEqual(r.status_code, 200)

    def test_superadmin_flags_show_console_and_system_scope(self):
        flags = access_flags(self.superuser)
        self.assertTrue(flags["is_super_admin"])
        self.assertTrue(flags["is_console_user"])
        self.assertTrue(flags["can_manage_any"])
        self.assertTrue(has_system_wide_scope(self.superuser))

    def test_superadmin_can_grant_any_role_in_any_organization(self):
        target = User.objects.create_user("grantee", password="x")
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(target.pk),
                "organization": str(self.other_org.pk),
                "role": "org_admin",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=target, organization=self.other_org, role="org_admin"
            ).exists()
        )

    def test_superadmin_can_create_and_toggle_organization(self):
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("manage_organizations"),
            {
                "action": "create",
                "name": "New Community",
                "kind": "community",
                "parent": "",
                "description": "",
                "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        created = Organization.objects.get(name="New Community")

        self.org.refresh_from_db()
        before = self.org.is_active
        self.client.post(
            reverse("manage_organizations"),
            {"action": "toggle", "organization": str(self.org.pk)},
        )
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.is_active, before)

    def test_control_center_requires_login(self):
        r = self.client.get(reverse("control_center"))
        self.assertEqual(r.status_code, 302)

    def test_control_center_forbidden_to_non_superusers(self):
        for user in (self.admin, self.official, self.chair):
            self.client.force_login(user)
            for name in (
                "control_center",
                "control_center_search",
                "control_center_overrides",
                "control_center_recovery",
            ):
                with self.subTest(user=user.username, page=name):
                    self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_control_center_renders_health_and_counts(self):
        self.client.force_login(self.superuser)
        r = self.client.get(reverse("control_center"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("counts", r.context)
        self.assertTrue(r.context["checks"])
        self.assertEqual(r.context["counts"]["elections"], 2)

    def test_system_search_finds_across_tenants(self):
        self.client.force_login(self.superuser)
        r = self.client.get(reverse("control_center_search"), {"q": "Other"})
        self.assertEqual(r.status_code, 200)
        titles = {e.title for e in r.context["elections"]}
        names = {o.name for o in r.context["organizations"]}
        self.assertIn("Other election", titles)
        self.assertIn("Other", names)

    # -- recovery actions -------------------------------------------------
    def test_recovery_restore_console_access(self):
        target = User.objects.create_user("lockedout", password="x", is_staff=False)
        OrganizationMembership.objects.create(
            user=target, organization=self.org, role="election_official"
        )
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("control_center_recovery"),
            {"action": "restore_console_access", "username": "lockedout", "reason": "role added"},
        )
        self.assertEqual(r.status_code, 302)
        target.refresh_from_db()
        self.assertTrue(target.is_staff)
        self.assertTrue(
            AuditEvent.objects.filter(action="recovery_console_access").exists()
        )

    def test_recovery_unlock_user(self):
        target = User.objects.create_user("disabled", password="x", is_active=False)
        self.client.force_login(self.superuser)
        self.client.post(
            reverse("control_center_recovery"),
            {"action": "unlock_user", "username": "disabled"},
        )
        target.refresh_from_db()
        self.assertTrue(target.is_active)

    def test_recovery_toggle_organization(self):
        self.client.force_login(self.superuser)
        before = self.other_org.is_active
        self.client.post(
            reverse("control_center_recovery"),
            {"action": "toggle_organization", "organization": str(self.other_org.pk)},
        )
        self.other_org.refresh_from_db()
        self.assertNotEqual(self.other_org.is_active, before)

    def test_recovery_clear_config_cache(self):
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("control_center_recovery"),
            {"action": "clear_config_cache", "election": str(self.election.pk)},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            AuditEvent.objects.filter(action="recovery_config_cache_cleared").exists()
        )

    def test_recovery_is_forbidden_and_inert_for_non_superusers(self):
        target = User.objects.create_user("someone", password="x", is_active=False)
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("control_center_recovery"),
            {"action": "unlock_user", "username": "someone"},
        )
        self.assertEqual(r.status_code, 403)
        target.refresh_from_db()
        self.assertFalse(target.is_active)


class ChairAuthorityTests(AuthorityFixture):
    def test_chair_can_oversee_but_not_administer_tenant(self):
        self.client.force_login(self.chair)
        self.assertEqual(self.client.get(reverse("manage_people")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("manage_organizations")).status_code, 403
        )
        # no control centre for a chair unless they are also a Super Admin
        self.assertEqual(self.client.get(reverse("control_center")).status_code, 403)

    def test_chair_oversight_dashboard_renders(self):
        self.client.force_login(self.chair)
        r = self.client.get(reverse("chair_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.context["election_total"], 1)

    def test_chair_oversight_dashboard_forbidden_to_official(self):
        self.client.force_login(self.official)
        self.assertEqual(self.client.get(reverse("chair_dashboard")).status_code, 403)

    def test_chair_grantable_roles_exclude_privileged_roles(self):
        self.assertEqual(
            set(grantable_roles(self.chair, self.org)),
            {"election_official", "observer"},
        )
        self.assertTrue(can_manage_officials(self.chair, self.org))

    def test_chair_cannot_grant_admin_role_via_post(self):
        target = User.objects.create_user("climber", password="x")
        self.client.force_login(self.chair)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(target.pk),
                "organization": str(self.org.pk),
                "role": "org_admin",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(
            OrganizationMembership.objects.filter(user=target).exists()
        )

    def test_chair_can_grant_official_and_observer_in_scope(self):
        target = User.objects.create_user("recruit", password="x")
        self.client.force_login(self.chair)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(target.pk),
                "organization": str(self.org.pk),
                "role": "election_official",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=target, organization=self.org, role="election_official"
            ).exists()
        )

    @override_settings(EC_CHAIR_SYSTEM_SCOPE_USERNAMES=["chair"])
    def test_system_wide_chair_grant_is_explicit(self):
        self.assertTrue(has_system_wide_scope(self.chair))
        # system-wide oversight covers every tenant's organizations ...
        self.assertTrue(
            chair_organizations_for(self.chair).filter(pk=self.other_org.pk).exists()
        )
        # ... but still not the tenant lifecycle console or the control centre
        self.client.force_login(self.chair)
        self.assertEqual(
            self.client.get(reverse("manage_organizations")).status_code, 403
        )
        self.assertEqual(self.client.get(reverse("control_center")).status_code, 403)


class InstitutionAdminScopingTests(AuthorityFixture):
    def test_org_admin_cannot_grant_into_another_tenant(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(self.outsider.pk),
                "organization": str(self.other_org.pk),
                "role": "observer",
            },
        )
        # the scoped queryset rejects the out-of-scope organization
        self.assertEqual(r.status_code, 302)
        self.assertFalse(
            OrganizationMembership.objects.filter(
                user=self.outsider, organization=self.other_org, role="observer"
            ).exists()
        )

    def test_org_admin_cannot_access_other_tenants_election(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("election_config", args=[self.other_election.pk]))
        self.assertEqual(r.status_code, 403)

    def test_superadmin_is_exempt_from_institution_scoping(self):
        self.client.force_login(self.superuser)
        r = self.client.get(reverse("election_config", args=[self.other_election.pk]))
        self.assertEqual(r.status_code, 200)


class PrivilegeEscalationTests(AuthorityFixture):
    def test_official_cannot_reach_people_console_at_all(self):
        target = User.objects.create_user("pawn", password="x")
        self.client.force_login(self.official)
        self.assertEqual(self.client.get(reverse("manage_people")).status_code, 403)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(target.pk),
                "organization": str(self.org.pk),
                "role": "org_admin",
            },
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(OrganizationMembership.objects.filter(user=target).exists())

    def test_official_cannot_create_superuser_account(self):
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "create_user",
                "username": "escalator",
                "password1": "V3ry-Str0ng-Passw0rd!",
                "password2": "V3ry-Str0ng-Passw0rd!",
                "organization": str(self.org.pk),
                "role": "org_admin",
            },
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username="escalator").exists())
