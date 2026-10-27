from __future__ import annotations

import threading
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from audit.models import AuditEvent
from elections.models import Election, Position, Voter
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
    issue_voter_token,
    prepare_ballot,
    revoke_credential,
    revoke_voter_token,
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
        auth, token = issue_authorization(voter, allow_reissue=True, reason="test")
        self.assertFalse(
            BallotAuthorization.objects.filter(token_hash=token).exists(),
            "plaintext token must not be persisted",
        )
        self.assertEqual(auth.token_hash, hash_token(token))

    def test_audit_event_contains_no_ballot_identity(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, token = issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(voter, allow_reissue=True, reason="test")
        prep = prepare_ballot(voter, election)
        first = self._cast(election, voter, position, candidates[0], prep.submission_id)
        second = self._cast(election, voter, position, candidates[0], prep.submission_id)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.receipt_code, second.receipt_code)
        self.assertEqual(Ballot.objects.filter(election=election).count(), 1)

    def test_expired_preparation_rejected(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(v1, allow_reissue=True, reason="test")
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
        auth, _token = issue_authorization(voter, allow_reissue=True, reason="test")
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
            issue_authorization(voter, allow_reissue=True, reason="test")
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
        issue_authorization(voter, allow_reissue=True, reason="test")
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
        auth, _ = issue_authorization(voter, allow_reissue=True, reason="test")
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


class TokenLifecycleTests(BaseVotingTestCase):
    def _active_auth(self, voter):
        return (
            BallotAuthorization.objects.filter(
                voter=voter,
                consumed_at__isnull=True,
                revoked_at__isnull=True,
            )
            .order_by("-issued_at")
            .first()
        )

    def test_revoke_unused_token_is_audited_and_blocks_use(self):
        election, _, _ = self.open_election()
        voter = election.voters.first()
        auth = self._active_auth(voter)
        self.assertIsNotNone(auth)

        result = revoke_voter_token(
            voter, actor=self.official, reason="expired token"
        )

        self.assertEqual(result.status, "revoked")
        self.assertEqual(result.revoked, 1)
        auth.refresh_from_db()
        self.assertIsNotNone(auth.revoked_at)
        self.assertFalse(auth.usable)
        self.assertTrue(
            AuditEvent.objects.filter(
                election=election, action="credential_revoked"
            ).exists()
        )

    def test_expired_unused_token_can_be_revoked(self):
        election, _, _ = self.open_election()
        voter = election.voters.first()
        auth = self._active_auth(voter)
        auth.expires_at = timezone.now() - timedelta(hours=1)
        auth.save(update_fields=["expires_at"])

        result = revoke_voter_token(voter, actor=self.official, reason="expired")

        self.assertEqual(result.status, "revoked")
        auth.refresh_from_db()
        self.assertIsNotNone(auth.revoked_at)

    def test_revoke_after_vote_is_refused(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, _token = issue_authorization(
            voter, allow_reissue=True, reason="test"
        )
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )

        result = revoke_voter_token(voter, actor=self.official, reason="attempt")

        self.assertEqual(result.status, "already_voted")
        self.assertEqual(result.revoked, 0)
        auth.refresh_from_db()
        self.assertIsNotNone(auth.consumed_at)
        self.assertIsNone(auth.revoked_at)

    def test_expired_unused_token_can_be_replaced(self):
        election, _, _ = self.open_election()
        voter = election.voters.first()
        old_auth = self._active_auth(voter)
        old_auth.expires_at = timezone.now() - timedelta(hours=1)
        old_auth.save(update_fields=["expires_at"])

        result = issue_voter_token(
            voter,
            election=election,
            actor=self.official,
            allow_reissue=True,
            reason="expired token",
        )

        self.assertEqual(result.status, "reissued")
        self.assertIsNotNone(result.token)
        old_auth.refresh_from_db()
        self.assertIsNotNone(old_auth.revoked_at)

    def test_reissue_after_vote_is_refused(self):
        election, position, candidates = self.open_election()
        voter = election.voters.first()
        auth, _token = issue_authorization(
            voter, allow_reissue=True, reason="test"
        )
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={str(position.pk): [str(candidates[0].pk)]},
        )

        result = issue_voter_token(
            voter,
            election=election,
            actor=self.official,
            allow_reissue=True,
            reason="attempt",
        )

        self.assertEqual(result.status, "already_voted")
        self.assertIsNone(result.token)
        auth.refresh_from_db()
        self.assertIsNotNone(auth.consumed_at)
        self.assertIsNone(auth.revoked_at)


