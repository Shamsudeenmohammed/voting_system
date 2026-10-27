"""Super Admin permanent election deletion: backend authority + audit tests.

Covers:
* the Super Admin can permanently delete an election (with related records)
  via an explicit, confirmed, fully audited break-glass action;
* the audit chain for the election survives the deletion and still verifies
  (the ``election_id`` FK is preserved in the chain key when the row is gone);
* every other role (org admin, EC chair, official, auditor, observer,
  representative) is refused with 403 and the election is left intact;
* the confirmation token and documented-reason requirements are enforced
  server-side (not just hidden buttons).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse

from audit.models import AuditChain, AuditEvent
from audit import services as audit
from ballot.models import Ballot, BallotAuthorization, BallotPreparation, BallotBox
from organizations.models import OrganizationMembership
from representatives.models import IncidentReport, Representative
from tally.models import TallySnapshot

from .models import Election, Position, Candidate, Voter, PollingStation, ElectionApproval
from .tests import BaseVotingTestCase
from .test_authority import AuthorityFixture

User = get_user_model()


class SuperAdminDeleteTests(AuthorityFixture):
    def _delete_url(self, election):
        return reverse("election_delete", args=[election.pk])

    def _post_delete(self, election, *, reason="Emergency cleanup after governance decision", confirm="DELETE"):
        self.client.force_login(self.superuser)
        return self.client.post(
            self._delete_url(election),
            {"reason": reason, "confirm": confirm},
        )

    def test_delete_confirmation_page_renders_for_superadmin(self):
        election = self.make_election()
        self.client.force_login(self.superuser)
        r = self.client.get(self._delete_url(election))
        self.assertEqual(r.status_code, 200)
        from django.utils.html import escape

        self.assertContains(r, escape(election.title))
        self.assertContains(r, "DELETE")

    def test_delete_confirmation_page_forbidden_to_non_superusers(self):
        election = self.make_election()
        for user in (self.admin, self.official, self.chair, self.auditor):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                self.assertEqual(self.client.get(self._delete_url(election)).status_code, 403)

    def test_delete_requires_explicit_confirmation_token(self):
        election = self.make_election()
        self.make_ballot_structure(election)
        r = self._post_delete(election, confirm="wrong-token")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Election.objects.filter(pk=election.pk).exists())

    def test_delete_requires_documented_reason(self):
        election = self.make_election()
        r = self._post_delete(election, reason="short", confirm="DELETE")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Election.objects.filter(pk=election.pk).exists())

    def test_superadmin_deletes_election_with_related_records(self):
        election, position, candidates = self.open_election(with_voters=3)
        from ballot.services import cast_ballot, prepare_ballot

        voter = election.voters.first()
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        election = Election.objects.get(pk=election.pk)
        election = self._close_and_certify(election)

        # Ensure related rows exist before deletion
        self.assertTrue(Ballot.objects.filter(election=election).exists())
        self.assertTrue(BallotAuthorization.objects.filter(election=election).exists())
        self.assertTrue(TallySnapshot.objects.filter(election=election).exists())

        r = self._post_delete(election, reason="Certified results invalid after audit finding.")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Election.objects.filter(pk=election.pk).exists())
        self.assertFalse(Position.objects.filter(election=election).exists())
        self.assertFalse(Candidate.objects.filter(position__election=election).exists())
        self.assertFalse(Voter.objects.filter(election=election).exists())
        self.assertFalse(PollingStation.objects.filter(election=election).exists())
        self.assertFalse(ElectionApproval.objects.filter(election=election).exists())
        self.assertFalse(BallotAuthorization.objects.filter(election=election).exists())
        self.assertFalse(BallotPreparation.objects.filter(election=election).exists())
        self.assertFalse(Ballot.objects.filter(election=election).exists())
        self.assertFalse(BallotBox.objects.filter(election=election).exists())
        self.assertFalse(TallySnapshot.objects.filter(election=election).exists())
        self.assertFalse(Representative.objects.filter(election=election).exists())
        self.assertFalse(IncidentReport.objects.filter(election=election).exists())

    def test_delete_records_audit_event_with_actor_and_outcome(self):
        election = self.make_election()
        chain_key = f"election:{election.pk}"
        r = self._post_delete(election, reason="Post-mortem cleanup after data breach.")
        self.assertEqual(r.status_code, 302)

        event = AuditEvent.objects.filter(action="election_deleted").order_by("-seq").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.object_type, "election")
        self.assertEqual(event.object_id, str(election.pk))
        self.assertEqual(event.actor_id, self.superuser.pk)
        self.assertIn("Post-mortem cleanup", event.reason)
        self.assertTrue(event.details.get("deleted"))
        self.assertEqual(event.chain_key, chain_key)

    def test_audit_chain_still_verifies_after_election_deletion(self):
        election = self.make_election()
        self.make_ballot_structure(election)
        chain_key = f"election:{election.pk}"
        # Record a couple of transitions so the chain is non-trivial
        from .lifecycle import transition

        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        report_before = audit.verify_chain(chain_key)
        self.assertTrue(report_before["ok"])
        seq_before = report_before["events"]

        r = self._post_delete(election, reason="Compliance-driven permanent removal.")
        self.assertEqual(r.status_code, 302)

        # The election row is gone but the audit chain persists
        self.assertFalse(Election.objects.filter(pk=election.pk).exists())
        self.assertTrue(AuditChain.objects.filter(chain_key=chain_key).exists())

        report = audit.verify_chain(chain_key)
        self.assertTrue(report["ok"], report["error"])
        self.assertGreaterEqual(report["events"], seq_before)

        # The new deletion event is present and the FK was nulled (SET_NULL)
        event = AuditEvent.objects.filter(chain_key=chain_key, action="election_deleted").first()
        self.assertIsNotNone(event)
        self.assertIsNone(event.election_id)

    def test_delete_is_refused_for_non_superusers_and_inert(self):
        election = self.make_election()
        self.make_ballot_structure(election)
        for user in (self.admin, self.admin2, self.official, self.chair, self.auditor):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                r = self.client.post(
                    self._delete_url(election),
                    {"reason": "Attempting privilege escalation", "confirm": "DELETE"},
                )
                self.assertEqual(r.status_code, 403)
        self.assertTrue(Election.objects.filter(pk=election.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="election_deleted").exists())

    def test_delete_requires_login(self):
        election = self.make_election()
        r = self.client.get(self._delete_url(election))
        self.assertEqual(r.status_code, 302)  # redirect to login

    def test_delete_out_of_scope_election_forbidden(self):
        # outsider belongs only to other_org
        election = self.make_election()  # belongs to self.org
        self.client.force_login(self.outsider)
        r = self.client.get(self._delete_url(election))
        self.assertEqual(r.status_code, 403)

    def test_superadmin_can_delete_unrelated_election_by_direct_url(self):
        election = self.make_election()
        self.client.force_login(self.superuser)
        r = self.client.get(self._delete_url(election))
        self.assertEqual(r.status_code, 200)  # confirmation screen

    def test_delete_of_election_with_ballots_is_permitted_and_audited(self):
        election, position, candidates = self.open_election(with_voters=2)
        from ballot.services import cast_ballot, prepare_ballot

        voter = election.voters.first()
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        election = Election.objects.get(pk=election.pk)
        r = self._post_delete(election, reason="Ballot integrity failure discovered.")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Election.objects.filter(pk=election.pk).exists())
        self.assertFalse(Ballot.objects.filter(election=election).exists())
        self.assertTrue(AuditEvent.objects.filter(action="election_deleted").exists())

    def test_delete_not_permitted_via_election_config_post(self):
        # The config page is only a link; a POST to config with delete must not delete
        election = self.make_election()
        self.client.force_login(self.superuser)
        r = self.client.post(reverse("election_config", args=[election.pk]), {"delete": "1"})
        # no such action on config; election is intact
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Election.objects.filter(pk=election.pk).exists())

    def _close_and_certify(self, election):
        from .lifecycle import transition

        election = transition(election, Election.STATE_CLOSED, actor=self.official)
        election = transition(election, Election.STATE_RECONCILIATION, actor=self.official)
        election = transition(election, Election.STATE_REVIEW, actor=self.official)
        election = transition(election, Election.STATE_CERTIFIED, actor=self.official)
        return election
