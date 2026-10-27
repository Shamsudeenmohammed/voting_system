"""Tally engine tests: determinism, reconciliation, versioning, verification."""
from __future__ import annotations

from django.utils import timezone

from elections.lifecycle import LifecycleError, transition
from elections.models import Election
from elections.tests import BaseVotingTestCase

from . import services
from .models import TallySnapshot


class TallyTests(BaseVotingTestCase):
    def _closed_with_votes(self, votes):
        """Open election, cast `votes` (lists of candidate indexes), close it."""
        election, position, candidates = self.open_election(with_voters=len(votes))
        from ballot.services import cast_ballot, issue_authorization, prepare_ballot

        for voter, picks in zip(election.voters.all(), votes):
            issue_authorization(voter, allow_reissue=True, reason="test")
            cast_ballot(
                election=election,
                voter=voter,
                submission_id=prepare_ballot(voter, election).submission_id,
                selections={
                    str(position.pk): [str(candidates[i].pk) for i in picks]
                },
            )
        election = transition(election, Election.STATE_CLOSED, actor=self.official)
        election = transition(
            election, Election.STATE_RECONCILIATION, actor=self.official
        )
        return election, position, candidates

    def test_count_is_correct_and_reconciled(self):
        election, position, candidates = self._closed_with_votes([[0], [0], [1]])
        snapshot = services.latest_snapshot(election)
        self.assertTrue(snapshot.reconciliation_ok)
        self.assertEqual(snapshot.ballots_accepted, 3)
        self.assertEqual(snapshot.ballots_counted, 3)
        rows = snapshot.payload["positions"][0]["candidates"]
        self.assertEqual(rows[0]["full_name"], "Alice A.")
        self.assertEqual(rows[0]["votes"], 2)
        self.assertEqual(rows[1]["votes"], 1)

    def test_recount_is_deterministic(self):
        # NB: the test position allows max_choices=1, so each ballot picks
        # exactly one candidate; determinism is about the count, not the picks.
        election, _, _ = self._closed_with_votes([[0], [1], [1]])
        v1 = services.compute_snapshot(election.pk, reason="first count")
        v2 = services.compute_snapshot(election.pk, reason="recount")
        self.assertNotEqual(v1.pk, v2.pk)  # versioned, never overwritten
        self.assertEqual(v1.payload, v2.payload)
        self.assertEqual(v2.version, v1.version + 1)

    def test_undervotes_and_blanks(self):
        election, position, _ = self._closed_with_votes([[], [0], [0]])
        snapshot = services.latest_snapshot(election)
        totals = snapshot.payload["positions"][0]["totals"]
        self.assertEqual(totals["undervotes"], 1)
        self.assertEqual(snapshot.ballots_accepted, 3)

    def test_verify_detects_tampered_payload(self):
        election, _, _ = self._closed_with_votes([[0], [1]])
        snapshot = services.latest_snapshot(election)
        report = services.verify_snapshot(snapshot)
        self.assertTrue(report["payload_matches"])
        self.assertTrue(report["hash_matches"])
        self.assertTrue(report["signature_valid"])

        snapshot.payload["positions"][0]["candidates"][0]["votes"] = 999
        snapshot.save(update_fields=["payload"])
        report = services.verify_snapshot(snapshot)
        self.assertFalse(report["payload_matches"])

    def test_certification_blocked_when_reconciliation_fails(self):
        from ballot.models import Ballot, BallotBox

        election, position, candidates = self._closed_with_votes([[0]])
        # simulate an inconsistent box (e.g. manual DB tampering)
        BallotBox.objects.filter(election=election).update(last_seq=5)
        election = transition(election, Election.STATE_REVIEW, actor=self.official)
        with self.assertRaises(LifecycleError) as ctx:
            transition(election, Election.STATE_CERTIFIED, actor=self.admin)
        self.assertEqual(ctx.exception.code, "reconciliation_failed")

    def test_spoiled_ballot_counted_invalid(self):
        from ballot.models import Ballot
        from ballot import crypto

        election, _, _ = self._closed_with_votes([[0]])
        # inject a ballot whose payload cannot be decrypted
        Ballot.objects.create(
            election=election,
            submission_id=crypto.new_submission_id(),
            receipt_code=crypto.new_receipt_code(),
            payload="not-a-fernet-token",
            ballot_hash="x" * 64,
            config_hash=election.config_hash,
            box_seq=2,
            accepted_at=timezone.now(),
        )
        from ballot.models import BallotBox

        BallotBox.objects.filter(election=election).update(last_seq=2)
        services.compute_snapshot(election.pk, reason="count with spoiled ballot")
        snapshot = services.latest_snapshot(election)
        self.assertEqual(snapshot.ballots_accepted, 2)
        self.assertEqual(snapshot.ballots_invalid, 1)
        self.assertEqual(snapshot.ballots_counted, 1)
        # 2 accepted = 1 valid + 1 invalid, box_seq 2 == accepted -> reconciled
        self.assertTrue(snapshot.reconciliation_ok)

    def test_certified_snapshot_is_signed(self):
        election, _, _ = self._closed_with_votes([[0]])
        election = transition(election, Election.STATE_REVIEW, actor=self.official)
        election = transition(election, Election.STATE_CERTIFIED, actor=self.admin)
        snapshot = services.latest_snapshot(election)
        self.assertEqual(snapshot.status, TallySnapshot.STATUS_CERTIFIED)
        self.assertTrue(snapshot.signature)
        self.assertTrue(snapshot.certified_at)

    def test_live_visibility_policy(self):
        election, _, _ = self._closed_with_votes([[0]])
        election = transition(election, Election.STATE_REVIEW, actor=self.official)
        election = transition(election, Election.STATE_CERTIFIED, actor=self.admin)
        election = transition(election, Election.STATE_PUBLISHED, actor=self.admin)
        self.client.force_login(self.auditor)
        r = self.client.get(f"/elections/{election.pk}/results/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Alice A.")
