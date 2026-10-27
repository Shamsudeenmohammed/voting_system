"""Election-side authority tests: Super Admin override, chair oversight,
governance policy and representative restrictions.

These complement ``elections/tests.py`` (which covers the base lifecycle and
four-eyes rules) and lock in the behaviour added for the Super Admin / chair /
institution-administrator authority model:

* a Super Admin is never blocked by another person's approval and can complete
  work directly or via the explicit, confirmed, audited emergency override;
* an Election commission chair can decide approvals and run the lifecycle, and
  the per-election governance policy can reserve certify/publish for a role;
* an ordinary user cannot force a protected transition, change the governance
  policy or use the override by calling the URL directly;
* candidate representatives stay read-only and cannot reach staff surfaces.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse

from audit.models import AuditEvent
from organizations.models import OrganizationMembership
from organizations.roles import (
    can_approve_election,
    can_manage_election,
    can_view_election,
    is_election_staff,
)
from representatives.models import Representative

from . import governance
from .config import compute_config_hash
from .lifecycle import (
    LifecycleError,
    admin_override,
    request_approval,
    transition,
)
from .models import Election, ElectionApproval
from .tests import BaseVotingTestCase

User = get_user_model()


class AuthorityFixture(BaseVotingTestCase):
    """Base fixtures plus a Super Admin and an election-commission chair."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser("root", "root@example.org", "x")
        cls.chair = User.objects.create_user("chair", password="x", is_staff=True)
        OrganizationMembership.objects.create(
            user=cls.chair, organization=cls.org, role="ec_chair"
        )

    def review_ready(self) -> Election:
        """A DRAFT election advanced to CONFIG_REVIEW with a full ballot."""
        election = self.make_election()
        self.make_ballot_structure(election)
        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        election = transition(election, Election.STATE_CONFIG_REVIEW, actor=self.official)
        return election

    def review_results_ready(self) -> Election:
        """An election advanced all the way to REVIEW (awaiting certification)."""
        election, _position, _candidates = self.open_election(with_voters=1)
        from ballot.services import cast_ballot, prepare_ballot

        voter = election.voters.first()
        prep = prepare_ballot(voter, election)
        cast_ballot(
            election=election,
            voter=voter,
            submission_id=prep.submission_id,
            selections={},
        )
        election = transition(election, Election.STATE_CLOSED, actor=self.official)
        election = transition(election, Election.STATE_RECONCILIATION, actor=self.official)
        election = transition(election, Election.STATE_REVIEW, actor=self.official)
        return election


class SuperAdminApprovalTests(AuthorityFixture):
    def test_superadmin_can_self_decide_own_approval(self):
        election = self.review_ready()
        approval = request_approval(
            election, "approve_config", actor=self.superuser, reason="Reviewed by root."
        )
        approval = decide_approval(self.superuser, approval.pk)
        election.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_APPROVED)
        self.assertEqual(election.state, Election.STATE_APPROVED)

    def test_non_superuser_still_bound_by_four_eyes(self):
        election = self.review_ready()
        approval = request_approval(
            election, "approve_config", actor=self.admin, reason="Reviewed."
        )
        with self.assertRaises(LifecycleError) as ctx:
            decide_approval(self.admin, approval.pk)
        self.assertEqual(ctx.exception.code, "four_eyes")


def decide_approval(actor, approval_pk):
    """Tiny helper mirroring the view's decision call (approve)."""
    from .lifecycle import decide_approval as _decide

    return _decide(approval_pk, actor=actor, approve=True)


