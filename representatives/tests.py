"""Representative surface tests: accreditation, incidents, restricted dashboard.

Covers the access rules documented in ``representatives.views``:
staff-only decisions, incident visibility (staff = all, reporter = own only)
and the aggregate-only representative dashboard.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse

from audit.models import AuditEvent
from ballot.services import cast_ballot, issue_authorization, prepare_ballot
from elections.tests import BaseVotingTestCase

from .models import AttendanceRecord, IncidentReport, Representative

User = get_user_model()


class RepresentativeAccessTests(BaseVotingTestCase):
    def setUp(self):
        self.election, self.position, self.candidates = self.open_election(with_voters=3)
        self.candidate = self.candidates[0]
        self.rep_user = User.objects.create_user("repuser", password="x")

    # -- helpers ----------------------------------------------------------
    def _apply(self, *, as_user=None, email="rep@example.org", candidate=None):
        if as_user is not None:
            self.client.force_login(as_user)
        return self.client.post(
            reverse("apply", args=[self.election.pk]),
            {
                "candidate": str((candidate or self.candidate).pk),
                "full_name": "Rep One",
                "email": email,
            },
        )

    def _make_rep(self, *, user=None, candidate=None, status=Representative.STATUS_APPROVED):
        return Representative.objects.create(
            election=self.election,
            candidate=candidate or self.candidate,
            user=user,
            full_name="Rep One",
            email="rep@example.org",
            status=status,
        )

    def _cast_one_ballot(self):
        voter = self.election.voters.first()
        issue_authorization(voter, allow_reissue=True, reason="test")
        return cast_ballot(
            election=self.election,
            voter=voter,
            submission_id=prepare_ballot(voter, self.election).submission_id,
            selections={str(self.position.pk): [str(self.candidates[0].pk)]},
        )

    # -- application ------------------------------------------------------
    def test_application_creates_pending_representative(self):
        r = self._apply(as_user=self.rep_user)
        self.assertEqual(r.status_code, 302)
        rep = Representative.objects.get(election=self.election)
        self.assertEqual(rep.status, Representative.STATUS_PENDING)
        self.assertEqual(rep.user_id, self.rep_user.pk)
        self.assertTrue(
            AuditEvent.objects.filter(
                election=self.election, action="representative_applied"
            ).exists()
        )

    def test_duplicate_application_rejected(self):
        self._apply(as_user=self.rep_user)
        r = self._apply(as_user=self.rep_user, email="rep@example.org")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already exists")
        self.assertEqual(
            Representative.objects.filter(election=self.election).count(), 1
        )

    # -- staff decisions --------------------------------------------------
    def test_outsider_cannot_decide_or_accredit(self):
        rep = self._make_rep(status=Representative.STATUS_PENDING)
        self.client.force_login(self.outsider)
        r = self.client.post(
            reverse("decide", args=[rep.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 403)
        rep.refresh_from_db()
        self.assertEqual(rep.status, Representative.STATUS_PENDING)
        r = self.client.post(reverse("accredit", args=[rep.pk]))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_official_approves_and_event_is_audited(self):
        rep = self._make_rep(status=Representative.STATUS_PENDING)
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("decide", args=[rep.pk]), {"decision": "approve"}
        )
        self.assertEqual(r.status_code, 302)
        rep.refresh_from_db()
        self.assertEqual(rep.status, Representative.STATUS_APPROVED)
        self.assertEqual(rep.approved_by_id, self.official.pk)
        self.assertTrue(
            AuditEvent.objects.filter(
                election=self.election, action="representative_approved"
            ).exists()
        )

    def test_accreditation_requires_approved_status(self):
        rep = self._make_rep(status=Representative.STATUS_PENDING)
        self.client.force_login(self.official)
        self.client.post(reverse("accredit", args=[rep.pk]))
        self.assertEqual(AttendanceRecord.objects.count(), 0)
        rep.status = Representative.STATUS_APPROVED
        rep.save(update_fields=["status"])
        self.client.post(reverse("accredit", args=[rep.pk]))
        self.assertEqual(AttendanceRecord.objects.filter(representative=rep).count(), 1)
        rep.refresh_from_db()
        self.assertIsNotNone(rep.accredited_at)

    # -- list / incidents -------------------------------------------------
    def test_representative_cannot_open_management_list(self):
        self._apply(as_user=self.rep_user)
        rep = Representative.objects.get(election=self.election)
        self.client.force_login(self.official)
        self.client.post(reverse("decide", args=[rep.pk]), {"decision": "approve"})
        # staff-only management page refuses a mere representative
        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("representatives_list", args=[self.election.pk]))
        self.assertEqual(r.status_code, 403)
        # ... but the staff of the election may open it
        self.client.force_login(self.official)
        r = self.client.get(reverse("representatives_list", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)

    def test_representative_sees_only_own_incidents(self):
        # approved representative bound to rep_user
        self._apply(as_user=self.rep_user)
        rep = Representative.objects.get(election=self.election)
        self.client.force_login(self.official)
        self.client.post(reverse("decide", args=[rep.pk]), {"decision": "approve"})

        # representative files their own report
        self.client.force_login(self.rep_user)
        self.client.post(
            reverse("incident_create", args=[self.election.pk]),
            {"title": "Rep own report", "description": "queue jam", "category": "incident"},
        )
        # staff files another report
        self.client.force_login(self.official)
        self.client.post(
            reverse("incident_create", args=[self.election.pk]),
            {"title": "Staff internal note", "description": "probe", "category": "incident"},
        )
        self.assertEqual(
            IncidentReport.objects.filter(election=self.election).count(), 2
        )

        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("incident_list", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Rep own report")
        self.assertNotContains(r, "Staff internal note")

        self.client.force_login(self.official)
        r = self.client.get(reverse("incident_list", args=[self.election.pk]))
        self.assertContains(r, "Rep own report")
        self.assertContains(r, "Staff internal note")

    def test_outsider_cannot_report_or_resolve_incidents(self):
        rep = self._make_rep()
        incident = IncidentReport.objects.create(
            election=self.election, title="T", description="D"
        )
        self.client.force_login(self.outsider)
        r = self.client.post(
            reverse("incident_create", args=[self.election.pk]),
            {"title": "x", "description": "y"},
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            reverse("incident_resolve", args=[incident.pk]), {"decision": "resolved"}
        )
        self.assertEqual(r.status_code, 403)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "open")

    def test_staff_can_resolve_incident_with_audit(self):
        incident = IncidentReport.objects.create(
            election=self.election, title="T", description="D"
        )
        self.client.force_login(self.official)
        r = self.client.post(
            reverse("incident_resolve", args=[incident.pk]),
            {"decision": "resolved", "note": "fixed"},
        )
        self.assertEqual(r.status_code, 302)
        incident.refresh_from_db()
        self.assertEqual(incident.status, "resolved")
        self.assertEqual(incident.resolved_by_id, self.official.pk)
        self.assertTrue(
            AuditEvent.objects.filter(
                election=self.election, action="incident_resolved"
            ).exists()
        )

    # -- representative dashboard ----------------------------------------
    def test_dashboard_is_aggregate_only(self):
        result = self._cast_one_ballot()
        self._apply(as_user=self.rep_user)
        rep = Representative.objects.get(election=self.election)
        self.client.force_login(self.official)
        self.client.post(reverse("decide", args=[rep.pk]), {"decision": "approve"})

        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("representative_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["turnout"]["total"], 3)
        self.assertEqual(r.context["turnout"]["voted"], 1)
        body = r.content.decode()
        # no receipt, no voter identity anywhere on this surface
        self.assertNotIn(result.receipt_code, body)
        self.assertNotIn("V001", body)
        # station rows carry only aggregate ballot volumes
        for row in r.context["stations"]:
            self.assertEqual(set(row), {"station", "ballots"})

    def test_dashboard_offers_next_actions_and_report_status(self):
        self._apply(as_user=self.rep_user)
        rep = Representative.objects.get(election=self.election)
        self.client.force_login(self.official)
        self.client.post(reverse("decide", args=[rep.pk]), {"decision": "approve"})
        IncidentReport.objects.create(
            election=self.election,
            reporter=rep,
            title="Ballot box seal broken",
            description="Observed at Main Hall.",
            status="open",
        )

        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("representative_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["open_incidents"], 1)
        # voting is OPEN, so live turnout is withheld from representatives
        self.assertFalse(r.context["live_turnout_visible"])
        body = r.content.decode()
        # the guidance panel links to the rep's allowed surfaces
        for label in (
            "Report an incident",
            "My reports",
            "My live monitor",
            "Browse elections",
        ):
            self.assertIn(label, body)
        for name in ("incident_create", "incident_list", "monitor"):
            self.assertIn(reverse(name, args=[self.election.pk]), body)
        self.assertContains(r, "Ballot box seal broken")

    def test_dashboard_without_approval_renders_empty_state(self):
        self.client.force_login(self.official)  # staff but not a representative
        r = self.client.get(reverse("representative_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.context.get("rep"))

    def test_representative_monitor_access_is_allowed(self):
        """Approved representatives may open the monitor surface (by design)."""
        from organizations.roles import can_view_election

        self._apply(as_user=self.rep_user)
        rep = Representative.objects.get(election=self.election)
        self.client.force_login(self.official)
        self.client.post(reverse("decide", args=[rep.pk]), {"decision": "approve"})
        rep.refresh_from_db()
        self.assertTrue(can_view_election(self.rep_user, self.election))
        self.client.force_login(self.rep_user)
        r = self.client.get(reverse("monitor", args=[self.election.pk]))
        self.assertEqual(r.status_code, 200)
