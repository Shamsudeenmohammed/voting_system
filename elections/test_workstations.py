from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ballot.models import BallotAuthorization
from elections.models import Election, PollingStation, Voter, Workstation
from elections.tests import BaseVotingTestCase

User = get_user_model()


class WorkstationTests(BaseVotingTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser("super", password="x")
        self.election, self.position, self.candidates = self.open_election(with_voters=2)
        self.voters = list(Voter.objects.filter(election=self.election))

    def _make_ws(self, purpose=Workstation.PURPOSE_VOTING, status=Workstation.STATUS_ACTIVE):
        ws = Workstation(
            name="WS1",
            code="WS1",
            election=self.election,
            purpose=purpose,
            status=status,
            created_by=self.superuser,
        )
        ws.set_credential("secret123")
        ws.save()
        return ws

    def test_authenticate_success(self):
        ws = self._make_ws()
        from elections.workstation_auth import authenticate_workstation

        self.assertEqual(authenticate_workstation("WS1", "secret123"), ws)
        self.assertIsNone(authenticate_workstation("WS1", "wrong"))

    def test_revoked_device_cannot_authenticate(self):
        ws = self._make_ws(status=Workstation.STATUS_REVOKED)
        from elections.workstation_auth import authenticate_workstation

        self.assertIsNone(authenticate_workstation("WS1", "secret123"))

    def test_purpose_enforcement_blocks_wrong_purpose(self):
        ws = self._make_ws(purpose=Workstation.PURPOSE_VERIFICATION)
        self.client.force_login(self.superuser)
        # establish workstation session via login view
        r = self.client.post(reverse("workstation-login"), {"code": "WS1", "credential": "secret123"})
        self.assertIn(r.status_code, (302, 200))
        # verification workstation should not access voting entry point
        r = self.client.get(reverse("workstation-voting"))
        self.assertIn(r.status_code, (302, 403))

    def test_unauthorized_url_access_redirects(self):
        r = self.client.get(reverse("workstation-voting"))
        self.assertIn(r.status_code, (302, 403))

    def _login_verification_workstation(self):
        self._make_ws(purpose=Workstation.PURPOSE_VERIFICATION)
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("workstation-login"),
            {"code": "WS1", "credential": "secret123"},
        )
        self.assertIn(response.status_code, (302, 200))
        return response

    def test_verification_page_uses_default_background(self):
        from django.contrib.staticfiles import finders

        self.assertIsNotNone(finders.find("img/workstation-background.svg"))
        self._login_verification_workstation()
        response = self.client.get(reverse("workstation-verification"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "workstation-background.svg")
        self.assertContains(response, "verify-card")

    def test_verification_background_is_configurable(self):
        self._login_verification_workstation()
        with self.settings(VERIFICATION_BACKGROUND_IMAGE="img/station-photo.jpg"):
            response = self.client.get(reverse("workstation-verification"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "img/station-photo.jpg")

    def test_verification_page_renders_issued_token(self):
        self._login_verification_workstation()
        voter = Voter.objects.create(
            election=self.election, voter_id="V900", full_name="New Voter"
        )
        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "issue_token": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertIsNotNone(result.token)
        self.assertContains(response, "Voting token")
        self.assertContains(response, result.token)

    def test_verification_result_is_not_duplicated_as_page_notice(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "issue_token": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"].status, "active_token")
        self.assertTrue(response.context["voter_eligible"])
        content = response.content.decode()
        self.assertEqual(
            content.count(
                "A valid voting token is already active for this voter. "
                "No duplicate token generated."
            ),
            1,
        )

    def test_token_issuance_and_duplicate_prevention(self):
        from ballot.services import issue_voter_token

        voter = self.voters[0]
        # At election open, each voter already received one token.
        res1 = issue_voter_token(voter, election=self.election, actor=self.official)
        self.assertIsNone(res1.token)
        self.assertEqual(res1.status, "active_token")

        # Explicit reissue (audited) produces a new token.
        res2 = issue_voter_token(
            voter, election=self.election, actor=self.official,
            allow_reissue=True, reason="lost token",
        )
        self.assertIsNotNone(res2.token)
        self.assertEqual(res2.status, "reissued")

        # Duplicate issuance is blocked.
        res3 = issue_voter_token(voter, election=self.election, actor=self.official)
        self.assertIsNone(res3.token)
        self.assertEqual(res3.status, "active_token")

        # After voting, issuance is refused.
        from ballot.models import BallotAuthorization

        BallotAuthorization.objects.filter(voter=voter, consumed_at__isnull=True).update(consumed_at=timezone.now() if False else res2.authorization.issued_at)
        Voter.objects.filter(pk=voter.pk).update(has_voted=True)
        res4 = issue_voter_token(voter, election=self.election, actor=self.official)
        self.assertIsNone(res4.token)
        self.assertEqual(res4.status, "already_voted")

    def _active_authorization(self, voter):
        return (
            BallotAuthorization.objects.filter(
                voter=voter, consumed_at__isnull=True, revoked_at__isnull=True
            )
            .order_by("-issued_at")
            .first()
        )

    def test_verification_lookup_shows_token_actions(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "lookup": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["verification_token"]["state"], "active")
        self.assertContains(response, "Token active")
        self.assertContains(response, 'name="retrieve_token"')
        self.assertContains(response, 'name="revoke_token"')

    def test_verification_lookup_shows_expired_token_actions(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        auth = self._active_authorization(voter)
        auth.expires_at = timezone.now() - timedelta(hours=1)
        auth.save(update_fields=["expires_at"])

        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "lookup": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["verification_token"]["state"], "expired")
        self.assertContains(response, "Token expired")
        self.assertContains(response, 'name="retrieve_token"')
        self.assertContains(response, 'name="revoke_token"')

    def test_verification_retrieve_replaces_active_token(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        old_auth = self._active_authorization(voter)

        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "retrieve_token": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual(result.status, "reissued")
        self.assertIsNotNone(result.token)
        old_auth.refresh_from_db()
        self.assertIsNotNone(old_auth.revoked_at)
        self.assertFalse(old_auth.usable)
        self.assertContains(response, result.token)
        self.assertContains(response, "previous token is no longer valid")
        self.assertEqual(response.context["verification_token"]["state"], "active")
        self.assertEqual(
            BallotAuthorization.objects.filter(
                voter=voter, consumed_at__isnull=True, revoked_at__isnull=True
            ).count(),
            1,
        )

    def test_verification_retrieve_replaces_expired_unused_token(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        old_auth = self._active_authorization(voter)
        old_auth.expires_at = timezone.now() - timedelta(hours=1)
        old_auth.save(update_fields=["expires_at"])

        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "retrieve_token": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"].status, "reissued")
        self.assertIsNotNone(response.context["result"].token)
        old_auth.refresh_from_db()
        self.assertIsNotNone(old_auth.revoked_at)

    def test_verification_revokes_expired_unused_token(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        auth = self._active_authorization(voter)
        auth.expires_at = timezone.now() - timedelta(hours=1)
        auth.save(update_fields=["expires_at"])

        response = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "revoke_token": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"].status, "revoked")
        auth.refresh_from_db()
        self.assertIsNotNone(auth.revoked_at)
        self.assertEqual(response.context["verification_token"]["state"], "revoked")
        self.assertContains(response, "Unused voting token revoked")

    def test_verification_blocks_token_changes_after_vote(self):
        self._login_verification_workstation()
        voter = self.voters[0]
        auth = self._active_authorization(voter)
        cast_at = timezone.now()
        Voter.objects.filter(pk=voter.pk).update(
            has_voted=True, voted_at=cast_at
        )
        BallotAuthorization.objects.filter(pk=auth.pk).update(consumed_at=cast_at)

        retrieve = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "retrieve_token": "1",
            },
        )
        self.assertEqual(retrieve.context["result"].status, "already_voted")
        self.assertIsNone(retrieve.context["result"].token)
        auth.refresh_from_db()
        self.assertIsNotNone(auth.consumed_at)
        self.assertIsNone(auth.revoked_at)
        self.assertEqual(retrieve.context["verification_token"]["state"], "used")
        self.assertNotContains(retrieve, 'name="retrieve_token"')

        revoke = self.client.post(
            reverse("workstation-verification"),
            {
                "election": str(self.election.pk),
                "voter_id": voter.voter_id,
                "revoke_token": "1",
            },
        )
        self.assertEqual(revoke.context["result"].status, "already_voted")
        auth.refresh_from_db()
        self.assertIsNone(auth.revoked_at)
        self.assertEqual(revoke.context["verification_token"]["state"], "used")
        self.assertNotContains(revoke, 'name="revoke_token"')

    def test_workstation_management_requires_superuser(self):
        self.client.force_login(self.official)
        r = self.client.get(reverse("workstation-manage"))
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.superuser)
        r = self.client.get(reverse("workstation-manage"))
        self.assertEqual(r.status_code, 200)

    def test_auto_logout_present_in_receipt_template(self):
        # smoke: receipt template contains 3-second auto logout
        from django.template.loader import render_to_string

        html = render_to_string("ballot/receipt.html", {"election": self.election, "receipt_code": "X", "ballot_hash": "H", "box_seq": 1, "idempotent": False})
        self.assertIn("3 seconds", html)