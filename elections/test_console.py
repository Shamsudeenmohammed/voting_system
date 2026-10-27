"""Tests for the role-aware navigation, the cross-election console and the
election workspace.

These exercise the *product* surfaces added on top of the existing HTTP tests in
``elections.test_views``: what each role is offered in the sidebar, what the
"needs attention" / approvals / results / audit / settings pages expose, and
that the workspace renders every piece of state the operator needs to progress
an election.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from organizations.navigation import build_navigation

from .lifecycle import request_approval, transition
from .models import Candidate, Election, Position
from .tests import BaseVotingTestCase

User = get_user_model()


def _nav_for(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return build_navigation(request)


def _group_keys(user, path="/"):
    return [g["key"] for g in _nav_for(user, path)]


def _labels(group):
    return [i["label"] for i in group["items"]]


class NavigationTests(BaseVotingTestCase):
    def test_anonymous_has_no_navigation(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        self.assertEqual(build_navigation(request), [])

    def test_official_navigation(self):
        groups = _nav_for(self.official, "/dashboard/")
        keys = [g["key"] for g in groups]
        self.assertIn("admin_setup", keys)
        self.assertIn("results_audit", keys)

        by_key = {g["key"]: g for g in groups}
        self.assertIn("Dashboard", _labels(by_key["admin_setup"]))
        self.assertIn("New election", _labels(by_key["admin_setup"]))
        # officials raise approval requests -> approvals inbox is offered
        self.assertIn("Approvals", _labels(by_key["results_audit"]))

    def test_polling_station_links_have_no_duplicate_destinations(self):
        by_key = {g["key"]: g for g in _nav_for(self.official, "/dashboard/")}
        voting = by_key["voting"]
        self.assertEqual(_labels(voting), ["Voting kiosk"])
        self.assertEqual(
            [item["url_name"] for item in voting["items"]],
            ["workstation-voting"],
        )

        login_items = [
            item
            for group in by_key.values()
            for item in group["items"]
            if item["url_name"] == "workstation-login"
        ]
        self.assertEqual(len(login_items), 1)
        self.assertEqual(login_items[0]["label"], "Workstation login")

    def test_elections_group_has_no_duplicate_dashboard_link(self):
        by_key = {g["key"]: g for g in _nav_for(self.official, "/dashboard/")}
        admin_labels = _labels(by_key["admin_setup"])
        self.assertEqual(admin_labels.count("All elections"), 0)
        # dashboard lives only under admin setup
        admin_labels = _labels(by_key["admin_setup"])
        self.assertEqual(admin_labels.count("Dashboard"), 1)

    def test_auditor_is_read_only_without_management_links(self):
        by_key = {g["key"]: g for g in _nav_for(self.auditor, "/dashboard/")}
        self.assertIn("Dashboard", _labels(by_key["admin_setup"]))
        self.assertNotIn("New election", _labels(by_key.get("admin_setup", {"items": []})))
        self.assertNotIn("approvals", by_key)
        self.assertIn("Results & reports", _labels(by_key["results_audit"]))
        self.assertIn("Audit overview", _labels(by_key["results_audit"]))

    def test_org_admin_gets_organisations_group(self):
        keys = _group_keys(self.admin, "/manage/organizations/")
        self.assertIn("admin_setup", keys)

    def test_system_admin_gets_settings_group(self):
        superuser = User.objects.create_superuser("root", "root@example.org", "x")
        keys = _group_keys(superuser, "/dashboard/settings/")
        self.assertIn("admin_setup", keys)

    def test_active_group_is_marked_and_expanded(self):
        groups = _nav_for(self.official, "/dashboard/operations/")
        operations = next(g for g in groups if g["key"] == "results_audit")
        self.assertTrue(operations["active"])
        self.assertTrue(operations["open"])
        active_items = [i["label"] for i in operations["items"] if i["active"]]
        self.assertEqual(active_items, ["Needs attention"])


class ConsoleViewTests(BaseVotingTestCase):
    def test_operations_requires_staff_login(self):
        r = self.client.get(reverse("operations"))
        self.assertRedirects(
            r, reverse("admin:login") + "?next=/dashboard/operations/", fetch_redirect_response=False
        )

    def test_operations_is_scoped_and_lists_attention(self):
        # a draft with no positions has blocking readiness problems
        self.make_election(title="Bare Draft")
        self.client.force_login(self.official)
        r = self.client.get(reverse("operations"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Bare Draft")
        self.assertGreaterEqual(r.context["blocking_total"], 1)

    def test_operations_excludes_other_tenants(self):
        self.make_election(organization=self.other_org, title="Other Tenancy")
        self.client.force_login(self.official)
        r = self.client.get(reverse("operations"))
        self.assertNotContains(r, "Other Tenancy")

    def test_approvals_inbox_separates_decidable_and_awaiting(self):
        election = self.make_election(title="Review Me")
        self.make_ballot_structure(election)
        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        election = transition(election, Election.STATE_CONFIG_REVIEW, actor=self.official)
        request_approval(election, "approve_config", actor=self.admin, reason="Reviewed.")

        # a *different* approver sees it as decidable
        self.client.force_login(self.admin2)
        r = self.client.get(reverse("approvals_inbox"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["decidable"]), 1)
        self.assertEqual(len(r.context["awaiting"]), 0)

        # the requester sees it as awaiting, never decidable
        self.client.force_login(self.admin)
        r = self.client.get(reverse("approvals_inbox"))
        self.assertEqual(len(r.context["decidable"]), 0)
        self.assertEqual(len(r.context["awaiting"]), 1)

    def test_approvals_inbox_forbidden_to_non_participant(self):
        self.make_election()
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("approvals_inbox"))
        self.assertEqual(r.status_code, 403)

    def test_results_index_renders_for_privileged_roles(self):
        self.open_election(with_voters=1)
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("results_index"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["rows"]), 1)

    def test_audit_overview_reports_integrity(self):
        self.open_election(with_voters=1)
        self.client.force_login(self.official)
        r = self.client.get(reverse("audit_overview"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["broken_total"], 0)
        self.assertTrue(all(row["chain"]["ok"] for row in r.context["rows"]))

    def test_settings_overview_is_system_admin_only(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("settings_overview"))
        self.assertEqual(r.status_code, 403)

        superuser = User.objects.create_superuser("root", "root@example.org", "x")
        self.client.force_login(superuser)
        r = self.client.get(reverse("settings_overview"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("signing_algorithm", r.context["config"])


class WorkspaceTests(BaseVotingTestCase):
    def _draft(self):
        election = self.make_election(title="Workspace Draft")
        self.make_ballot_structure(election)
        return election

    def test_workspace_renders_readiness_and_actions_for_draft(self):
        election = self._draft()
        self.client.force_login(self.official)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Readiness")
        self.assertContains(r, "Open registration")
        # the recommended action is flagged
        self.assertContains(r, "recommended")

    def test_workspace_flags_blocking_when_setup_incomplete(self):
        election = self.make_election(title="Empty Draft")
        self.client.force_login(self.official)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["readiness"]["ready"])
        self.assertContains(r, "blocking issue")

    def test_workspace_offers_dual_control_request_in_review(self):
        election = self._draft()
        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        election = transition(election, Election.STATE_CONFIG_REVIEW, actor=self.official)
        self.client.force_login(self.admin)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertEqual(r.status_code, 200)
        kinds = [t["dual"] for t in r.context["dual_transitions"]]
        self.assertIn("approve_config", kinds)
        self.assertContains(r, "request approval")

    def test_workspace_forbidden_to_other_tenancy(self):
        election = self._draft()
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertEqual(r.status_code, 403)

    def test_workspace_hides_structure_editors_after_draft(self):
        election = self._draft()
        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        self.client.force_login(self.official)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertFalse(r.context["can_edit_structure"])
        self.assertNotContains(r, "Add position")


class AvailableTransitionsTests(BaseVotingTestCase):
    def test_edge_action_names_are_exposed(self):
        from .views import available_transitions

        election = self.make_election()
        actions = {t["action"] for t in available_transitions(election)}
        self.assertIn("submit", actions)
        self.assertIn("archive", actions)

        transition(election, Election.STATE_REGISTRATION, actor=self.official)
        election.refresh_from_db()
        actions = {t["action"] for t in available_transitions(election)}
        self.assertIn("request_review", actions)
        self.assertIn("return_draft", actions)
