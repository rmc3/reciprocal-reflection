"""Mechanical invariants and explicit negative controls, not judgment scores."""
import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from evidence_action import EvidenceEngine, GateError, copy, digest
from examples import envelope, contract, proposal, packet, assessment, objection, demo
from vendor.decision_repair import Engine


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite"
        self.e = EvidenceEngine.initialize(self.path, envelope(), contract())

    def accepted(self, p=None):
        p = proposal() if p is None else p
        self.assertEqual(self.e.propose(p)["status"], "RESERVED")
        return p

    def ready(self, p=None, pack=None, a=None):
        p = self.accepted(p)
        pack = packet() if pack is None else pack
        self.e.record_packet(pack)
        self.e.record_assessment(assessment(p, pack) if a is None else a)
        return p

    def no_effect(self):
        self.assertEqual(self.e.export()["effects"], [])

    def test_missing_assessment_withholds_consequence(self):
        self.accepted()
        with self.assertRaisesRegex(GateError, "no action-specific"):
            self.e.execute("apply-1")
        self.assertFalse(self.e.action("apply-1")["next_inert_attempt_permitted"])
        self.no_effect()

    def test_original_path_and_extended_path_differ_only_as_software(self):
        base = Engine(Path(self.tmp.name) / "old.sqlite")
        base.create_envelope(envelope())
        base.propose(proposal())
        base.execute("apply-1")
        self.assertTrue(base.reconcile("apply-1")["verified"])
        self.accepted()
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        self.no_effect()

    def test_inquiry_completion_does_not_promote_consequence(self):
        self.accepted()
        self.accepted(proposal("inspect-1", "inspect"))
        self.e.execute("inspect-1")
        self.assertTrue(self.e.reconcile("inspect-1")["inert_effect_verified"])
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        self.assertEqual(len(self.e.export()["effects"]), 1)

    def test_sufficient_assessment_allows_only_inert_effect(self):
        self.ready()
        before = self.e.action("apply-1")
        self.assertTrue(before["next_inert_attempt_permitted"])
        self.assertFalse(before["inert_effect_verified"])
        self.assertEqual(before["semantic_verification"], "not_performed")
        self.e.execute("apply-1")
        after = self.e.reconcile("apply-1")
        self.assertTrue(after["inert_effect_verified"])
        self.assertFalse(after["external_delivery"])
        self.assertNotIn("verified", after)

    def test_insufficient_and_unresolved_do_not_authorize(self):
        self.accepted()
        self.e.record_packet(packet())
        for judgment in ("insufficient", "unresolved"):
            with self.subTest(judgment=judgment):
                self.e.record_assessment(assessment(identity=judgment, judgment=judgment))
                with self.assertRaises(GateError):
                    self.e.execute("apply-1")
        self.no_effect()

    def test_routine_flag_cannot_remove_consequence_requirement(self):
        p = proposal()
        self.assertFalse(p["material"])
        self.accepted(p)
        self.assertEqual(self.e.action(p["id"])["current_evidence_gate"]["route"], "consequence")
        with self.assertRaises(GateError):
            self.e.execute(p["id"])

    def test_packet_exact_claim_subject_period_criterion_binding(self):
        for key in contract()["claim"]:
            with self.subTest(key=key):
                p = packet()
                p["claim"][key] += "-different"
                with self.assertRaises(GateError):
                    self.e.record_packet(p)
        self.assertEqual(self.e.export()["packets"], [])

    def test_assessment_exact_binding_and_declared_assessor(self):
        self.accepted()
        self.e.record_packet(packet())
        for key in ("proposal_sha256", "contract_sha256", "claim_id", "packet_id",
                    "packet_sha256", "assessor", "action_id"):
            with self.subTest(key=key):
                a = assessment()
                a[key] = "wrong"
                with self.assertRaises(GateError):
                    self.e.record_assessment(a)
        self.assertEqual(self.e.export()["assessments"], [])

    def test_assessment_cannot_be_copied_to_a_renamed_action(self):
        self.ready()
        self.accepted(proposal("apply-2"))
        with self.assertRaises(GateError):
            self.e.execute("apply-2")
        a = assessment()
        a.update(id="copy-attempt", action_id="apply-2")
        with self.assertRaises(GateError):
            self.e.record_assessment(a)

    def test_current_packet_replaces_reliance_not_history(self):
        self.ready()
        self.e.record_packet(packet("packet-2", evidence_state="conflicting"))
        result = self.e.action("apply-1")
        self.assertIn("evidence packet changed", result["current_evidence_gate"]["blocking_reasons"])
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        self.assertEqual(len(self.e.export()["assessments"]), 1)

    def test_new_assessment_can_use_changed_packet_without_new_dispatch(self):
        self.ready()
        p2 = packet("packet-2")
        self.e.record_packet(p2)
        self.e.record_assessment(assessment(pack=p2, identity="assessment-2"))
        self.assertTrue(self.e.action("apply-1")["next_inert_attempt_permitted"])
        self.no_effect()

    def test_stale_assessment_submission_rejected(self):
        self.accepted()
        self.e.record_packet(packet())
        self.e.record_packet(packet("packet-2"))
        with self.assertRaises(GateError):
            self.e.record_assessment(assessment())

    def test_required_check_not_performed_blocks_even_sufficient_judgment(self):
        self.ready(pack=packet(check_state="not_checked"))
        with self.assertRaisesRegex(GateError, "source check not performed"):
            self.e.execute("apply-1")
        self.no_effect()

    def test_unavailable_and_not_found_never_auto_supply_a_judgment(self):
        self.accepted()
        p = packet(check_state="unavailable", evidence_state="not_found")
        self.e.record_packet(p)
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        saved = self.e.export()["packets"][0]["body"]
        self.assertEqual(saved["checks"][0]["state"], "unavailable")
        self.assertEqual(saved["items"][0]["state"], "not_found")
        self.e.record_assessment(assessment(pack=p, judgment="unresolved"))
        with self.assertRaises(GateError):
            self.e.execute("apply-1")

    def test_declared_evidence_states_remain_distinct(self):
        for state in ("supporting", "conflicting", "not_checked", "not_found", "unavailable"):
            p = packet(state, evidence_state=state)
            self.e.record_packet(p)
        self.assertEqual({r["body"]["items"][0]["state"] for r in self.e.export()["packets"]},
                         {"supporting", "conflicting", "not_checked", "not_found", "unavailable"})

    def test_check_inventory_and_checked_reference_required(self):
        mutations = [
            lambda p: p.update(checks=[]),
            lambda p: p["checks"].append(copy(p["checks"][0])),
            lambda p: p["checks"][0].update(id="wrong"),
            lambda p: p["checks"][0].update(record_ref=""),
        ]
        for mutate in mutations:
            p = packet()
            mutate(p)
            with self.assertRaises(GateError):
                self.e.record_packet(p)

    def test_objection_without_documentary_proof_can_hold(self):
        self.ready()
        self.e.record_objection(objection())
        self.assertEqual(self.e.export()["objections"][0]["body"]["account_ref"], "")
        with self.assertRaisesRegex(GateError, "objections changed"):
            self.e.execute("apply-1")
        self.no_effect()

    def test_advisory_objection_does_not_automatically_veto(self):
        self.ready()
        self.e.record_objection(objection(material=False))
        self.assertTrue(self.e.action("apply-1")["next_inert_attempt_permitted"])
        self.e.execute("apply-1")

    def test_material_objection_does_not_block_explicit_inquiry(self):
        self.ready()
        self.e.record_objection(objection())
        self.accepted(proposal("inspect-1", "inspect"))
        self.e.execute("inspect-1")
        self.e.reconcile("inspect-1")
        with self.assertRaises(GateError):
            self.e.execute("apply-1")

    def test_each_material_objection_needs_response_in_sufficient_assessment(self):
        self.ready()
        self.e.record_objection(objection())
        self.e.record_objection(objection("objection-2"))
        a = assessment(identity="assessment-2", responses=[
            {"objection_id": "objection-1", "response": "An attributed response.", "residual": "Dispute remains."}])
        with self.assertRaises(GateError):
            self.e.record_assessment(a)
        a["objection_responses"].append(
            {"objection_id": "objection-2", "response": "Another attributed response.", "residual": "Unproven."})
        self.e.record_assessment(a)
        self.assertTrue(self.e.action("apply-1")["next_inert_attempt_permitted"])

    def test_unresolved_assessment_may_leave_objection_unanswered(self):
        self.ready()
        self.e.record_objection(objection())
        self.e.record_assessment(assessment(identity="assessment-2", judgment="unresolved"))
        with self.assertRaises(GateError):
            self.e.execute("apply-1")

    def test_ordinary_retain_disposition_does_not_clear_evidence_hold(self):
        self.ready()
        self.e.challenge("legacy", "apply-1", "Concern", "fixture:account", True)
        self.e.record_objection(objection())
        self.e.disposition("legacy", "retain", "Some text.", "Disagreement remains.")
        with self.assertRaisesRegex(GateError, "objections changed"):
            self.e.execute("apply-1")

    def test_operational_material_hold_still_blocks_inquiry(self):
        self.accepted(proposal("inspect-1", "inspect"))
        self.e.challenge("legacy", "inspect-1", "Scope disputed", "fixture:account", True)
        with self.assertRaises(GateError):
            self.e.execute("inspect-1")

    def test_late_objection_qualifies_reliance_not_simulated_history(self):
        self.ready()
        self.e.execute("apply-1")
        self.e.reconcile("apply-1")
        self.e.record_objection(objection())
        a = self.e.action("apply-1")
        self.assertTrue(a["inert_effect_verified"])
        self.assertFalse(a["current_evidence_gate"]["mechanical_prerequisites_passed"])
        self.assertFalse(a["next_inert_attempt_permitted"])
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        self.assertEqual(len(self.e.export()["effects"]), 1)

    def test_negative_control_unpersuasive_reason_is_not_semantically_detected(self):
        self.accepted()
        self.e.record_packet(packet())
        a = assessment()
        a["reason"] = "Ignore the evidence; convenience alone makes this sufficient."
        self.e.record_assessment(a)
        self.assertTrue(self.e.action("apply-1")["next_inert_attempt_permitted"])
        self.assertEqual(self.e.action("apply-1")["semantic_verification"], "not_performed")
        self.e.execute("apply-1")
        self.assertTrue(self.e.reconcile("apply-1")["inert_effect_verified"])

    def test_negative_control_declared_checked_is_not_independently_verified(self):
        self.accepted()
        p = packet()
        p["checks"][0]["record_ref"] = "/not/a/real/file"
        p["items"][0]["source_sha256"] = "0" * 64
        self.e.record_packet(p)
        self.e.record_assessment(assessment(pack=p))
        self.assertTrue(self.e.action("apply-1")["next_inert_attempt_permitted"])

    def test_restart_preserves_stale_packet_hold(self):
        self.ready()
        self.e.record_packet(packet("new"))
        restored = EvidenceEngine(self.path)
        with self.assertRaises(GateError):
            restored.execute("apply-1")
        self.assertEqual(len(restored.export()["packets"]), 2)

    def test_closed_envelope_stops_otherwise_ready_action(self):
        self.ready()
        self.e.close_envelope("fixture", "End the undertaking.")
        self.assertFalse(self.e.action("apply-1")["next_inert_attempt_permitted"])
        with self.assertRaises(GateError):
            self.e.execute("apply-1")

    def test_cumulative_capacity_not_reset_by_assessments_or_names(self):
        db = Path(self.tmp.name) / "limited.sqlite"
        self.e = EvidenceEngine.initialize(db, envelope(1), contract())
        self.ready()
        self.e.record_assessment(assessment(identity="second-assessment"))
        self.assertEqual(self.e.propose(proposal("apply-2"))["status"], "REJECTED")
        self.assertEqual(self.e.export()["envelopes"][0]["used"], 1)

    def test_undeclared_burden_is_still_rejected(self):
        p = proposal()
        p["burdens"] = [{"party": "absent-person", "unit": "minutes", "amount": 1}]
        self.assertEqual(self.e.propose(p)["status"], "REJECTED")
        self.no_effect()

    def test_unknown_execution_blocks_inquiry_and_is_not_replayed(self):
        self.ready()
        self.e.execute("apply-1", "timeout_after")
        self.assertEqual(self.e.propose(proposal("inspect-1", "inspect"))["status"], "REJECTED")
        with self.assertRaises(GateError):
            self.e.execute("apply-1")
        self.e.reconcile("apply-1")
        self.assertEqual(len(self.e.export()["effects"]), 1)

    def test_reinitialization_and_legacy_database_rejected(self):
        with self.assertRaises(FileExistsError):
            EvidenceEngine.initialize(self.path, envelope(), contract())
        old = Path(self.tmp.name) / "legacy.sqlite"
        Engine(old)
        with self.assertRaises(GateError):
            EvidenceEngine(old)

    def test_contract_route_coverage_is_exact(self):
        for index, change in enumerate(("missing", "duplicate", "unknown", "boolean_schema")):
            c = contract()
            if change == "missing":
                c["routes"].pop()
            elif change == "duplicate":
                c["routes"].append(copy(c["routes"][0]))
            elif change == "unknown":
                c["routes"][0]["mode"] = "bypass"
            else:
                c["schema"] = True
            with self.assertRaises(GateError):
                EvidenceEngine.initialize(Path(self.tmp.name) / f"bad-{index}.sqlite", envelope(), c)

    def test_initializer_cannot_claim_live_authority(self):
        e = envelope()
        e["approval_ref"] = "real:consent"
        with self.assertRaises(GateError):
            EvidenceEngine.initialize(Path(self.tmp.name) / "bad.sqlite", e, contract())

    def test_unknown_proposal_fields_and_scope_cannot_bypass(self):
        p = proposal()
        p["evidence_verified"] = True
        with self.assertRaises(GateError):
            self.e.propose(p)
        p = proposal()
        p["kind"] = "different"
        self.assertEqual(self.e.propose(p)["status"], "REJECTED")

    def test_unknown_record_fields_and_bad_shapes_refused(self):
        p = packet()
        p["evidence_verified"] = True
        with self.assertRaises(GateError):
            self.e.record_packet(p)
        for key, val in (("state", "true"), ("source_sha256", 17)):
            p = packet()
            p["items"][0][key] = val
            with self.assertRaises(GateError):
                self.e.record_packet(p)
        self.accepted()
        self.e.record_packet(packet())
        for key, val in (("judgment", "approved"), ("reason", ""), ("objection_responses", {})):
            a = assessment()
            a[key] = val
            with self.assertRaises(GateError):
                self.e.record_assessment(a)

    def test_no_overwriting_records(self):
        self.ready()
        with self.assertRaises(GateError):
            self.e.record_packet(packet())
        with self.assertRaises(GateError):
            self.e.record_assessment(assessment())
        self.e.record_objection(objection())
        with self.assertRaises(GateError):
            self.e.record_objection(objection())

    def test_invalid_objection_metadata_refused(self):
        for key, val in (("claim_id", "wrong"), ("classified_by", "stranger"), ("material", 1)):
            o = objection()
            o[key] = val
            with self.assertRaises(GateError):
                self.e.record_objection(o)

    def test_tampered_packet_is_not_accepted_as_intact(self):
        self.ready()
        with sqlite3.connect(self.path) as db:
            p = packet()
            p["items"][0]["supports"] = "Tampered"
            db.execute("UPDATE packets SET body=?", (json.dumps(p),))
        with self.assertRaisesRegex(GateError, "integrity"):
            self.e.execute("apply-1")
        self.no_effect()

    def test_tampered_contract_refused_on_restart(self):
        with sqlite3.connect(self.path) as db:
            c = contract()
            c["routes"][1]["mode"] = "inquiry"
            db.execute("UPDATE evidence_profile SET body=?", (json.dumps(c),))
        with self.assertRaisesRegex(GateError, "integrity"):
            EvidenceEngine(self.path)

    def test_generic_mutation_and_live_mode_unavailable(self):
        with self.assertRaises(GateError):
            self.e.create_envelope(envelope())
        with self.assertRaises(GateError):
            self.e.revise("x", proposal(), "reason", "residual")
        self.ready()
        with self.assertRaises(GateError):
            self.e.execute("apply-1", "live")

    def test_concurrent_attempts_commit_only_one_effect(self):
        self.ready()
        def start(_):
            try:
                EvidenceEngine(self.path).execute("apply-1")
                return "started"
            except GateError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(start, range(2)))
        self.assertEqual(sorted(outcomes), ["blocked", "started"])
        self.assertEqual(len(self.e.export()["effects"]), 1)
        attempts = [e for e in self.e.export()["events"] if e["kind"] == "ATTEMPTED"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["body"]["evidence_gate_at_begin"]["assessment_id"], "assessment-1")

    def test_demo_preserves_mechanical_and_semantic_separation(self):
        d = demo()
        self.assertFalse(d["initial"]["next_inert_attempt_permitted"])
        self.assertFalse(d["after_inquiry"]["next_inert_attempt_permitted"])
        self.assertTrue(d["assessment_recorded"]["next_inert_attempt_permitted"])
        self.assertTrue(d["late_objection"]["inert_effect_verified"])
        self.assertFalse(d["late_objection"]["current_evidence_gate"]["mechanical_prerequisites_passed"])
        self.assertFalse(d["ledger"]["external_effects"])

    def test_foreign_envelope_is_rejected_at_api_boundary(self):
        p = proposal()
        p["envelope_id"] = "foreign"
        with self.assertRaises(GateError):
            self.e.propose(p)
        self.assertEqual(self.e.export()["actions"], [])

    def test_latest_unresolved_assessment_overrides_earlier_sufficient_one(self):
        self.ready()
        self.e.record_assessment(assessment(identity="later", judgment="unresolved"))
        self.assertEqual(self.e.action("apply-1")["current_evidence_gate"]["recorded_judgment"], "unresolved")
        with self.assertRaises(GateError):
            self.e.execute("apply-1")

    def test_inquiry_cannot_supply_consequence_assessment(self):
        p = self.accepted(proposal("inspect-1", "inspect"))
        self.e.record_packet(packet())
        with self.assertRaises(GateError):
            self.e.record_assessment(assessment(p=p))

    def test_tampered_assessment_fails_before_effect(self):
        self.ready()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE assessments SET digest=?", ("0" * 64,))
        with self.assertRaisesRegex(GateError, "integrity"):
            self.e.execute("apply-1")
        self.no_effect()

    def test_evidence_change_and_begin_are_serialized_not_retroactive_cancellation(self):
        self.ready()
        entered, release, writer_started = threading.Event(), threading.Event(), threading.Event()
        original = self.e._gate

        def paused_gate(db, row):
            result = original(db, row)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")
            return result

        def change_packet():
            writer_started.set()
            return EvidenceEngine(self.path).record_packet(packet("packet-2"))

        self.e._gate = paused_gate  # Test-only interleaving; not an adapter API.
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempt = pool.submit(self.e.execute, "apply-1")
            try:
                self.assertTrue(entered.wait(5))
                writer = pool.submit(change_packet)
                self.assertTrue(writer_started.wait(5))
                self.assertFalse(writer.done())
            finally:
                release.set()
            attempt.result(timeout=10)
            writer.result(timeout=10)
        self.e._gate = original
        events = self.e.export()["events"]
        begin = next(e["sequence"] for e in events if e["kind"] == "ATTEMPTED")
        update = next(e["sequence"] for e in events
                      if e["kind"] == "EVIDENCE_PACKET_RECORDED" and e["subject"] == "packet-2")
        self.assertLess(begin, update)
        self.assertEqual(len(self.e.export()["effects"]), 1)
        result = self.e.reconcile("apply-1")
        self.assertTrue(result["inert_effect_verified"])
        self.assertFalse(result["current_evidence_gate"]["mechanical_prerequisites_passed"])

    def test_all_original_simulation_modes_retain_their_narrow_meaning(self):
        expected = {"success": "VERIFIED", "timeout_before": "NOT_APPLIED",
                    "timeout_after": "VERIFIED", "returned_only": "UNKNOWN",
                    "interrupt": "UNKNOWN", "failure": "FAILED", "contradictory": "UNKNOWN"}
        for mode, status in expected.items():
            with self.subTest(mode=mode):
                self.e = EvidenceEngine.initialize(
                    Path(self.tmp.name) / f"{mode}.sqlite", envelope(), contract())
                self.ready()
                returned = self.e.execute("apply-1", mode)
                result = returned if mode == "failure" else self.e.reconcile("apply-1")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["semantic_verification"], "not_performed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
