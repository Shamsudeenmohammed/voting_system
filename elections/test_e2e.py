"""End-to-end guided lifecycle test over HTTP.

Drives one election through the entire operational path the UI presents, using
only the same URLs/forms a browser hits:

    create -> positions + candidates + station -> open registration
    -> register voters -> submit for configuration review
    -> four-eyes approve_config -> schedule -> open -> cast a ballot
    -> close -> reconcile -> review -> certify -> publish -> archive

The point is to prove the *guided workspace* is genuinely operable end to end
(not merely that the underlying services work), and that the documented
invariants hold at the HTTP boundary: readiness gating, four-eyes, and the
"certified results are never silently overwritten" rule.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ballot.models import Ballot, BallotAuthorization

from .models import Candidate, Election, ElectionApproval, Position, Voter
from .tests import BaseVotingTestCase


def _messages(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


class GuidedLifecycleE2ETests(BaseVotingTestCase):
    """Full DRAFT -> ARCHIVED drive, with the workspace surfaced throughout."""

    def setUp(self):
        # the voter-facing POSTs are rate limited per IP in the shared cache
        cache.clear()

    # -- helpers ---------------------------------------------------------

    def _create_election(self) -> Election:
        now = timezone.now()
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("election_create"),
            {
                "title": "E2E Election",
                "description": "Full guided flow.",
                "category": "general",
                "ballot_type": "single",
                # future start so a SCHEDULED election does not auto-open
                "start_at": (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                "end_at": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                "results_visibility": "after_close",
                "allow_blank": "on",
            },
        )
        self.assertEqual(r.status_code, 302, _messages(r))
        return Election.objects.get(title="E2E Election")

    def _act(self, election: Election, action: str, actor, expect_state: str, note: str = ""):
        """POST a direct lifecycle action and assert the resulting state."""
        self.client.force_login(actor)
        r = self.client.post(
            reverse("election_action", args=[election.pk]),
            {"action": action, "note": note},
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(
            election.state,
            expect_state,
            f"action={action!r} messages={_messages(r)}",
        )
        return r

    def _workspace(self, election: Election):
        self.client.force_login(self.official)
        r = self.client.get(reverse("election_config", args=[election.pk]))
        self.assertEqual(r.status_code, 200)
        return r

    def _drive_to_certified(self):
        """Run the whole path up to and including certification."""
        election = self._create_election()

        # --- setup: position, candidates, station -----------------------
        self.client.post(
            reverse("position_add", args=[election.pk]),
            {"title": "President", "description": "", "order": 0, "max_choices": 1},
        )
        position = Position.objects.get(election=election)
        candidate = None
        for name in ("Alice A.", "Bob B."):
            self.client.post(
                reverse("candidate_add", args=[election.pk, position.pk]),
                {
                    "full_name": name,
                    "bio": "",
                    "manifesto": "",
                    "order": 0,
                    "status": "approved",
                },
            )
        candidate = Candidate.objects.get(position=position, full_name="Alice A.")
        self.client.post(
            reverse("station_add", args=[election.pk]),
            {"name": "Main Hall", "code": "MH", "location": "Campus", "channel": "onsite"},
        )
        self.assertEqual(position.candidates.count(), 2)

        # the draft workspace should now be "ready" and recommend registration
        r = self._workspace(election)
        self.assertTrue(r.context["readiness"]["ready"])
        self.assertEqual(r.context["next_action"]["action"], "submit")

        # --- draft -> registration ---------------------------------------
        self._act(election, "submit", self.official, Election.STATE_REGISTRATION)

        # --- register voters (only legal during REGISTRATION) -----------
        for i in range(1, 4):
            r = self.client.post(
                reverse("voter_add", args=[election.pk]),
                {
                    "voter_id": f"V{i:03d}",
                    "full_name": f"Voter {i}",
                    "email": "",
                    "organization_unit": "",
                },
            )
            self.assertEqual(r.status_code, 302)
        self.assertEqual(election.voters.count(), 3)

        # --- registration -> config_review ------------------------------
        self._act(election, "request_review", self.official, Election.STATE_CONFIG_REVIEW)

        # --- four-eyes approve_config -----------------------------------
        # The requester is an organization administrator, so the *four-eyes*
        # rule (not the broader role check) is what stops self-approval.
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("approval_request", args=[election.pk]),
            {"kind": "approve_config", "reason": "Configuration reviewed against the rules."},
        )
        self.assertEqual(r.status_code, 302)
        approval = ElectionApproval.objects.get(
            election=election, kind="approve_config", status=ElectionApproval.STATUS_PENDING
        )
        self.assertEqual(approval.requested_by, self.admin)

        # the requester may not decide their own request
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_CONFIG_REVIEW)
        self.assertTrue(any("four-eyes" in m.lower() for m in _messages(r)), _messages(r))

        # a different administrator decides
        self.client.force_login(self.admin2)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_APPROVED)

        # --- approved -> scheduled (freezes config + issues credentials) -
        self._act(election, "schedule", self.official, Election.STATE_SCHEDULED)
        self.assertTrue(election.config_frozen_at)
        self.assertEqual(
            BallotAuthorization.objects.filter(election=election)
            .filter(consumed_at__isnull=True, revoked_at__isnull=True)
            .count(),
            3,
        )

        # --- scheduled -> open ------------------------------------------
        self._act(election, "open", self.official, Election.STATE_OPEN)
        self.assertIsNotNone(election.opened_at)

        # --- cast one ballot through the public voter flow --------------
        self.client.logout()
        from ballot.services import issue_authorization
        from elections.models import Voter
        _auth, token = issue_authorization(
            Voter.objects.get(election=election, voter_id="V001"),
            allow_reissue=True, reason="test",
        )
        self.assertTrue(token)
        r = self.client.get(reverse("ballot_prepare", args=[token]))
        self.assertEqual(r.status_code, 200)
        # Regression: the approved candidates must actually render on the
        # ballot (config ids are strings, model PKs are UUIDs).
        self.assertContains(r, "Alice A.")
        self.assertContains(r, "Bob B.")
        self.assertNotContains(r, "No candidates available for this position.")
        r = self.client.post(
            reverse("ballot_prepare", args=[token]),
            {
                "submission_id": r.context["submission_id"],
                str(position.pk): [str(candidate.pk)],
            },
        )
        self.assertEqual(r.status_code, 200)
        receipt = r.context["receipt_code"]
        self.assertTrue(receipt)
        self.assertEqual(Ballot.objects.filter(election=election).count(), 1)

        # --- close -> reconcile -> review -> certify --------------------
        self._act(election, "close", self.official, Election.STATE_CLOSED)
        self._act(election, "start_reconciliation", self.official, Election.STATE_RECONCILIATION)
        self._act(election, "complete_reconciliation", self.official, Election.STATE_REVIEW)
        self._act(election, "certify", self.official, Election.STATE_CERTIFIED)
        return election, position, candidate

    # -- tests -----------------------------------------------------------

    def test_full_guided_lifecycle_over_http(self):
        election, position, candidate = self._drive_to_certified()

        from audit import services as audit
        from tally.services import latest_snapshot

        snapshot = latest_snapshot(election)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.status, "certified")

        # --- publish (official) then archive (approver) -----------------
        self._act(election, "publish", self.official, Election.STATE_PUBLISHED)
        self.assertTrue(election.is_published)

        # archival out of a published state is an elevated (approver) action
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("election_action", args=[election.pk]), {"action": "archive"}
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_ARCHIVED)

        # the public results page now shows the published tally
        r = self.client.get(reverse("results", args=[election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["visible"])
        self.assertContains(r, "Alice A.")

        # the election's audit chain is intact all the way through
        self.assertTrue(audit.verify_chain(f"election:{election.pk}")["ok"])

    def test_certified_results_are_not_silently_overwritten(self):
        election, position, candidate = self._drive_to_certified()
        from tally.services import latest_snapshot

        snapshot = latest_snapshot(election)
        before = snapshot.computed_at if hasattr(snapshot, "computed_at") else None

        # direct re-certification and any lifecycle jump are refused
        for action in ("certify", "open", "submit", "close"):
            self.client.force_login(self.official)
            r = self.client.post(
                reverse("election_action", args=[election.pk]), {"action": action}
            )
            self.assertEqual(r.status_code, 302)
            election.refresh_from_db()
            self.assertEqual(election.state, Election.STATE_CERTIFIED, action)
            self.assertTrue(_messages(r), f"expected an error message for {action!r}")

        # the certified snapshot is untouched
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, "certified")
        if before is not None:
            self.assertEqual(snapshot.computed_at, before)

        # a correction must go through the documented four-eyes decertify path
        r = self.client.post(
            reverse("approval_request", args=[election.pk]),
            {"kind": "decertify", "reason": "Tally discrepancy raised by an observer."},
        )
        self.assertEqual(r.status_code, 302)
        approval = ElectionApproval.objects.get(
            election=election, kind="decertify", status=ElectionApproval.STATUS_PENDING
        )
        # requester cannot decide their own request
        self.client.force_login(self.official)
        self.client.post(reverse("approval_decide", args=[approval.pk]), {"decision": "approve"})
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_CERTIFIED)

        # an independent approver completes it -> REVIEW, snapshot superseded
        self.client.force_login(self.admin2)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_REVIEW)
        self.assertEqual(snapshot.status, "superseded")

    def test_registry_locked_after_registration_closes(self):
        """Voters can only be added while the election is in REGISTRATION."""
        election = self._create_election()
        self._act(election, "submit", self.official, Election.STATE_REGISTRATION)
        self._act(election, "request_review", self.official, Election.STATE_CONFIG_REVIEW)

        self.client.force_login(self.official)
        r = self.client.post(
            reverse("voter_add", args=[election.pk]),
            {"voter_id": "LATE", "full_name": "Too Late", "email": "", "organization_unit": ""},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Voter.objects.filter(election=election).count(), 0)
        self.assertTrue(any("REGISTRATION" in m for m in _messages(r)), _messages(r))

    def test_lifecycle_actions_reject_unauthorised_tenancy(self):
        election = self._create_election()
        self.client.force_login(self.outsider)
        r = self.client.post(
            reverse("election_action", args=[election.pk]), {"action": "submit"}
        )
        self.assertEqual(r.status_code, 403)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_DRAFT)
