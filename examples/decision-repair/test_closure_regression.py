"""Regression assertions written before the closure repair. Local fixtures only."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bridge_store import Bridge
from evidence_action import EvidenceEngine
from examples import envelope, contract, proposal, packet, assessment
from test_bridge import encode, fixture_manifest
from vendor.decision_repair import Engine, example_envelope, example_proposal, GateError


class ClosureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def assert_closed(self, engine, identity):
        data = engine.export()
        self.assertEqual(data["envelopes"][0]["status"], "CLOSED")
        self.assertEqual(engine.action(identity)["status"], "FAILED")
        self.assertTrue(any(e["kind"] == "ENVELOPE_CLOSED" for e in data["events"]))
        self.assertEqual(data["envelopes"][0]["used"], 1)

    def close_after_commit(self, engine, identity):
        """Insert the authorized close precisely between existing transactions."""
        original = engine._transaction
        inserted = False

        @contextmanager
        def interleave():
            nonlocal inserted
            with original() as db:
                yield db
            if not inserted:
                inserted = True
                engine.close_envelope(identity, "Explicit close after commitment.")

        engine._transaction = interleave

    def test_base_failure_does_not_replace_closed(self):
        e = Engine(self.root / "base.sqlite")
        e.create_envelope(example_envelope())
        e.propose(example_proposal())
        self.close_after_commit(e, "example")
        e.execute("example-action", "failure")
        self.assert_closed(e, "example-action")
        self.assertEqual(e.propose(example_proposal("next"))["status"], "REJECTED")

    def test_evidence_failure_does_not_replace_closed(self):
        e = EvidenceEngine.initialize(self.root / "evidence.sqlite", envelope(), contract())
        e.propose(proposal())
        e.record_packet(packet())
        e.record_assessment(assessment())
        self.close_after_commit(e, "fixture")
        e.execute("apply-1", "failure")
        self.assert_closed(e, "apply-1")
        self.assertFalse(e.action("apply-1")["next_inert_attempt_permitted"])

    def bridge(self):
        out = self.root / "out"
        out.mkdir()
        self.encoded = encode(fixture_manifest())
        b = Bridge.initialize(self.root / "bridge.sqlite", self.encoded, out, 3, "fixture:closure")
        b.reserve("a", self.encoded)
        return b, b.begin("a", self.encoded)

    @staticmethod
    def event(permit, kind):
        event = {"event_id": kind, "transport": "fake", "kind": kind,
                 "action_id": "a", "nonce": permit["nonce"],
                 "request_id": "fake-" + permit["nonce"]}
        if kind == "completed":
            event.update(success=False, outputs=[])
        return event

    def test_bridge_late_rejection_keeps_closed(self):
        b, p = self.bridge()
        b.close_envelope("dispatch", "Stop new work, retain evidence.")
        self.assertTrue(b.observe("a", self.event(p, "rejected"))["accepted"])
        self.assert_closed(b, "a")
        self.assertEqual(b.reserve("next", self.encoded)["status"], "REJECTED")

    def test_bridge_late_unsuccessful_completion_keeps_closed(self):
        b, p = self.bridge()
        b.observe("a", self.event(p, "accepted"))
        b.close_envelope("dispatch", "Stop new work, retain evidence.")
        self.assertTrue(b.observe("a", self.event(p, "completed"))["accepted"])
        self.assert_closed(b, "a")

    def test_closure_does_not_block_late_acceptance_or_reopen(self):
        b, p = self.bridge()
        b.close_envelope("dispatch", "Stop new work, retain evidence.")
        self.assertTrue(b.observe("a", self.event(p, "accepted"))["accepted"])
        self.assertEqual(b.export()["envelopes"][0]["status"], "CLOSED")
        with self.assertRaises(GateError):
            b.begin("a", self.encoded)


if __name__ == "__main__":
    unittest.main()
