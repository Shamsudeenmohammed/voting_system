"""Tenancy isolation and role-predicate tests.

These are the rules every view and service relies on: a user may only reach
election data through a membership in that election's organization (or its
root), and the four roles have strictly different powers.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import AnonymousUser

from elections.models import Election

from .models import Organization, OrganizationMembership
from .roles import (
    can_approve_election,
    can_manage_election,
    can_view_election,
    can_view_results_early,
    is_election_staff,
    organizations_for,
    role_in,
    user_org_roles,
)

User = get_user_model()


class RolePredicateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Uni", slug="uni")
        cls.child = Organization.objects.create(
            name="Faculty", slug="faculty", parent=cls.org
        )
        cls.other_org = Organization.objects.create(name="Other", slug="other")

        cls.admin = User.objects.create_user("admin", password="x")
        cls.official = User.objects.create_user("official", password="x")
        cls.auditor = User.objects.create_user("auditor", password="x")
        cls.observer = User.objects.create_user("observer", password="x")
        cls.outsider = User.objects.create_user("outsider", password="x")
        cls.superuser = User.objects.create_superuser("root", password="x")

        OrganizationMembership.objects.create(
            user=cls.admin, organization=cls.org, role="org_admin"
        )
        OrganizationMembership.objects.create(
            user=cls.official, organization=cls.org, role="election_official"
        )
        OrganizationMembership.objects.create(
            user=cls.auditor, organization=cls.org, role="auditor"
        )
        OrganizationMembership.objects.create(
            user=cls.observer, organization=cls.org, role="observer"
        )
        OrganizationMembership.objects.create(
            user=cls.outsider, organization=cls.other_org, role="org_admin"
        )

        now = timezone.now()
        common = dict(start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=3))
        cls.election = Election.objects.create(
            organization=cls.org, title="Uni election", created_by=cls.admin, **common
        )
        cls.child_election = Election.objects.create(
            organization=cls.child, title="Faculty election", created_by=cls.admin, **common
        )
        cls.other_election = Election.objects.create(
            organization=cls.other_org, title="Other election", created_by=cls.outsider, **common
        )

    # -- cross-tenant isolation ------------------------------------------
    def test_outsider_has_no_access_to_other_tenants_election(self):
        for user in (self.outsider,):
            self.assertFalse(can_manage_election(user, self.election))
            self.assertFalse(can_approve_election(user, self.election))
            self.assertFalse(can_view_election(user, self.election))
            self.assertFalse(is_election_staff(user, self.election))

    def test_member_of_one_org_cannot_manage_sibling_election(self):
        # admin is org_admin of self.org only - not of other_org
        self.assertFalse(can_manage_election(self.admin, self.other_election))
        self.assertFalse(can_view_election(self.admin, self.other_election))
        # ... but each tenant admin manages their own
        self.assertTrue(can_manage_election(self.outsider, self.other_election))

    # -- role powers ------------------------------------------------------
    def test_official_can_manage_but_not_approve(self):
        self.assertTrue(can_manage_election(self.official, self.election))
        self.assertFalse(can_approve_election(self.official, self.election))
        self.assertTrue(is_election_staff(self.official, self.election))

    def test_admin_can_approve(self):
        self.assertTrue(can_manage_election(self.admin, self.election))
        self.assertTrue(can_approve_election(self.admin, self.election))

    def test_auditor_is_read_only(self):
        self.assertFalse(can_manage_election(self.auditor, self.election))
        self.assertFalse(can_approve_election(self.auditor, self.election))
        self.assertTrue(can_view_election(self.auditor, self.election))
        self.assertTrue(is_election_staff(self.auditor, self.election))
        self.assertTrue(can_view_results_early(self.auditor, self.election))

    def test_observer_is_read_only_without_early_results(self):
        self.assertFalse(can_manage_election(self.observer, self.election))
        self.assertTrue(can_view_election(self.observer, self.election))
        self.assertFalse(can_view_results_early(self.observer, self.election))
        # observers are not staff: staff-only surfaces (incidents, exports)
        # must refuse them
        self.assertFalse(is_election_staff(self.observer, self.election))

    def test_superuser_has_everything(self):
        self.assertTrue(can_manage_election(self.superuser, self.other_election))
        self.assertTrue(can_approve_election(self.superuser, self.other_election))
        self.assertTrue(can_view_election(self.superuser, self.other_election))
        self.assertTrue(can_view_results_early(self.superuser, self.other_election))

    def test_anonymous_has_no_access(self):
        anon = AnonymousUser()
        self.assertFalse(can_manage_election(anon, self.election))
        self.assertFalse(can_approve_election(anon, self.election))
        self.assertFalse(can_view_election(anon, self.election))
        self.assertFalse(is_election_staff(anon, self.election))
        self.assertFalse(organizations_for(anon).exists())

    # -- hierarchy scoping ------------------------------------------------
    def test_root_membership_grants_access_to_child_org_election(self):
        # official is a member of the *root* -> reaches the faculty election
        self.assertTrue(can_manage_election(self.official, self.child_election))
        self.assertEqual(self.child.root().pk, self.org.pk)

    def test_child_membership_does_not_grant_access_to_root_election(self):
        deep = User.objects.create_user("dean", password="x")
        OrganizationMembership.objects.create(
            user=deep, organization=self.child, role="org_admin"
        )
        self.assertTrue(can_manage_election(deep, self.child_election))
        self.assertFalse(can_manage_election(deep, self.election))
        self.assertFalse(can_view_election(deep, self.election))

    # -- queryset scoping -------------------------------------------------
    def test_organizations_for_returns_manageable_orgs_only(self):
        # root-org managers administrate the whole subtree (see role_in)
        self.assertEqual(set(organizations_for(self.admin)), {self.org, self.child})
        self.assertEqual(set(organizations_for(self.official)), {self.org, self.child})
        self.assertEqual(set(organizations_for(self.outsider)), {self.other_org})
        self.assertFalse(organizations_for(self.auditor).exists())
        self.assertFalse(organizations_for(self.observer).exists())
        superuser_orgs = set(organizations_for(self.superuser))
        expected = {self.org, self.child, self.other_org}
        self.assertTrue(expected.issubset(superuser_orgs))

    def test_role_in_requires_membership(self):
        self.assertTrue(role_in(self.admin, self.org, ("org_admin",)))
        self.assertFalse(role_in(self.outsider, self.org, ("org_admin",)))
        self.assertFalse(role_in(self.admin, None, ("org_admin",)))

    def test_user_org_roles_prefers_highest_role(self):
        # auditor *and* org_admin in the same org -> org_admin wins
        OrganizationMembership.objects.create(
            user=self.auditor, organization=self.org, role="org_admin"
        )
        self.assertEqual(user_org_roles(self.auditor)[str(self.org.pk)], "org_admin")
        self.assertEqual(user_org_roles(self.official)[str(self.org.pk)], "election_official")
        self.assertEqual(user_org_roles(self.outsider), {str(self.other_org.pk): "org_admin"})


class ConsoleViewTests(TestCase):
    """HTTP tests for the organization / people-access console.

    These exercise the product surface a Super Admin (or a scoped ``org_admin``)
    actually uses to onboard people: creating tenants, editing/toggling them,
    granting/revoking roles and creating accounts.  The console must never let a
    tenant admin act outside their own organization subtree.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Uni", slug="uni")
        cls.child = Organization.objects.create(name="Faculty", slug="faculty", parent=cls.org)
        cls.other_org = Organization.objects.create(name="Other", slug="other")

        cls.admin = User.objects.create_user("admin", password="x")
        cls.official = User.objects.create_user("official", password="x")
        cls.observer = User.objects.create_user("observer", password="x")
        cls.outsider = User.objects.create_user("outsider", password="x")
        cls.superuser = User.objects.create_superuser("root", password="x")

        OrganizationMembership.objects.create(
            user=cls.admin, organization=cls.org, role="org_admin"
        )
        OrganizationMembership.objects.create(
            user=cls.official, organization=cls.org, role="election_official"
        )
        OrganizationMembership.objects.create(
            user=cls.observer, organization=cls.org, role="observer"
        )
        OrganizationMembership.objects.create(
            user=cls.outsider, organization=cls.other_org, role="org_admin"
        )

    # -- access -----------------------------------------------------------
    def test_console_requires_login(self):
        r = self.client.get(reverse("manage_organizations"))
        self.assertEqual(r.status_code, 302)

    def test_non_admin_roles_are_refused(self):
        for user in (self.official, self.observer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("manage_organizations")).status_code, 403)
            self.assertEqual(self.client.get(reverse("manage_people")).status_code, 403)

    def test_org_admin_console_is_scoped_to_subtree(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("manage_organizations"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual({o.name for o in r.context["organizations"]}, {"Uni", "Faculty"})

        r = self.client.get(reverse("manage_people"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            {m.organization for m in r.context["memberships"]}, {self.org}
        )

    def test_outsider_sees_only_their_own_tenant(self):
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("manage_organizations"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual({o.name for o in r.context["organizations"]}, {"Other"})

    # -- create / update / toggle ----------------------------------------
    def test_only_system_admin_may_create_organization(self):
        payload = {
            "action": "create",
            "name": "New Community",
            "kind": "community",
            "parent": "",
            "description": "",
            "is_active": "on",
        }
        self.client.force_login(self.admin)
        r = self.client.post(reverse("manage_organizations"), payload)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Organization.objects.filter(name="New Community").exists())

        self.client.force_login(self.superuser)
        r = self.client.post(reverse("manage_organizations"), payload)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Organization.objects.filter(name="New Community").exists())

    def test_org_admin_updates_subtree_but_not_other_tenant(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("manage_organizations"),
            {
                "action": "update",
                "organization": str(self.child.pk),
                "name": "Renamed Faculty",
                "kind": "faculty",
                "parent": str(self.org.pk),
                "description": "",
                "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.child.refresh_from_db()
        self.assertEqual(self.child.name, "Renamed Faculty")

        # another tenant is out of scope -> 404, never modified
        r = self.client.post(
            reverse("manage_organizations"),
            {
                "action": "update",
                "organization": str(self.other_org.pk),
                "name": "Hijacked",
                "kind": "community",
                "parent": "",
                "description": "",
                "is_active": "on",
            },
        )
        self.assertEqual(r.status_code, 404)
        self.other_org.refresh_from_db()
        self.assertEqual(self.other_org.name, "Other")

    def test_toggle_activation(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("manage_organizations"),
            {"action": "toggle", "organization": str(self.org.pk)},
        )
        self.assertEqual(r.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)

    # -- people & access --------------------------------------------------
    def test_grant_console_role_grants_staff_flag(self):
        self.client.force_login(self.superuser)
        new = User.objects.create_user("newperson", password="x")
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(new.pk),
                "organization": str(self.org.pk),
                "role": "election_official",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=new, organization=self.org, role="election_official"
            ).exists()
        )
        new.refresh_from_db()
        self.assertTrue(new.is_staff)

    def test_grant_observer_does_not_grant_staff(self):
        self.client.force_login(self.superuser)
        new = User.objects.create_user("watcher", password="x")
        self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(new.pk),
                "organization": str(self.org.pk),
                "role": "observer",
            },
        )
        new.refresh_from_db()
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=new, organization=self.org, role="observer"
            ).exists()
        )
        self.assertFalse(new.is_staff)

    def test_grant_then_revoke_keeps_staff_flag_in_sync(self):
        self.client.force_login(self.superuser)
        target = User.objects.create_user("temp", password="x")
        self.client.post(
            reverse("manage_people"),
            {
                "action": "grant",
                "user": str(target.pk),
                "organization": str(self.org.pk),
                "role": "auditor",
            },
        )
        target.refresh_from_db()
        self.assertTrue(target.is_staff)

        membership = OrganizationMembership.objects.get(
            user=target, organization=self.org, role="auditor"
        )
        self.client.post(
            reverse("manage_people"),
            {"action": "revoke", "membership": str(membership.pk)},
        )
        self.assertFalse(OrganizationMembership.objects.filter(pk=membership.pk).exists())
        target.refresh_from_db()
        self.assertFalse(target.is_staff)

    def test_org_admin_cannot_grant_outside_scope(self):
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
        self.assertEqual(r.status_code, 302)
        self.assertFalse(
            OrganizationMembership.objects.filter(
                user=self.outsider, organization=self.other_org, role="observer"
            ).exists()
        )

    def test_create_user_with_role(self):
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("manage_people"),
            {
                "action": "create_user",
                "username": "brandnew",
                "password1": "V3ry-Str0ng-Passw0rd!",
                "password2": "V3ry-Str0ng-Passw0rd!",
                "first_name": "Brand",
                "last_name": "New",
                "email": "brand@example.org",
                "organization": str(self.org.pk),
                "role": "election_official",
            },
        )
        self.assertEqual(r.status_code, 302)
        user = User.objects.get(username="brandnew")
        self.assertTrue(user.check_password("V3ry-Str0ng-Passw0rd!"))
        self.assertTrue(user.is_staff)
        self.assertTrue(
            OrganizationMembership.objects.filter(
                user=user, organization=self.org, role="election_official"
            ).exists()
        )