class SuperAdminOverrideTests(AuthorityFixture):
    def test_override_requires_superadmin(self):
        election = self.review_ready()
        with self.assertRaises(LifecycleError) as ctx:
            admin_override(
                election,
                Election.STATE_APPROVED,
                actor=self.admin,
                reason="A perfectly long documented reason.",
                confirm="OVERRIDE",
            )
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_override_requires_reason_and_confirmation(self):
        election = self.review_ready()
        with self.assertRaises(LifecycleError) as ctx:
            admin_override(
                election, Election.STATE_APPROVED, actor=self.superuser, reason="short", confirm="OVERRIDE"
            )
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(LifecycleError) as ctx:
            admin_override(
                election,
                Election.STATE_APPROVED,
                actor=self.superuser,
                reason="A sufficiently long documented reason.",
                confirm="nope",
            )
        self.assertEqual(ctx.exception.code, "confirmation_required")

    def test_override_bypasses_dual_control_and_is_audited(self):
        election = self.review_ready()
        election = admin_override(
            election,
            Election.STATE_APPROVED,
            actor=self.superuser,
            reason="Vendor outage blocked the second approver; documented here.",
            confirm="OVERRIDE",
        )
        self.assertEqual(election.state, Election.STATE_APPROVED)
        self.assertTrue(
            AuditEvent.objects.filter(action="admin_override", election=election).exists()
        )
        approval = ElectionApproval.objects.get(election=election, kind="approve_config")
        self.assertEqual(approval.status, ElectionApproval.STATUS_APPROVED)
        self.assertEqual(approval.decided_by_id, self.superuser.pk)
        self.assertEqual(approval.requested_by_id, self.superuser.pk)

    def test_override_refuses_illegal_edge(self):
        election = self.make_election()
        with self.assertRaises(LifecycleError) as ctx:
            admin_override(
                election,
                Election.STATE_OPEN,
                actor=self.superuser,
                reason="A sufficiently long documented reason.",
                confirm="OVERRIDE",
            )
        self.assertEqual(ctx.exception.code, "illegal_transition")


class GovernancePolicyTests(AuthorityFixture):
    def test_default_policy_preserves_behaviour(self):
        election = self.make_election()
        policy = governance.policy_for(election)
        self.assertEqual(policy["certify_authority"], "manage")
        self.assertTrue(policy["require_institution_approval"])
        self.assertEqual(
            governance.dual_control_kind(
                election, Election.STATE_CONFIG_REVIEW, Election.STATE_APPROVED
            ),
            "approve_config",
        )

    def test_rules_do_not_change_config_hash(self):
        election = self.make_election()
        self.make_ballot_structure(election)
        before = compute_config_hash(election)
        election.rules = {"governance": {"certify_authority": "ec_chair"}}
        election.save(update_fields=["rules"])
        self.assertEqual(before, compute_config_hash(election))

    def test_certify_authority_can_reserve_for_a_role(self):
        election = self.make_election()
        election.rules = {"governance": {"certify_authority": "ec_chair"}}
        election.save(update_fields=["rules"])
        self.assertFalse(governance.can_certify(self.official, election))
        self.assertFalse(governance.can_certify(self.admin, election))
        self.assertTrue(governance.can_certify(self.chair, election))
        self.assertTrue(governance.can_certify(self.superuser, election))

    def test_chair_cannot_certify_when_policy_reserves_it(self):
        election = self.review_results_ready()
        election.rules = {"governance": {"certify_authority": "org_admin"}}
        election.save(update_fields=["rules"])
        self.assertTrue(governance.can_certify(self.admin, election))
        with self.assertRaises(LifecycleError) as ctx:
            transition(election, Election.STATE_CERTIFIED, actor=self.chair)
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_dual_control_can_be_waived_by_policy(self):
        election = self.review_ready()
        election.rules = {"governance": {"require_institution_approval": False}}
        election.save(update_fields=["rules"])
        self.assertIsNone(
            governance.dual_control_kind(
                election, Election.STATE_CONFIG_REVIEW, Election.STATE_APPROVED
            )
        )
        election = transition(election, Election.STATE_APPROVED, actor=self.admin)
        self.assertEqual(election.state, Election.STATE_APPROVED)

    def test_governance_endpoint_updates_policy_and_audits(self):
        election = self.make_election()
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("election_governance", args=[election.pk]),
            {
                "certify_authority": "ec_chair",
                "publish_authority": "org_admin",
                "require_institution_approval": "on",
                "dual_approve_config": "on",
                "dual_reopen": "on",
                "dual_decertify": "on",
            },
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        policy = governance.policy_for(election)
        self.assertEqual(policy["certify_authority"], "ec_chair")
        self.assertEqual(policy["publish_authority"], "org_admin")
        self.assertTrue(
            AuditEvent.objects.filter(
                action="governance_policy_updated", election=election
            ).exists()
        )

    def test_governance_endpoint_forbidden_to_official(self):
        election = self.make_election()
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("election_governance", args=[election.pk]),
            {
                "certify_authority": "ec_chair",
                "publish_authority": "ec_chair",
                "require_institution_approval": "on",
            },
        )
        self.assertEqual(r.status_code, 403)
        election.refresh_from_db()
        self.assertEqual(governance.policy_for(election)["certify_authority"], "manage")

    def test_governance_endpoint_locked_after_draft(self):
        election = self.make_election()
        election = transition(election, Election.STATE_REGISTRATION, actor=self.official)
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("election_governance", args=[election.pk]),
            {"certify_authority": "ec_chair", "publish_authority": "ec_chair"},
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(governance.policy_for(election)["certify_authority"], "manage")


