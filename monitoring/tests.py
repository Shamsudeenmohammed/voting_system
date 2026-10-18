"""Monitoring endpoint tests: aggregate-only payloads and access rules.

Every response in this module must stay aggregate: no ballot contents, no
receipt codes, no voter identities - and turnout is withheld from
non-privileged roles while the election is still open.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.html import escape

from ballot.services import cast_ballot, issue_authorization, prepare_ballot
from elections.tests import BaseVotingTestCase
from organizations.models import OrganizationMembership
from representatives.models import Representative

User = get_user_model()


class MonitoringEndpointTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        voter = self.election.voters.first()
        issue_authorization(voter)
        self.cast = cast_ballot(
            election=self.election,
            voter=voter,
            submission_id=prepare_ballot(voter, self.election).submission_id,
            selections={str(self.position.pk): [str(self.candidates[0].pk)]},
        )
        self.rep_user = User.objects.create_user("repmon", password="x")
        self.rep = Representative.objects.create(
            election=self.election,
            candidate=self.candidates[0],
            user=self.rep_user,
            full_name="Rep Mon",
            email="repmon@example.org",
            status=Representative.STATUS_APPROVED,
        )
        self.observer = User.objects.create_user("observer", password="x")
        OrganizationMembership.objects.create(
            user=self.observer, organization=self.org, role="observer"
        )

    # -- live status ------------------------------------------------------
    def test_live_status_returns_aggregates_only(self):
        self.client.force_login(self.official)
        r = self.client.get(reverse("live_status", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["turnout"]["total"], 3)
        self.assertEqual(data["turnout"]["voted"], 1)
        self.assertEqual(data["ballot_box"]["last_seq"], 1)
        self.assertEqual(data["state"], self.election.state)
        body = r.content.decode()
        self.assertNotIn(self.cast.receipt_code, body)
        self.assertNotIn("selections", body)
        self.assertNotIn("V001", body)
        # exactly aggregate keys - nothing identity-shaped
        self.assertEqual(
            set(data),
            {
                "election",
                "title",
                "state",
                "now",
                "start_at",
                "end_at",
                "turnout",
                "ballot_box",
                "stations",
                "incidents_open",
                "representatives",
            },
        )

    def test_live_status_access_denied(self):
        for user in (self.outsider, None):
            if user is None:
                self.client.logout()
            else:
                self.client.force_login(user)
            r = self.client.get(reverse("live_status", args=[self.election.pk]))
            self.assertEqual(r.status_code, 403)

    def test_turnout_withheld_from_rep_while_open(self):
        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("live_status", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["turnout"]["voted"])

    def test_turnout_withheld_from_observer_while_open(self):
        self.client.force_login(self.observer)
        r = self.client.get(reverse("live_status", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["turnout"]["voted"])

    # -- monitoring page --------------------------------------------------
    def test_monitor_page_access(self):
        # authorized representative of the election may watch the monitor
        self.client.force_login(self.rep_user)
        self.assertEqual(
            self.client.get(reverse("monitor", args=[self.election.pk])).status_code, 200
        )
        # outsider organization may not
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.get(reverse("monitor", args=[self.election.pk])).status_code, 403
        )

    # -- audit export -----------------------------------------------------
    def test_audit_export_for_privileged_roles(self):
        for user in (self.official, self.auditor, self.admin):
            self.client.force_login(user)
            r = self.client.get(reverse("audit_export", args=[self.election.pk]))
            self.assertEqual(r.status_code, 200, msg=str(user))
            body = r.content.decode()
            data = r.json()
            self.assertEqual(data["chain_key"], f"election:{self.election.pk}")
            self.assertGreater(data["head"]["seq"], 0)
            # never ballot contents or receipts
            self.assertNotIn(self.cast.receipt_code, body)
            self.assertNotIn("selections", body)

    def test_audit_export_denied_outside_privileged_roles(self):
        cases = {
            "rep": self.rep_user,
            "observer": self.observer,
            "outsider": self.outsider,
        }
        for label, user in cases.items():
            self.client.force_login(user)
            r = self.client.get(reverse("audit_export", args=[self.election.pk]))
            self.assertEqual(r.status_code, 403, msg=label)
        self.client.logout()
        r = self.client.get(reverse("audit_export", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)

    # -- org dashboard ----------------------------------------------------
    def test_org_dashboard_scoped_to_own_orgs(self):
        self.client.force_login(self.official)
        r = self.client.get(reverse("org_dashboard") + f"?org={self.org.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, escape(self.election.title))

        # an outsider may use the page but only for their own organization
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("org_dashboard") + f"?org={self.org.pk}")
        self.assertEqual(r.status_code, 404)
        r = self.client.get(reverse("org_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, escape(self.election.title))

        # anonymous users are refused outright
        self.client.logout()
        r = self.client.get(reverse("org_dashboard"))
        self.assertEqual(r.status_code, 403)