class BallotRenderingTests(BaseVotingTestCase):
    """The ballot page must render the approved candidates of each position."""

    def _ballot_page(self, election, voter):
        from django.urls import reverse

        from .services import issue_authorization

        _auth, token = issue_authorization(voter, allow_reissue=True, reason="test")
        return self.client.get(reverse("ballot_prepare", args=[token]))

    def test_rows_carry_the_positions_approved_candidates(self):
        from .views import _positions

        election, position, candidates = self.open_election(with_voters=2)
        rows = _positions(election)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position"].pk, position.pk)
        self.assertEqual(len(rows[0]["candidates"]), len(candidates))
        # config ids are strings while model PKs are UUID objects; the row
        # must contain the candidates, not an empty list.  Candidate order is
        # not asserted here: fixtures leave ``order`` at its default so the
        # canonical order falls back to the (random) UUID, and this test only
        # guards the string-id -> model mapping.
        self.assertEqual(
            sorted((c.pk for c in rows[0]["candidates"]), key=str),
            sorted((c.pk for c in candidates), key=str),
        )

    def test_ballot_page_shows_candidate_names(self):
        election, position, candidates = self.open_election(with_voters=2)
        r = self._ballot_page(election, election.voters.first())
        self.assertEqual(r.status_code, 200)
        for candidate in candidates:
            self.assertContains(r, candidate.full_name)
        self.assertNotContains(r, "No candidates available for this position.")
        rows = r.context["rows"]
        self.assertTrue(all(row["candidates"] for row in rows))

    def test_multiple_positions_each_render_their_candidates(self):
        from elections.models import Candidate, Position

        from .views import _positions

        election, position, candidates = self.open_election(with_voters=1)
        second = Position.objects.create(
            election=election, title="SRC Treasurer", max_choices=1
        )
        Candidate.objects.create(position=second, full_name="Carol C.")
        rows = {row["position"].pk: row for row in _positions(election)}
        self.assertEqual(len(rows[position.pk]["candidates"]), 2)
        self.assertEqual(len(rows[second.pk]["candidates"]), 1)


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
            issue_authorization(voter, allow_reissue=True, reason="test")
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


# ---------------------------------------------------------------------------
# Voter Login Tests (new dedicated login flow)
# ---------------------------------------------------------------------------


