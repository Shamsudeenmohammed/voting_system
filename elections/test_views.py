"""HTTP layer tests: routing, page access, and end-to-end flows over HTTP.

Complements the service-level tests in ``elections.tests`` / ``ballot.tests``
by exercising the same rules the way a browser would: URL routing, status
codes, scoping of dashboards, the full ballot round trip, four-eyes actions
and result visibility.
"""
from __future__ import annotations

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from django.utils.html import escape

from audit.models import AuditCheckpoint
from ballot.models import Ballot
from ballot.services import cast_ballot, issue_authorization, prepare_ballot

from .lifecycle import transition
from .models import Election, ElectionApproval
from .tests import BaseVotingTestCase


class PublicPageTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        self.draft = self.make_election(title="Draft Election")

    def test_home_lists_only_public_elections(self):
        r = self.client.get(reverse("home"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, escape(self.election.title))
        self.assertNotContains(r, "Draft Election")

    def test_detail_is_public_for_open_election(self):
        r = self.client.get(reverse("election_detail", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Alice A.")

    def test_detail_is_404_for_draft_election_to_anonymous(self):
        r = self.client.get(reverse("election_detail", args=[self.draft.pk]))
        self.assertEqual(r.status_code, 404)

    def test_detail_forbidden_for_other_tenancy(self):
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("election_detail", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)

    def test_dashboard_requires_staff_login(self):
        r = self.client.get(reverse("dashboard"))
        self.assertRedirects(
            r,
            reverse("admin:login") + "?next=/dashboard/",
            fetch_redirect_response=False,
        )

    def test_dashboard_is_scoped_to_own_organization(self):
        self.make_election(organization=self.other_org, title="Other Tenancy Election")
        self.client.force_login(self.official)
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, escape(self.election.title))
        self.assertNotContains(r, "Other Tenancy Election")

    def test_audit_page_scoped(self):
        self.client.force_login(self.outsider)
        r = self.client.get(reverse("election_audit", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.official)
        r = self.client.get(reverse("election_audit", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["chain"]["ok"])

    def test_checkpoint_restricted_to_approvers(self):
        self.client.force_login(self.official)
        r = self.client.post(reverse("audit_checkpoint", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(AuditCheckpoint.objects.exists())
        self.client.force_login(self.admin)
        r = self.client.post(reverse("audit_checkpoint", args=[self.election.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(AuditCheckpoint.objects.count(), 1)


class BallotFlowHttpTests(BaseVotingTestCase):
    def setUp(self):
        # the view-level throttle counts per IP in the shared locmem cache
        cache.clear()
        self.election, self.position, self.candidates = self.open_election(with_voters=3)

    def test_full_ballot_round_trip(self):
        voter = self.election.voters.first()
        _auth, token = issue_authorization(voter)
        url = reverse("ballot_prepare", args=[token])

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        submission_id = r.context["submission_id"]

        payload = {
            "submission_id": submission_id,
            str(self.position.pk): [str(self.candidates[0].pk)],
        }
        r = self.client.post(url, payload)
        self.assertEqual(r.status_code, 200)
        receipt = r.context["receipt_code"]
        self.assertTrue(receipt)

        # lost response -> identical retry stays idempotent
        r = self.client.post(url, payload)
        self.assertEqual(r.context["receipt_code"], receipt)
        self.assertTrue(r.context["idempotent"])
        self.assertEqual(Ballot.objects.filter(election=self.election).count(), 1)

        # public receipt lookup: existence + hash, never voter identity
        r = self.client.get(
            reverse("receipt_lookup", args=[self.election.pk]), {"code": receipt}
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.context["found"])
        self.assertEqual(r.context["found"]["receipt_code"], receipt)
        self.assertNotContains(r, "V001")

    def test_unknown_and_used_tokens_share_one_denial(self):
        # unknown token: uniform refusal, never a 404 (existence oracle)
        r = self.client.get("/ballot/totally-made-up-token/")
        self.assertEqual(r.status_code, 403)
        self.assertContains(
            r,
            "This voting credential is not valid or has already been used.",
            status_code=403,
        )
        # consumed token: same status and same message
        voter = self.election.voters.first()
        _auth, token = issue_authorization(voter)
        url = reverse("ballot_prepare", args=[token])
        form = self.client.get(url)
        self.client.post(
            url,
            {
                "submission_id": form.context["submission_id"],
                str(self.position.pk): [str(self.candidates[0].pk)],
            },
        )
        r = self.client.get(url)
        self.assertEqual(r.status_code, 403)
        self.assertContains(
            r,
            "This voting credential is not valid or has already been used.",
            status_code=403,
        )

    def test_voter_access_point_issues_single_use_credential(self):
        url = reverse("voter_access", args=[self.election.pk])
        r = self.client.post(url, {"voter_id": "V001"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["issued"])
        self.assertTrue(r.context["token"])
        # unknown voter id: same page shape, no token
        r = self.client.post(url, {"voter_id": "NOPE"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context["issued"])


class LifecycleHttpTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=1)
        self.election = transition(self.election, Election.STATE_CLOSED, actor=self.official)

    def test_direct_reopen_is_refused_with_four_eyes_message(self):
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("election_action", args=[self.election.pk]), {"action": "open"}
        )
        self.assertRedirects(
            r,
            reverse("election_config", args=[self.election.pk]),
            fetch_redirect_response=False,
        )
        msgs = [str(m) for m in get_messages(r.wsgi_request)]
        self.assertTrue(any("four-eyes" in m for m in msgs), msgs)
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_CLOSED)

    def test_reopen_flows_through_two_different_users(self):
        # official requests the reopen with a documented reason
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("approval_request", args=[self.election.pk]),
            {"kind": "reopen", "reason": "Ballot box replaced after power cut."},
        )
        self.assertEqual(r.status_code, 302)
        approval = ElectionApproval.objects.get(election=self.election, kind="reopen")

        # the requester may not decide their own request
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_CLOSED)

        # a different org admin decides -> the reopen is applied
        self.client.force_login(self.admin2)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_OPEN)
        self.assertEqual(self.election.reopened_count, 1)

    def test_other_tenancy_cannot_trigger_actions(self):
        self.client.force_login(self.outsider)
        r = self.client.post(
            reverse("election_action", args=[self.election.pk]), {"action": "close"}
        )
        self.assertEqual(r.status_code, 403)
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_CLOSED)

    def test_unknown_action_is_rejected(self):
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("election_action", args=[self.election.pk]), {"action": "detonate"}
        )
        self.assertEqual(r.status_code, 302)
        msgs = [str(m) for m in get_messages(r.wsgi_request)]
        self.assertIn("Unknown action.", msgs)


class ResultsVisibilityHttpTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=1)
        voter = self.election.voters.first()
        issue_authorization(voter)
        cast_ballot(
            election=self.election,
            voter=voter,
            submission_id=prepare_ballot(voter, self.election).submission_id,
            selections={str(self.position.pk): [str(self.candidates[0].pk)]},
        )
        for state, actor in (
            (Election.STATE_CLOSED, self.official),
            (Election.STATE_RECONCILIATION, self.official),
            (Election.STATE_REVIEW, self.official),
        ):
            self.election = transition(self.election, state, actor=actor)

    def _publish(self):
        self.election = transition(self.election, Election.STATE_CERTIFIED, actor=self.admin)
        self.election = transition(self.election, Election.STATE_PUBLISHED, actor=self.admin)

    def test_results_hidden_before_publication(self):
        r = self.client.get(reverse("results", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context["visible"])
        self.assertIsNone(r.context["snapshot"])
        self.assertNotContains(r, "Alice A.")

        # interim snapshot page refuses the public
        r = self.client.get(reverse("snapshot_detail", args=[self.election.pk, 1]))
        self.assertEqual(r.status_code, 403)
        r = self.client.get(reverse("export_snapshot", args=[self.election.pk, 1]))
        self.assertEqual(r.status_code, 403)

        # ... but an auditor may review them early
        self.client.force_login(self.auditor)
        r = self.client.get(reverse("results", args=[self.election.pk]))
        self.assertTrue(r.context["visible"])
        self.assertContains(r, "Alice A.")

    def test_results_public_after_publication(self):
        self._publish()
        r = self.client.get(reverse("results", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["visible"])
        self.assertContains(r, "Alice A.")

        r = self.client.get(reverse("snapshot_detail", args=[self.election.pk, 1]))
        self.assertEqual(r.status_code, 200)

        r = self.client.get(reverse("export_snapshot", args=[self.election.pk, 1]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])

    def test_recount_requires_manage_rights_and_closed_state(self):
        # other tenancy refused
        self.client.force_login(self.outsider)
        r = self.client.post(reverse("recount", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)

        # official of the election may request a recount after closing
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("recount", args=[self.election.pk]), {"reason": "judge order"}
        )
        self.assertEqual(r.status_code, 302)
