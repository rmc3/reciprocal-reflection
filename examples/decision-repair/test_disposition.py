"""Local lifecycle tests. No source/service/model invocation or real participants."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import json
import sqlite3
import subprocess
import sys
import unittest

from bridge_store import Bridge
from evidence_action import EvidenceEngine
from examples import envelope, contract, proposal, packet, assessment, objection
from test_bridge import encode, fixture_manifest
from vendor.decision_repair import Engine, GateError, example_envelope, example_proposal


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "evidence.sqlite"
        self.e = EvidenceEngine.initialize(self.path, envelope(1), contract())

    def reserve(self):
        return self.e.propose(proposal())

    def ready(self):
        self.reserve()
        self.e.record_packet(packet())
        self.e.record_assessment(assessment())

    def withdraw(self, identity="apply-1"):
        return self.e.withdraw(identity, "fixture-owner", "Do not pursue this proposal.",
                               "Claim remains unresolved; no continuing review promised.")

    def grant(self):
        return self.e.export()["envelopes"][0]

    def test_withdrawal_frees_authorized_inquiry_not_consequence(self):
        self.reserve()
        self.assertEqual(self.e.propose(proposal("too-early", "inspect"))["status"], "REJECTED")
        a = self.withdraw()
        self.assertEqual(a["status"], "WITHDRAWN")
        self.assertFalse(a["attempted"])
        self.assertEqual(self.grant()["used"], 0)
        self.assertEqual(self.e.propose(proposal("inspect-later", "inspect"))["status"], "RESERVED")
        self.e.execute("inspect-later")
        self.assertTrue(self.e.reconcile("inspect-later")["inert_effect_verified"])
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        data = self.e.export()
        self.assertEqual([r["action_id"] for r in data["effects"]], ["inspect-later"])
        self.assertEqual(self.grant()["used"], 1)

    def test_release_uses_frozen_costs_and_burdens_once(self):
        e = Engine(self.root / "base.sqlite")
        e.create_envelope(example_envelope())
        p = example_proposal()
        e.propose(p)
        p["costs"]["tokens"] = 999
        e.withdraw("example-action", "owner", "Withdraw.", "Remaining concern.")
        state = e.export()
        g = state["envelopes"][0]
        self.assertEqual(g["used"], 0)
        self.assertEqual(g["charged"], {"tokens": 0})
        self.assertEqual(g["burden_used"], {"assigned-team": 0})
        self.assertEqual(state["withdrawals"][0]["body"]["released_reservation"]["costs"], {"tokens": 2})
        with self.assertRaises(GateError):
            e.withdraw("example-action", "owner", "Again.", "Again.")
        self.assertEqual(e.export()["envelopes"][0], g)

    def test_withdrawal_preserves_material_undertaking_challenge(self):
        self.reserve()
        self.e.challenge("c", "apply-1", "The whole undertaking is disputed.", "fixture:account", True)
        self.withdraw()
        self.assertEqual(self.e.export()["challenges"][0]["status"], "OPEN")
        self.assertEqual(self.e.propose(proposal("inquiry", "inspect"))["status"], "REJECTED")
        self.assertIn("material challenge", self.e.action("apply-1")["operational_blocking_reasons"][-1])

    def test_withdrawal_does_not_erase_evidence_or_objections(self):
        self.ready()
        self.e.record_objection(objection())
        self.withdraw()
        data = self.e.export()
        self.assertEqual(len(data["packets"]), 1)
        self.assertEqual(len(data["assessments"]), 1)
        self.assertEqual(len(data["objections"]), 1)
        self.assertEqual(data["objections"][0]["body"]["material"], True)
        self.assertFalse(self.e.action("apply-1")["current_evidence_gate"]["mechanical_prerequisites_passed"])

    def test_same_identifier_cannot_be_reproposed(self):
        self.reserve()
        self.withdraw()
        with self.assertRaises(GateError):
            self.reserve()
        self.assertEqual(self.grant()["used"], 0)

    def test_assessment_cannot_reinstate_withdrawn_action(self):
        self.ready()
        self.withdraw()
        with self.assertRaises(GateError):
            self.e.record_assessment(assessment(identity="new"))
        self.assertFalse(self.e.action("apply-1")["next_inert_attempt_permitted"])

    def test_withdrawal_after_closure_does_not_reopen(self):
        self.reserve()
        self.e.close_envelope("fixture", "User ends participation.")
        a = self.withdraw()
        self.assertEqual(a["undertaking_status"], "CLOSED")
        self.assertEqual(a["status"], "WITHDRAWN")
        self.assertEqual(self.grant()["used"], 0)
        self.assertEqual(self.e.propose(proposal("other", "inspect"))["status"], "REJECTED")

    def test_closed_reserved_view_is_explicit_not_pending_review(self):
        self.ready()
        self.e.close_envelope("fixture", "No further work.")
        a = self.e.action("apply-1")
        self.assertEqual(a["status"], "RESERVED")
        self.assertEqual(a["undertaking_status"], "CLOSED")
        self.assertTrue(a["current_evidence_gate"]["mechanical_prerequisites_passed"])
        self.assertFalse(a["next_inert_attempt_permitted"])
        self.assertIn("undertaking is CLOSED", a["blocking_reasons"])
        self.assertEqual(a["review_assignment"], "not_represented")

    def test_evidence_and_closure_blocking_reasons_both_visible(self):
        self.reserve()
        self.e.close_envelope("fixture", "Stop.")
        reasons = self.e.action("apply-1")["blocking_reasons"]
        self.assertIn("undertaking is CLOSED", reasons)
        self.assertIn("no action-specific assessment", reasons)

    def test_attempted_unknown_cannot_release_or_replay(self):
        self.ready()
        self.e.execute("apply-1", "interrupt")
        with self.assertRaises(GateError):
            self.withdraw()
        self.e.close_envelope("fixture", "Stop.")
        a = self.e.reconcile("apply-1")
        self.assertEqual(a["status"], "UNKNOWN")
        self.assertEqual(a["undertaking_status"], "CLOSED")
        self.assertTrue(a["attempted"])
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.e.export()["withdrawals"], [])

    def test_all_attempted_terminal_outcomes_keep_charges(self):
        for mode in ("success", "timeout_before", "failure"):
            with self.subTest(mode=mode):
                self.e = EvidenceEngine.initialize(self.root / (mode + ".sqlite"), envelope(1), contract())
                self.ready()
                self.e.execute("apply-1", mode)
                if mode != "failure":
                    self.e.reconcile("apply-1")
                with self.assertRaises(GateError):
                    self.withdraw()
                self.assertEqual(self.grant()["used"], 1)

    def test_rejected_proposal_cannot_be_withdrawn_for_credit(self):
        p = proposal()
        p["costs"] = {"not-authorized": 1}
        self.assertEqual(self.e.propose(p)["status"], "REJECTED")
        with self.assertRaises(GateError):
            self.withdraw()
        self.assertEqual(self.grant()["used"], 0)

    def test_withdrawal_requires_nonempty_account(self):
        self.reserve()
        for actor, reason, residual in (("", "r", "s"), ("a", "", "s"), ("a", "r", "")):
            with self.subTest(actor=actor, reason=reason, residual=residual), self.assertRaises(GateError):
                self.e.withdraw("apply-1", actor, reason, residual)
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.e.export()["withdrawals"], [])

    def test_restart_preserves_withdrawal_and_released_reservation(self):
        self.reserve()
        self.withdraw()
        restored = EvidenceEngine(self.path)
        a = restored.action("apply-1")
        self.assertEqual(a["status"], "WITHDRAWN")
        self.assertIn("unresolved", a["withdrawal"]["residual"])
        self.assertEqual(restored.export()["envelopes"][0]["used"], 0)
        with self.assertRaises(GateError):
            restored.execute("apply-1")

    def test_two_withdrawals_release_exactly_once(self):
        self.reserve()
        barrier = Barrier(2)

        def withdraw():
            engine = EvidenceEngine(self.path)
            barrier.wait(timeout=5)
            try:
                engine.withdraw("apply-1", "owner", "Withdraw.", "Unresolved.")
                return True
            except GateError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(withdraw) for _ in range(2)]
            self.assertEqual(sum(f.result(timeout=10) for f in futures), 1)
        self.assertEqual(self.grant()["used"], 0)
        self.assertEqual(len(self.e.export()["withdrawals"]), 1)

    def test_withdraw_and_execute_have_exclusive_outcomes(self):
        self.ready()
        barrier = Barrier(2)

        def act(withdraw):
            engine = EvidenceEngine(self.path)
            barrier.wait(timeout=5)
            try:
                if withdraw:
                    engine.withdraw("apply-1", "owner", "Withdraw.", "Unresolved.")
                else:
                    engine.execute("apply-1")
                return True
            except GateError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            w, x = pool.submit(act, True), pool.submit(act, False)
            self.assertEqual(int(w.result(timeout=10)) + int(x.result(timeout=10)), 1)
        data = self.e.export()
        if data["withdrawals"]:
            self.assertEqual(data["effects"], [])
            self.assertEqual(self.grant()["used"], 0)
            self.assertFalse(self.e.action("apply-1")["attempted"])
        else:
            self.assertEqual(len(data["effects"]), 1)
            self.assertEqual(self.grant()["used"], 1)

    def test_reservation_can_be_released_without_erasing_other_unknown(self):
        self.e = EvidenceEngine.initialize(self.root / "two.sqlite", envelope(2), contract())
        self.ready()
        self.e.propose(proposal("spare", "inspect"))
        self.e.execute("apply-1", "interrupt")
        self.e.withdraw("spare", "owner", "Withdraw unattempted sibling.", "Execution remains unknown.")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.e.action("apply-1")["status"], "UNKNOWN")
        self.assertEqual(self.e.propose(proposal("another", "inspect"))["status"], "REJECTED")

    def test_base_revision_cannot_resurrect_withdrawn_action(self):
        e = Engine(self.root / "base.sqlite")
        e.create_envelope(example_envelope())
        e.propose(example_proposal())
        e.challenge("c", "example-action", "Concern", "fixture:account", True)
        e.withdraw("example-action", "owner", "Withdraw.", "Concern remains.")
        with self.assertRaises(GateError):
            e.revise("c", example_proposal("new"), "Replace.", "Unresolved.")
        self.assertEqual(e.export()["challenges"][0]["status"], "OPEN")

    def test_bad_withdrawal_reason_is_not_semantically_detected(self):
        self.reserve()
        a = self.e.withdraw("apply-1", "invented-label", "Convenience is my only reason.", "Nobody was consulted.")
        self.assertEqual(a["status"], "WITHDRAWN")
        self.assertEqual(a["semantic_verification"], "not_performed")
        self.assertEqual(a["withdrawal"]["actor"], "invented-label")

    def test_claim_objection_persists_for_replacement_named_proposal(self):
        self.ready()
        self.e.record_objection(objection())
        self.withdraw()
        p = proposal("new-name")
        self.e.propose(p)
        with self.assertRaises(GateError):
            self.e.record_assessment(assessment(p, identity="new"))
        self.assertFalse(self.e.action("new-name")["next_inert_attempt_permitted"])

    def test_tampered_withdrawal_does_not_report_as_intact(self):
        self.reserve()
        self.withdraw()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE withdrawals SET body='{}'")
        with self.assertRaises(GateError):
            self.e.action("apply-1")

    def test_old_storage_is_refused_without_migration(self):
        # Removing only the marker reproduces the old format's missing version guard.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE disposition_profile")
        before = self.path.read_bytes()
        for cls in (Engine, EvidenceEngine):
            with self.subTest(cls=cls), self.assertRaisesRegex(GateError, "no automatic migration"):
                cls(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def bridge(self):
        out = self.root / "out"
        out.mkdir()
        encoded = encode(fixture_manifest())
        b = Bridge.initialize(self.root / "bridge.sqlite", encoded, out, 1, "fixture:withdrawal")
        return b, encoded

    def test_bridge_withdrawal_prevents_begin_and_releases_only_unused_slot(self):
        b, encoded = self.bridge()
        b.reserve("a", encoded)
        a = b.withdraw("a", "owner", "Withdraw.", "No fake call has occurred.")
        self.assertEqual(a["status"], "WITHDRAWN")
        self.assertFalse(a["next_fake_attempt_permitted"])
        with self.assertRaises(GateError):
            b.begin("a", encoded)
        self.assertEqual(b.reserve("b", encoded)["status"], "RESERVED")
        b.begin("b", encoded)
        with self.assertRaises(GateError):
            b.withdraw("b", "owner", "Withdraw.", "Not allowed after commitment.")
        self.assertEqual(b.export()["envelopes"][0]["used"], 1)

    def test_bridge_old_storage_refused(self):
        b, encoded = self.bridge()
        with sqlite3.connect(b.database) as db:
            db.execute("DROP TABLE disposition_profile")
        before = Path(b.database).read_bytes()
        with self.assertRaisesRegex(GateError, "no automatic migration"):
            Bridge(b.database)
        self.assertEqual(Path(b.database).read_bytes(), before)

    def test_bridge_cli_withdrawal_records_no_dispatch(self):
        b, encoded = self.bridge()
        b.reserve("a", encoded)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("bridge_store.py")),
             "--db", b.database, "withdraw"],
            input=json.dumps({"identity": "a", "actor": "fixture-owner",
                              "reason": "End proposal.", "residual": "No merits determination."}),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["status"], "WITHDRAWN")
        self.assertEqual(b.export()["attempts"], [])
        self.assertFalse((self.root / "out" / "fake-invocations.jsonl").exists())

    def test_withdrawal_cycles_do_not_refund_an_eventual_attempt(self):
        for i in range(3):
            p = proposal("abandoned-" + str(i))
            self.e.propose(p)
            self.e.withdraw(p["id"], "owner", "Withdraw.", "Not attempted.")
        self.ready()
        self.e.execute("apply-1")
        self.e.reconcile("apply-1")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.e.propose(proposal("after", "inspect"))["status"], "REJECTED")
        with self.assertRaises(GateError):
            self.withdraw()
        self.assertEqual(len(self.e.export()["withdrawals"]), 3)

    def test_unknown_storage_version_is_refused(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE disposition_profile SET version=2")
        with self.assertRaisesRegex(GateError, "unsupported disposition"):
            EvidenceEngine(self.path)


if __name__ == "__main__":
    unittest.main()