class VoterLoginTests(BaseVotingTestCase):
    """Tests for the dedicated voter login page (Voter ID + 6-char token)."""

    def setUp(self):
        super().setUp()
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        self.voter = self.election.voters.first()

    def test_voter_login_page_loads(self):
        """GET /elections/<pk>/login/ returns the login form."""
        from django.urls import reverse

        response = self.client.get(reverse("voter_login", args=[self.election.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voter Login")
        self.assertContains(response, "Voter ID")
        self.assertContains(response, "Voting Credential")

    def test_voter_login_success_redirects_to_ballot(self):
        """Valid Voter ID + token redirects to ballot preparation."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        # Should redirect to ballot_prepare
        self.assertEqual(response.status_code, 302)
        self.assertIn("ballot", response.url)

    def test_voter_login_invalid_voter_id_rejected(self):
        """Invalid voter ID shows uniform error (no enumeration)."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": "NONEXISTENT", "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Voter ID or voting credential", status_code=403)

    def test_voter_login_invalid_token_rejected(self):
        """Invalid token shows uniform error."""
        from django.urls import reverse

        # Use a 6-char token that doesn't exist (format is valid but token is wrong)
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": "WRONG1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Voter ID or voting credential", status_code=403)

    def test_voter_login_mismatched_voter_id_token_rejected(self):
        """Token for voter A + voter ID for voter B is rejected."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        voter_b = self.election.voters.all()[1]
        _auth, token = issue_authorization(voter_b, allow_reissue=True, reason="test")
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Voter ID or voting credential", status_code=403)

    def test_voter_login_token_too_long_rejected(self):
        """Tokens longer than 6 characters are rejected."""
        from django.urls import reverse

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": "TOOLONGTOKEN"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "6 characters or fewer", status_code=400)

    def test_voter_login_already_voted_shows_correct_message(self):
        """Already-voted voter sees the required message."""
        from ballot.services import cast_ballot, issue_authorization, prepare_ballot
        from django.urls import reverse

        # Cast a ballot first
        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        prep = prepare_ballot(self.voter, self.election)
        cast_ballot(
            election=self.election,
            voter=self.voter,
            submission_id=prep.submission_id,
            selections={str(self.position.pk): [str(self.candidates[0].pk)]},
        )

        # Try to log in again with same credentials
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            "You have already cast your vote for this election. You cannot vote again.",
            status_code=403,
        )

    def test_voter_login_expired_token_rejected(self):
        """Expired token is rejected with appropriate message."""
        from ballot.models import BallotAuthorization
        from ballot.services import issue_authorization
        from django.urls import reverse
        from django.utils import timezone
        from datetime import timedelta

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        # Expire the token
        BallotAuthorization.objects.filter(voter=self.voter).update(
            expires_at=timezone.now() - timedelta(hours=1)
        )

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "expired", status_code=403)

    def test_voter_login_revoked_token_rejected(self):
        """Revoked token is rejected."""
        from ballot.services import issue_authorization, revoke_credential
        from django.urls import reverse

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        revoke_credential(self.voter)

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "revoked", status_code=403)

    def test_voter_login_inactive_voter_rejected(self):
        """Inactive/suspended voter is rejected."""
        from ballot.services import issue_authorization
        from django.urls import reverse
        from elections.models import Voter

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        self.voter.status = Voter.STATUS_SUSPENDED
        self.voter.save()

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "not eligible", status_code=403)

    def test_voter_login_wrong_election_token_rejected(self):
        """Token from different election is rejected."""
        from ballot.services import issue_authorization
        from django.urls import reverse
        from elections.models import Candidate

        # Create another election
        other_election = self.make_election(title="Other Election")
        other_position = Position.objects.create(election=other_election, title="President", max_choices=1)
        Candidate.objects.create(position=other_position, full_name="Other Candidate")
        other_voter = Voter.objects.create(election=other_election, voter_id="V999")
        from elections.lifecycle import transition, request_approval, decide_approval
        other_election = transition(other_election, Election.STATE_REGISTRATION, actor=self.official)
        other_election = transition(other_election, Election.STATE_CONFIG_REVIEW, actor=self.official)
        approval = request_approval(other_election, "approve_config", actor=self.admin, reason="ok")
        decide_approval(approval.pk, actor=self.admin2, approve=True)
        other_election.refresh_from_db()
        other_election = transition(other_election, Election.STATE_SCHEDULED, actor=self.official)
        other_election = transition(other_election, Election.STATE_OPEN, actor=self.official)

        _auth, token = issue_authorization(other_voter, allow_reissue=True, reason="test")

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": other_voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Invalid Voter ID or voting credential", status_code=403)

    def test_voter_login_rate_limited(self):
        """Too many failed attempts triggers rate limiting."""
        from django.urls import reverse
        from django.core.cache import cache

        # Clear any existing cache
        cache.clear()

        # Make 8 failed attempts (default limit is 8)
        for i in range(9):
            response = self.client.post(
                reverse("voter_login", args=[self.election.pk]),
                {"voter_id": f"FAKE{i}", "token": "FAKE"},
            )
            if i < 8:
                self.assertEqual(response.status_code, 403)
            else:
                # 9th attempt should be rate limited
                self.assertEqual(response.status_code, 429)
                self.assertContains(response, "Too many login attempts", status_code=429)

    def test_voter_login_clears_throttle_on_success(self):
        """Successful login clears the rate limit counter."""
        from ballot.services import issue_authorization
        from django.urls import reverse
        from django.core.cache import cache

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")

        # Make 3 failed attempts
        for i in range(3):
            self.client.post(
                reverse("voter_login", args=[self.election.pk]),
                {"voter_id": f"FAKE{i}", "token": "FAKE"},
            )

        # Now succeed
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 302)

        # Next failed attempt should not be rate limited (counter reset)
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": "FAKE", "token": "FAKE"},
        )
        self.assertEqual(response.status_code, 403)  # Not 429

    def test_voter_login_token_format_numeric(self):
        """Numeric token format works correctly."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        self.election.token_format = Election.TOKEN_FORMAT_NUMERIC
        self.election.save()

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        self.assertTrue(token.isdigit())
        self.assertEqual(len(token), 6)

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 302)

    def test_voter_login_token_format_alphanumeric(self):
        """Alphanumeric token format works correctly."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        self.election.token_format = Election.TOKEN_FORMAT_ALPHANUMERIC
        self.election.save()

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        self.assertTrue(all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in token))
        self.assertEqual(len(token), 6)

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 302)

    def test_voter_login_case_insensitive_alphanumeric(self):
        """Alphanumeric tokens are case-insensitive."""
        from ballot.services import issue_authorization
        from django.urls import reverse

        self.election.token_format = Election.TOKEN_FORMAT_ALPHANUMERIC
        self.election.save()

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        # Submit lowercase
        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token.lower()},
        )
        self.assertEqual(response.status_code, 302)

    def test_voter_login_preserves_leading_zeros_numeric(self):
        """Numeric tokens preserve leading zeros."""
        from ballot import crypto
        from ballot.models import BallotAuthorization
        from django.urls import reverse

        self.election.token_format = Election.TOKEN_FORMAT_NUMERIC
        self.election.save()

        # Manually create a token with leading zeros
        token = "001234"
        token_hash = crypto.hash_token(token)
        BallotAuthorization.objects.create(
            election=self.election,
            voter=self.voter,
            token_hash=token_hash,
        )

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 302)

    def test_voter_login_closed_election_rejected(self):
        """Login fails when election is not open."""
        from ballot.services import issue_authorization
        from django.urls import reverse
        from elections.lifecycle import transition

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        # Close the election
        transition(self.election, Election.STATE_CLOSED, actor=self.official)
        self.election.refresh_from_db()

        response = self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "not currently open", status_code=403)

    def test_voter_login_audit_record_created(self):
        """Successful login creates audit record without token."""
        from audit.models import AuditEvent
        from ballot.services import issue_authorization
        from django.urls import reverse

        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        self.client.post(
            reverse("voter_login", args=[self.election.pk]),
            {"voter_id": self.voter.voter_id, "token": token},
        )

        event = AuditEvent.objects.filter(
            election=self.election, action="voter_login"
        ).first()
        self.assertIsNotNone(event)
        self.assertNotIn(token, str(event.details))

    def test_voter_login_duplicate_vote_checked_at_ballot_prepare(self):
        """Ballot prepare also rejects already-voted voters with correct message."""
        from ballot.services import cast_ballot, issue_authorization, prepare_ballot
        from django.urls import reverse

        # Cast a ballot
        _auth, token = issue_authorization(self.voter, allow_reissue=True, reason="test")
        prep = prepare_ballot(self.voter, self.election)
        cast_ballot(
            election=self.election,
            voter=self.voter,
            submission_id=prep.submission_id,
            selections={str(self.position.pk): [str(self.candidates[0].pk)]},
        )

        # Try to access ballot directly with token
        response = self.client.get(reverse("ballot_prepare", args=[token]))
        self.assertEqual(response.status_code, 403)
        self.assertContains(
            response,
            "You have already cast your vote for this election. You cannot vote again.",
            status_code=403,
        )
