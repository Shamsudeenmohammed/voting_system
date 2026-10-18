"""Audit chain tests: append, verification, tamper detection, checkpoints."""
from __future__ import annotations

from django.test import TestCase

from elections.models import Election
from elections.tests import BaseVotingTestCase

from . import services
from .models import AuditChain, AuditCheckpoint, AuditEvent


class AuditChainTests(BaseVotingTestCase):
    def _election_with_events(self, n=5) -> Election:
        election = self.make_election()
        for i in range(n):
            services.record(
                action="test_event",
                actor=self.official,
                election=election,
                details={"i": i},
            )
        return election

    def test_chain_grows_and_verifies(self):
        election = self._election_with_events()
        report = services.verify_chain(f"election:{election.pk}")
        self.assertTrue(report["ok"])
        self.assertEqual(report["events"], 5)
        self.assertEqual(report["head_seq"], 5)

    def test_tampered_event_detected(self):
        election = self._election_with_events()
        event = AuditEvent.objects.filter(chain_key=f"election:{election.pk}")[2]
        event.details = {"i": 999}
        event.save(update_fields=["details"])
        report = services.verify_chain(f"election:{election.pk}")
        self.assertFalse(report["ok"])
        self.assertEqual(report["first_bad_seq"], event.seq)

    def test_deleted_event_detected(self):
        election = self._election_with_events()
        event = AuditEvent.objects.filter(chain_key=f"election:{election.pk}")[2]
        seq = event.seq
        event.delete()
        report = services.verify_chain(f"election:{election.pk}")
        self.assertFalse(report["ok"])
        self.assertEqual(report["first_bad_seq"], seq + 1)

    def test_forbidden_detail_keys_rejected(self):
        election = self.make_election()
        for key in ("selections", "ballot", "token", "receipt_code", "password"):
            with self.assertRaises(ValueError):
                services.record(
                    action="bad",
                    election=election,
                    details={key: "should never be stored"},
                )

    def test_checkpoint_is_signed_and_verifiable(self):
        election = self._election_with_events(3)
        cp = services.create_checkpoint(f"election:{election.pk}", actor=self.admin)
        self.assertTrue(services.verify_checkpoint(cp))
        self.assertEqual(cp.seq, 3)
        # tampering with the checkpoint payload invalidates the signature
        cp.head_hash = "f" * 64
        cp.save(update_fields=["head_hash"])
        self.assertFalse(services.verify_checkpoint(cp))

    def test_export_structure(self):
        election = self._election_with_events(3)
        body = services.export_chain(f"election:{election.pk}")
        self.assertEqual(len(body["events"]), 3)
        self.assertEqual(body["head"]["seq"], 3)
        for ev in body["events"]:
            # exports carry the verification material an independent reviewer
            # needs, and never any ballot/identity data
            for field in ("seq", "prev_hash", "event_hash", "created_at", "action"):
                self.assertIn(field, ev)
            self.assertNotIn("selections", ev["details"])

    def test_chain_key_isolation(self):
        e1 = self._election_with_events(2)
        e2 = self.make_election(title="Second")
        services.record(action="x", election=e2)
        self.assertEqual(services.verify_chain(f"election:{e1.pk}")["events"], 2)
        self.assertEqual(services.verify_chain(f"election:{e2.pk}")["events"], 1)
