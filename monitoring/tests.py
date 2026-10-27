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
from elections.models import Election
from elections.tests import BaseVotingTestCase
from organizations.models import OrganizationMembership
from representatives.models import Representative

User = get_user_model()


class MonitoringEndpointTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        voter = self.election.voters.first()
        issue_authorization(voter, allow_reissue=True, reason="test")
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

    # -- live results board -----------------------------------------------
    def test_live_board_staff_sees_candidate_totals(self):
        self.client.force_login(self.official)
        r = self.client.get(reverse("live_board", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Alice A.")
        self.assertContains(r, 'id="resultsBoard"')
        self.assertContains(r, 'id="boardSeed"')

        board = r.context["board"]
        self.assertEqual(board["totals"]["ballots_accepted"], 1)
        self.assertEqual(board["totals"]["positions"], 1)
        self.assertEqual(board["state_label"], "Voting open")
        self.assertTrue(board["health"]["reconciliation_ok"])
        self.assertEqual(board["health"]["status"], "healthy")

        position = board["positions"][0]
        self.assertEqual(position["title"], "President")
        self.assertEqual(position["total_votes"], 1)
        alice = position["candidates"][0]
        self.assertEqual(alice["name"], "Alice A.")
        self.assertEqual(alice["votes"], 1)
        self.assertEqual(alice["percent"], 100.0)
        self.assertEqual(alice["rank"], 1)
        self.assertTrue(alice["leading"])
        self.assertFalse(alice["tied"])

        body = r.content.decode()
        self.assertNotIn(self.cast.receipt_code, body)
        self.assertNotIn("selections", body)
        self.assertNotIn("V001", body)

    def test_live_board_data_is_aggregate_only_and_refreshes(self):
        self.client.force_login(self.official)
        first_response = self.client.get(
            reverse("live_board_data", args=[self.election.pk])
        )
        self.assertEqual(first_response.status_code, 200)
        first = first_response.json()
        self.assertEqual(first["totals"]["ballots_accepted"], 1)
        self.assertIn("Alice A.", first_response.content.decode())
        self.assertEqual(
            set(first["health"]),
            {
                "status",
                "label",
                "detail",
                "reconciliation_ok",
                "ballots_invalid",
                "box_seq",
                "count_ms",
                "last_ballot_at",
                "last_ballot_label",
                "generated_at",
            },
        )
        body = first_response.content.decode()
        self.assertNotIn(self.cast.receipt_code, body)
        self.assertNotIn("selections", body)
        self.assertNotIn("submission", body)
        self.assertNotIn("V001", body)

        # The cache is keyed by ballot-box sequence, so a new ballot is visible
        # on the very next poll rather than after the cache TTL.
        voter = self.election.voters.filter(has_voted=False).first()
        issue_authorization(voter, allow_reissue=True, reason="live board refresh test")
        cast_ballot(
            election=self.election,
            voter=voter,
            submission_id=prepare_ballot(voter, self.election).submission_id,
            selections={str(self.position.pk): [str(self.candidates[1].pk)]},
        )
        second = self.client.get(
            reverse("live_board_data", args=[self.election.pk])
        ).json()
        self.assertEqual(second["totals"]["ballots_accepted"], 2)
        self.assertEqual(second["health"]["box_seq"], 2)
        by_name = {
            candidate["name"]: candidate
            for candidate in second["positions"][0]["candidates"]
        }
        self.assertEqual(by_name["Alice A."]["votes"], 1)
        self.assertEqual(by_name["Bob B."]["votes"], 1)
        self.assertEqual(by_name["Alice A."]["percent"], 50.0)
        self.assertEqual(by_name["Bob B."]["percent"], 50.0)
        self.assertTrue(by_name["Alice A."]["leading"])
        self.assertTrue(by_name["Bob B."]["leading"])
        self.assertTrue(by_name["Alice A."]["tied"])
        self.assertTrue(by_name["Bob B."]["tied"])

    def test_live_board_follows_results_visibility_policy(self):
        # Default policy is after-close: non-staff cannot see candidate totals
        # while the election is open.
        for user in (self.rep_user, self.observer):
            self.client.force_login(user)
            r = self.client.get(reverse("live_board", args=[self.election.pk]))
            self.assertEqual(r.status_code, 403)
            r = self.client.get(
                reverse("live_board_data", args=[self.election.pk])
            )
            self.assertEqual(r.status_code, 403)

        self.election.results_visibility = Election.VISIBILITY_LIVE
        self.election.save(update_fields=["results_visibility"])

        for user in (self.rep_user, self.observer):
            self.client.force_login(user)
            r = self.client.get(reverse("live_board", args=[self.election.pk]))
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                self.client.get(
                    reverse("live_board_data", args=[self.election.pk])
                ).status_code,
                200,
            )

    def test_live_board_available_after_close_to_approved_rep(self):
        self.election.state = Election.STATE_CLOSED
        self.election.save(update_fields=["state"])
        self.client.force_login(self.rep_user)
        self.assertEqual(
            self.client.get(
                reverse("live_board", args=[self.election.pk])
            ).status_code,
            200,
        )

        self.election.results_visibility = Election.VISIBILITY_PRIVATE
        self.election.save(update_fields=["results_visibility"])
        self.assertEqual(
            self.client.get(
                reverse("live_board", args=[self.election.pk])
            ).status_code,
            403,
        )

    def test_live_board_denied_to_outsiders(self):
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.get(
                reverse("live_board", args=[self.election.pk])
            ).status_code,
            403,
        )
        self.client.logout()
        self.assertEqual(
            self.client.get(
                reverse("live_board_data", args=[self.election.pk])
            ).status_code,
            403,
        )

    def test_monitor_links_to_board_for_available_users(self):
        self.client.force_login(self.official)
        monitor = self.client.get(reverse("monitor", args=[self.election.pk]))
        self.assertContains(
            monitor, reverse("live_board", args=[self.election.pk])
        )
        self.assertTrue(monitor.context["board_available"])

        # The representative's monitor remains accessible, but the board link
        # is hidden until the visibility policy permits candidate totals.
        self.client.force_login(self.rep_user)
        monitor = self.client.get(reverse("monitor", args=[self.election.pk]))
        self.assertEqual(monitor.status_code, 200)
        self.assertNotContains(
            monitor, reverse("live_board", args=[self.election.pk])
        )
        self.assertFalse(monitor.context["board_available"])

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
