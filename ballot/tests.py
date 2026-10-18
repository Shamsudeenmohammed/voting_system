"""Ballot engine tests: secrecy, exactly-once acceptance, idempotency.

The most important assertions here are *negative* ones: no schema, row, log
or audit event may ever link a voter identity to a ballot or its selections.
"""
from __future__ import annotations

import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from audit.models import AuditEvent
from elections.models import Election, Voter
from elections.tests import BaseVotingTestCase
from organizations.models import Organization, OrganizationMembership

from .crypto import hash_token, decrypt_payload
from .models import Ballot, BallotAuthorization, BallotBox, BallotPreparation
from .services import (
    DuplicateVote,
    ElectionNotOpen,
    InvalidSelection,
    InvalidSubmission,
    NotEligible,
    cast_ballot,
    find_receipt,
    issue_authorization,
    prepare_ballot,
    revoke_credential,
)

User = get_user_model()


class BallotSecrecyTests(BaseVotingTestCase):
    def test_ballot_schema_has_no_identity_columns(self):
        field_names = {f.name for f in Ballot._meta.get_fields()}
        for forbidden in ("voter", "authorization", "voter_id", "authorization_voter"):
            self.assertNotIn(forbidden, field_names)

    def test_token_is_never_stored_in_plaintext(self):
        election, _, _ = self.open_election()
        voter = election.voters.first()
        auth, token = issue_authorization(voter)
        self.assertFalse(
            BallotAuthorization.objects.filter(token_hash=token).exists(),
            "plaintext token must not be persisted",
        )
        self.assertEqual(auth.token_hash, hash_token(token))

    def test_audit_event_contains_no_ballot_identity(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, token = issue_authorization(voter)
        prep = prepare_ballot(voter, election)
        result = cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        event = AuditEvent.objects.get(election=election, action="ballot_accepted")
        blob = str(event.details) + event.reason + (event.ip_hash or "")
        self.assertNotIn(token, blob)
        self.assertNotIn(result.receipt_code, blob)
        self.assertNotIn(str(prep.submission_id), blob)
        self.assertNotIn(str(voter.pk), blob)
        self.assertEqual(event.ip_hash, "")

    def test_receipt_lookup_is_identity_free(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        prep = prepare_ballot(voter, election)
        result = cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        found = find_receipt(election, result.receipt_code)
        self.assertIsNotNone(found)
        self.assertNotIn("voter", found)
        self.assertNotIn("payload", found)

    def test_encrypted_payload_not_readable_without_key(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        ballot = Ballot.objects.get(election=election)
        self.assertNotIn(candidates[0].full_name, ballot.payload)
        # ... but the configured key holder can decrypt it for tallying
        data = decrypt_payload(ballot.payload)
        self.assertEqual(
            data["selections"][str(position.pk)], [str(candidates[0].pk)]
        )


class ExactlyOnceTests(BaseVotingTestCase):
    def _cast(self, election, voter, position, candidate, submission_id=None):
        prep_id = submission_id or prepare_ballot(voter, election).submission_id
        return cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep_id,
            selections={str(position.pk): [str(candidate.pk)]},
        )

    def test_second_credential_use_rejected(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        # Two live preparations of the same credential (e.g. ballot form open
        # twice): the second submission id must not bypass single-use.
        prep1 = prepare_ballot(voter, election)
        prep2 = prepare_ballot(voter, election)
        self._cast(election, voter, position, candidates[0], prep1.submission_id)
        with self.assertRaises(DuplicateVote):
            self._cast(election, voter, position, candidates[1], prep2.submission_id)
        self.assertEqual(Ballot.objects.filter(election=election).count(), 1)

    def test_idempotent_retry_returns_same_receipt(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        prep = prepare_ballot(voter, election)
        first = self._cast(election, voter, position, candidates[0], prep.submission_id)
        second = self._cast(election, voter, position, candidates[0], prep.submission_id)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.receipt_code, second.receipt_code)
        self.assertEqual(Ballot.objects.filter(election=election).count(), 1)

    def test_expired_preparation_rejected(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        prep = prepare_ballot(voter, election)
        BallotPreparation.objects.filter(pk=prep.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(InvalidSubmission):
            self._cast(
                election, voter, position, candidates[0], prep.submission_id
            )

    def test_revoked_credential_cannot_vote_but_cast_vote_stands(self):
        election, position, candidates = self.open_election()
        v1, v2, _v3 = election.voters.all()
        issue_authorization(v1)
        cast_ballot(
            election=election,
            voter=v1,
            submission_id=prepare_ballot(v1, election).submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        revoke_credential(v2)
        with self.assertRaises(NotEligible):
            cast_ballot(
                election=election,
                voter=v2,
                submission_id=prepare_ballot(v2, election).submission_id,
                selections={str(position.pk): [str(candidates[1].pk)]},
            )
        self.assertEqual(Ballot.objects.filter(election=election).count(), 1)

    def test_voter_marks_and_authorization_consumed(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, _token = issue_authorization(voter)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prepare_ballot(voter, election).submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )
        voter.refresh_from_db()
        auth.refresh_from_db()
        self.assertTrue(voter.has_voted)
        self.assertIsNotNone(auth.consumed_at)
        self.assertIsNone(auth.revoked_at)

    def test_box_sequence_matches_accepted_count(self):
        election, position, candidates = self.open_election()
        for voter in election.voters.all():
            issue_authorization(voter)
            cast_ballot(
                election=election,
                voter=voter,
                submission_id=prepare_ballot(voter, election).submission_id,
                selections={str(position.pk): [str(candidates[0].pk)]},
            )
        box = BallotBox.objects.get(election=election)
        self.assertEqual(box.last_seq, Ballot.objects.filter(election=election).count())
        self.assertEqual(
            sorted(Ballot.objects.filter(election=election).values_list("box_seq", flat=True)),
            [1, 2, 3],
        )

    def test_closed_election_rejects(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter)
        from elections.lifecycle import transition

        transition(election, Election.STATE_CLOSED, actor=self.official)
        election.refresh_from_db()
        with self.assertRaises(ElectionNotOpen):
            cast_ballot(
                election=election,
                voter=voter,
                submission_id=prepare_ballot(voter, election).submission_id,
                selections={str(position.pk): [str(candidates[0].pk)]},
            )

    def test_invalid_selection_rejected_without_side_effects(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, _ = issue_authorization(voter)
        with self.assertRaises(InvalidSelection):
            cast_ballot(
                election=election,
                voter=voter,
                submission_id=prepare_ballot(voter, election).submission_id,
                selections={str(position.pk): ["bogus"]},
            )
        voter.refresh_from_db()
        auth.refresh_from_db()
        self.assertFalse(voter.has_voted)
        self.assertIsNone(auth.consumed_at)
        self.assertEqual(Ballot.objects.filter(election=election).count(), 0)


class ThreadedCastTests(TransactionTestCase):
    """Two voters casting at the same time: both accepted, sequences intact.

    Uses TransactionTestCase so threads see committed data (and exercise the
    real row-level locking of the per-election ballot box).
    """

    def setUp(self):
        self.org = Organization.objects.create(name="T", slug="t-org")
        self.official = User.objects.create_user("t-official", password="x", is_staff=True)
        OrganizationMembership.objects.create(
            user=self.official, organization=self.org, role="election_official"
        )
        now = timezone.now()
        self.election = Election.objects.create(
            organization=self.org,
            title="Threaded",
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            created_by=self.official,
        )
        from elections.models import Position, Candidate

        self.position = Position.objects.create(
            election=self.election, title="President", max_choices=1
        )
        self.c1 = Candidate.objects.create(position=self.position, full_name="A")
        self.voters = [
            Voter.objects.create(election=self.election, voter_id=f"T{i:03d}")
            for i in range(6)
        ]
        from elections.lifecycle import transition

        self.election = transition(self.election, Election.STATE_REGISTRATION, actor=self.official)
        self.election = transition(self.election, Election.STATE_CONFIG_REVIEW, actor=self.official)
        from elections.lifecycle import request_approval, decide_approval

        admin = User.objects.create_user("t-admin", password="x", is_staff=True)
        admin2 = User.objects.create_user("t-admin2", password="x", is_staff=True)
        OrganizationMembership.objects.create(user=admin, organization=self.org, role="org_admin")
        OrganizationMembership.objects.create(user=admin2, organization=self.org, role="org_admin")
        approval = request_approval(
            self.election, "approve_config", actor=admin, reason="ok"
        )
        decide_approval(approval.pk, actor=admin2, approve=True)
        self.election.refresh_from_db()
        self.election = transition(self.election, Election.STATE_SCHEDULED, actor=self.official)
        self.election = transition(self.election, Election.STATE_OPEN, actor=self.official)

    def test_parallel_casts(self):
        errors = []
        preps = []
        for voter in self.voters:
            issue_authorization(voter)
            preps.append((voter, prepare_ballot(voter, self.election).submission_id))

        def worker(voter, submission_id):
            try:
                cast_ballot(
                    election=Election.objects.get(pk=self.election.pk),
                    voter=voter,
                    submission_id=submission_id,
                    selections={str(self.position.pk): [str(self.c1.pk)]},
                )
            except Exception as exc:  # pragma: no cover - surfaced below
                import traceback

                info = ""
                try:
                    from django.db import connection as _c

                    cause = exc.__cause__ or exc
                    info = (
                        f" [in_atomic={_c.in_atomic_block}"
                        f" errcode={getattr(cause, 'sqlite_errorcode', None)}"
                        f" errstr={getattr(cause, 'sqlite_errorname', None)}]"
                    )
                except Exception:  # pragma: no cover
                    pass
                errors.append(
                    f"{exc.__class__.__name__}: {exc}{info}\n{traceback.format_exc()}"
                )
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(voter, sub)) for voter, sub in preps
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], f"thread errors: {errors}")
        self.assertEqual(Ballot.objects.filter(election=self.election).count(), 6)
        box = BallotBox.objects.get(election=self.election)
        self.assertEqual(box.last_seq, 6)
        self.assertEqual(
            sorted(
                Ballot.objects.filter(election=self.election).values_list(
                    "box_seq", flat=True
                )
            ),
            list(range(1, 7)),
        )