class OverrideEndpointTests(AuthorityFixture):
    def test_override_endpoint_forbidden_to_non_superusers(self):
        election = self.make_election()
        for user in (self.admin, self.official, self.chair):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                r = self.client.post(
                    reverse("election_override", args=[election.pk]),
                    {
                        "target": Election.STATE_REGISTRATION,
                        "reason": "A sufficiently long documented reason.",
                        "confirm": "OVERRIDE",
                    },
                )
                self.assertEqual(r.status_code, 403)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_DRAFT)

    def test_override_endpoint_requires_confirmation_token(self):
        election = self.make_election()
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("election_override", args=[election.pk]),
            {
                "target": Election.STATE_REGISTRATION,
                "reason": "A sufficiently long documented reason.",
                "confirm": "WRONG",
            },
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_DRAFT)

    def test_override_endpoint_superadmin_applies_transition(self):
        election = self.make_election()
        self.client.force_login(self.superuser)
        r = self.client.post(
            reverse("election_override", args=[election.pk]),
            {
                "target": Election.STATE_REGISTRATION,
                "reason": "A sufficiently long documented reason.",
                "confirm": "OVERRIDE",
            },
        )
        self.assertEqual(r.status_code, 302)
        election.refresh_from_db()
        self.assertEqual(election.state, Election.STATE_REGISTRATION)
        self.assertTrue(
            AuditEvent.objects.filter(action="admin_override", election=election).exists()
        )


class ChairElectionOversightTests(AuthorityFixture):
    def test_chair_can_decide_approval_raised_by_another_user(self):
        election = self.review_ready()
        approval = request_approval(
            election, "approve_config", actor=self.admin, reason="Reviewed."
        )
        approval = decide_approval(self.chair, approval.pk)
        election.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_APPROVED)
        self.assertEqual(election.state, Election.STATE_APPROVED)

    def test_chair_can_run_lifecycle(self):
        election = self.make_election()
        election = transition(election, Election.STATE_REGISTRATION, actor=self.chair)
        self.assertEqual(election.state, Election.STATE_REGISTRATION)

    def test_chair_is_an_approver_but_official_is_not(self):
        election = self.make_election()
        self.assertTrue(can_approve_election(self.chair, election))
        self.assertFalse(can_approve_election(self.official, election))


class RepresentativeRestrictionTests(AuthorityFixture):
    def _approved_rep(self, election):
        _position, candidates = self.make_ballot_structure(election)
        rep_user = User.objects.create_user("rep", password="x")
        Representative.objects.create(
            election=election,
            candidate=candidates[0],
            user=rep_user,
            full_name="Rep One",
            status=Representative.STATUS_APPROVED,
        )
        return rep_user

    def test_representative_is_read_only_and_never_staff(self):
        election = self.make_election()
        rep_user = self._approved_rep(election)
        self.assertTrue(can_view_election(rep_user, election))
        self.assertFalse(is_election_staff(rep_user, election))
        self.assertFalse(can_manage_election(rep_user, election))
        self.assertFalse(can_approve_election(rep_user, election))

    def test_representative_cannot_reach_staff_surfaces(self):
        election = self.make_election()
        rep_user = self._approved_rep(election)
        self.client.force_login(rep_user)
        self.assertEqual(self.client.get(reverse("manage_people")).status_code, 403)
        self.assertEqual(self.client.get(reverse("control_center")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("election_config", args=[election.pk])).status_code,
            403,
        )

    def test_representative_cannot_decide_an_approval(self):
        election = self.review_ready()
        approval = request_approval(
            election, "approve_config", actor=self.admin, reason="Reviewed."
        )
        rep_user = self._approved_rep(election)
        self.client.force_login(rep_user)
        r = self.client.post(
            reverse("approval_decide", args=[approval.pk]),
            {"decision": "approve", "note": "let me in"},
        )
        self.assertEqual(r.status_code, 403)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ElectionApproval.STATUS_PENDING)
