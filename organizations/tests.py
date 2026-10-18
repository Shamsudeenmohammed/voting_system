"""Tenancy isolation and role-predicate tests.

These are the rules every view and service relies on: a user may only reach
election data through a membership in that election's organization (or its
root), and the four roles have strictly different powers.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
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
        self.assertEqual(
            set(organizations_for(self.superuser)),
            {self.org, self.child, self.other_org},
        )

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
