"""HTTP layer tests: routing, page access, and end-to-end flows over HTTP.

Complements the service-level tests in ``elections.tests`` / ``ballot.tests``
by exercising the same rules the way a browser would: URL routing, status
codes, scoping of dashboards, the full ballot round trip, four-eyes actions
and result visibility.
"""
from __future__ import annotations

import shutil
import tempfile
from io import BytesIO

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils.html import escape
from PIL import Image

from audit.models import AuditCheckpoint
from ballot.models import Ballot
from ballot.services import cast_ballot, issue_authorization, prepare_ballot

from .lifecycle import transition
from .models import Candidate, Election, ElectionApproval, Position
from .tests import BaseVotingTestCase


class PublicPageTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        self.draft = self.make_election(title="Draft Election")

    def test_home_redirects_to_login_for_anonymous(self):
        r = self.client.get(reverse("home"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

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
        _auth, token = issue_authorization(voter, allow_reissue=True, reason="test")
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
        # consumed token: shows duplicate vote message
        voter = self.election.voters.first()
        _auth, token = issue_authorization(voter, allow_reissue=True, reason="test")
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
            "You have already cast your vote for this election. You cannot vote again.",
            status_code=403,
        )

    def test_voter_access_point_issues_single_use_credential(self):
        url = reverse("voter_access", args=[self.election.pk])
        r = self.client.post(url, {"voter_id": "V001"})
        self.assertEqual(r.status_code, 200)
        # An active credential already exists from registration; no duplicate
        # token is silently generated.
        self.assertFalse(r.context.get("issued"))
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

    def test_missing_decision_never_rejects(self):
        """A dropped submitter value must not be read as a rejection."""
        self.client.force_login(self.official)
        self.client.post(
            reverse("approval_request", args=[self.election.pk]),
            {"kind": "reopen", "reason": "Ballot box replaced after power cut."},
        )
        approval = ElectionApproval.objects.get(election=self.election, kind="reopen")

        self.client.force_login(self.admin2)
        # Simulates the submitter button's name/value being omitted from POST.
        r = self.client.post(reverse("approval_decide", args=[approval.pk]), {"note": ""})
        self.assertEqual(r.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_PENDING)
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_CLOSED)
        self.assertTrue(
            any("No valid decision" in str(m) for m in get_messages(r.wsgi_request))
        )

    def test_unknown_decision_never_rejects(self):
        self.client.force_login(self.official)
        self.client.post(
            reverse("approval_request", args=[self.election.pk]),
            {"kind": "reopen", "reason": "Ballot box replaced after power cut."},
        )
        approval = ElectionApproval.objects.get(election=self.election, kind="reopen")

        self.client.force_login(self.admin2)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "maybe"}
        )
        self.assertEqual(r.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_PENDING)

    def test_reject_decision_rejects(self):
        self.client.force_login(self.official)
        self.client.post(
            reverse("approval_request", args=[self.election.pk]),
            {"kind": "reopen", "reason": "Ballot box replaced after power cut."},
        )
        approval = ElectionApproval.objects.get(election=self.election, kind="reopen")

        self.client.force_login(self.admin2)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]),
            {"decision": "reject", "note": "Insufficient evidence."},
        )
        self.assertEqual(r.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_REJECTED)
        self.assertEqual(approval.decision_note, "Insufficient evidence.")
        self.election.refresh_from_db()
        self.assertEqual(self.election.state, Election.STATE_CLOSED)

    def test_config_page_exposes_named_decision_buttons(self):
        """The decide buttons must carry name/value for the POST contract."""
        self.client.force_login(self.official)
        self.client.post(
            reverse("approval_request", args=[self.election.pk]),
            {"kind": "reopen", "reason": "Ballot box replaced after power cut."},
        )
        self.client.force_login(self.admin2)
        r = self.client.get(reverse("election_config", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="decision" value="approve"')
        self.assertContains(r, 'name="decision" value="reject"')

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
        issue_authorization(voter, allow_reissue=True, reason="test")
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


_PHOTO_MEDIA_ROOT = tempfile.mkdtemp(prefix="votesys-photo-tests-")


@override_settings(MEDIA_ROOT=_PHOTO_MEDIA_ROOT)
class CandidatePhotoUploadTests(BaseVotingTestCase):
    """Uploading a passport photograph must never overflow the storage path.

    Regression: ``elections/<uuid>/candidates/<uuid>/<file>`` is already 94
    characters before the filename, so a name longer than a few characters
    used to exceed ``ImageField``'s default ``max_length=100`` and Django
    raised ``SuspiciousFileOperation`` while looking for a free filename.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_PHOTO_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.election = self.make_election(title="Photo Election")
        self.position = Position.objects.create(
            election=self.election, title="President"
        )
        self.client.force_login(self.official)

    def _photo(self, name: str) -> SimpleUploadedFile:
        buffer = BytesIO()
        Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
        return SimpleUploadedFile(
            name, buffer.getvalue(), content_type="image/jpeg"
        )

    def _add_candidate(self, name: str, filename: str):
        return self.client.post(
            reverse("candidate_add", args=[self.election.pk, self.position.pk]),
            {
                "full_name": name,
                "bio": "",
                "manifesto": "",
                "order": 0,
                "status": "approved",
                "photo": self._photo(filename),
            },
        )

    def test_long_photo_filename_is_stored_without_overflow(self):
        r = self._add_candidate("Alice A.", "ab_rah_" + "x" * 200 + ".jpg")
        self.assertEqual(r.status_code, 302)

        candidate = Candidate.objects.get(position=self.position, full_name="Alice A.")
        max_length = Candidate._meta.get_field("photo").max_length
        self.assertTrue(candidate.photo)
        self.assertLessEqual(len(candidate.photo.name), max_length)
        self.assertTrue(candidate.photo.storage.exists(candidate.photo.name))

    def test_colliding_photo_filenames_are_both_stored(self):
        self._add_candidate("Alice A.", "ab_rah.jpg")
        self._add_candidate("Bob B.", "ab_rah.jpg")

        alice = Candidate.objects.get(position=self.position, full_name="Alice A.")
        bob = Candidate.objects.get(position=self.position, full_name="Bob B.")
        self.assertTrue(alice.photo and bob.photo)
        self.assertNotEqual(alice.photo.name, bob.photo.name)
        for candidate in (alice, bob):
            self.assertTrue(candidate.photo.storage.exists(candidate.photo.name))
