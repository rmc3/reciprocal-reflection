"""Local software tests. No model calls, device access or service adapters."""

import json
import random
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from decision_repair import Engine, GateError, demo, example_envelope, example_proposal


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="decision-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.engine = Engine(self.path)
        self.engine.create_envelope(example_envelope())

    def proposal(self, identity="a", **changes):
        proposal = example_proposal(identity)
        proposal.update(changes)
        return proposal

    def state(self):
        return self.engine.export()

    def grant(self):
        return self.state()["envelopes"][0]

    def accepted(self, identity="a", **changes):
        p = self.proposal(identity, **changes)
        self.assertEqual(self.engine.propose(p)["status"], "RESERVED")
        return p

    def evidence_free(self):
        self.assertEqual(self.state()["effects"], [])

    def test_permitted_path_returns_then_verifies_inert_effect(self):
        self.accepted()
        returned = self.engine.execute("a")
        self.assertEqual(returned["status"], "RETURNED")
        self.assertFalse(returned["verified"])
        self.assertTrue(returned["attempted"])
        verified = self.engine.reconcile("a")
        self.assertEqual(verified["status"], "VERIFIED")
        self.assertFalse(verified["external_delivery"])
        self.assertFalse(self.state()["external_effects"])

    def test_routine_path_needs_no_essay_or_further_approval(self):
        self.accepted(material=False, reasons={}, consequences=[])
        self.engine.execute("a")
        self.assertTrue(self.engine.reconcile("a")["verified"])
        self.assertEqual(self.state()["challenges"], [])

    def test_closed_envelope_rejects_renamed_verification(self):
        self.engine.close_envelope("example", "window complete")
        p = self.proposal("verification-under-new-name")
        p["reasons"]["benefit"] = "Helpful continuation"
        self.assertEqual(self.engine.propose(p)["status"], "REJECTED")
        self.assertEqual(self.grant()["used"], 0)
        self.evidence_free()

    def test_closure_rechecked_after_reservation(self):
        self.accepted()
        self.engine.close_envelope("example", "withdrawal before execution")
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.assertFalse(self.engine.action("a")["attempted"])
        self.evidence_free()

    def test_attempt_limit_is_cumulative_not_reset_by_action_id(self):
        for index in range(3):
            self.accepted(str(index), costs={}, burdens=[], material=False,
                          reasons={}, consequences=[])
        self.assertEqual(self.engine.propose(self.proposal("fourth"))["status"], "REJECTED")
        self.assertEqual(self.grant()["used"], 3)

    def test_envelope_id_cannot_be_recreated_to_reset_count(self):
        self.accepted()
        with self.assertRaises(GateError):
            self.engine.create_envelope(example_envelope())
        self.assertEqual(self.grant()["used"], 1)

    def test_duplicate_action_does_not_replay_or_recharge(self):
        p = self.accepted()
        with self.assertRaises(GateError):
            self.engine.propose(p)
        self.engine.execute("a")
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(len(self.state()["effects"]), 1)

    def test_resource_budget_is_cumulative(self):
        self.accepted("one", costs={"tokens": 6})
        result = self.engine.propose(self.proposal("two", costs={"tokens": 5}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("resource limit", result["note"])
        self.assertEqual(self.grant()["charged"], {"tokens": 6})

    def test_unlisted_resource_is_not_an_implicit_allowance(self):
        result = self.engine.propose(self.proposal(costs={"new-budget": 1}))
        self.assertEqual(result["status"], "REJECTED")

    def test_undeclared_party_cannot_supply_capacity(self):
        p = self.proposal(burdens=[{"party": "residents", "unit": "minutes", "amount": 1}])
        p["consequences"][0]["party"] = "residents"
        result = self.engine.propose(p)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.grant()["used"], 0)
        self.evidence_free()

    def test_duplicate_burden_rows_cannot_evade_capacity(self):
        p = self.proposal(burdens=[
            {"party": "assigned-team", "unit": "minutes", "amount": 40},
            {"party": "assigned-team", "unit": "minutes", "amount": 30}])
        self.assertEqual(self.engine.propose(p)["status"], "REJECTED")

    def test_burden_capacity_is_cumulative(self):
        self.accepted("one", burdens=[{"party": "assigned-team", "unit": "minutes", "amount": 40}])
        p = self.proposal("two", burdens=[{"party": "assigned-team", "unit": "minutes", "amount": 21}])
        self.assertEqual(self.engine.propose(p)["status"], "REJECTED")

    def test_units_cannot_be_silently_changed(self):
        p = self.proposal(burdens=[{"party": "assigned-team", "unit": "hours", "amount": 1}])
        self.assertEqual(self.engine.propose(p)["status"], "REJECTED")

    def test_scope_dimensions_are_checked(self):
        for key, value in (("purpose", "different-purpose"), ("kind", "publish"),
                           ("target", "different-target"), ("audience", "public"),
                           ("horizon", "recurring")):
            with self.subTest(key=key):
                p = self.proposal(key, **{key: value})
                self.assertEqual(self.engine.propose(p)["status"], "REJECTED")

    def test_scope_permissions_do_not_form_an_unapproved_cross_product(self):
        e = example_envelope("other")
        e["permissions"].append({"kind": "read", "target": "public-file",
                                  "audience": "public", "horizon": "one-task"})
        self.engine.create_envelope(e)
        p = self.proposal(envelope_id="other", audience="public")
        self.assertEqual(self.engine.propose(p)["status"], "REJECTED")

    def test_candidate_reference_is_preserved_but_does_not_grant_permission(self):
        p = self.proposal(audience="public")
        p["references"] = [{"source": "candidate-permission", "version": "1", "standing": "candidate"}]
        result = self.engine.propose(p)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["proposal"]["references"][0]["standing"], "candidate")

    def test_proposed_authority_is_not_loadable_as_authorized(self):
        e = example_envelope("candidate")
        e["standing"] = "candidate"
        with self.assertRaises(GateError):
            self.engine.create_envelope(e)

    def test_acceptance_reference_required_for_capacity(self):
        e = example_envelope("invalid")
        e["accepted_burdens"]["assigned-team"]["acceptance_ref"] = ""
        with self.assertRaises(GateError):
            self.engine.create_envelope(e)

    def test_quantities_reject_negative_boolean_and_fractional_values(self):
        for value in (-1, True, 0.5, "1"):
            with self.subTest(value=value):
                with self.assertRaises(GateError):
                    self.engine.propose(self.proposal(costs={"tokens": value}))
        self.assertEqual(self.state()["actions"], [])

    def test_unknown_fields_do_not_silently_change_meaning(self):
        p = self.proposal()
        p["override_permission"] = True
        with self.assertRaises(GateError):
            self.engine.propose(p)

    def test_material_reason_and_consequence_account_required(self):
        for change in ({"reasons": {}}, {"consequences": []},
                       {"consequences": [{"party": "other", "benefit": "b", "burden": "b",
                                          "unserved": "u", "unknown": "u"}]}):
            with self.subTest(change=change):
                with self.assertRaises(GateError):
                    self.engine.propose(self.proposal(**change))

    def test_timeout_after_effect_blocks_renamed_retry_until_reconciled(self):
        self.accepted()
        self.assertEqual(self.engine.execute("a", "timeout_after")["status"], "UNKNOWN")
        self.assertEqual(self.engine.propose(self.proposal("renamed"))["status"], "REJECTED")
        self.assertEqual(len(self.state()["effects"]), 1)
        self.assertEqual(self.engine.reconcile("a")["status"], "VERIFIED")
        self.assertEqual(self.grant()["used"], 1)
        self.accepted("next")

    def test_timeout_before_effect_requires_terminal_proof_and_keeps_attempt_charge(self):
        self.accepted()
        self.engine.execute("a", "timeout_before")
        self.assertEqual(self.engine.reconcile("a")["status"], "NOT_APPLIED")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.grant()["charged"], {"tokens": 2})
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.evidence_free()

    def test_missing_receipt_is_not_proof_of_non_execution(self):
        self.accepted()
        self.engine.execute("a", "interrupt")
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")
        restarted = Engine(self.path)
        self.assertEqual(restarted.reconcile("a")["status"], "UNKNOWN")
        self.assertEqual(restarted.propose(self.proposal("renamed"))["status"], "REJECTED")

    def test_returned_without_matching_evidence_is_not_verified(self):
        self.accepted()
        returned = self.engine.execute("a", "returned_only")
        self.assertEqual(returned["status"], "RETURNED")
        self.assertFalse(returned["verified"])
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")

    def test_contradictory_effect_is_not_verified(self):
        self.accepted()
        self.engine.execute("a", "contradictory")
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")
        self.assertEqual(self.engine.propose(self.proposal("repair"))["status"], "REJECTED")

    def test_effect_body_must_match_its_hash_not_only_the_hash_column(self):
        self.accepted()
        self.engine.execute("a")
        with sqlite3.connect(self.path) as db:
            body = json.loads(db.execute("SELECT body FROM effects WHERE action_id='a'").fetchone()[0])
            body["proposal"]["audience"] = "tampered"
            db.execute("UPDATE effects SET body=? WHERE action_id='a'", (json.dumps(body),))
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")

    def test_reported_failure_halts_when_envelope_requires_it(self):
        self.accepted()
        self.assertEqual(self.engine.execute("a", "failure")["status"], "FAILED")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.grant()["status"], "HALTED")
        self.assertEqual(self.engine.propose(self.proposal("next"))["status"], "REJECTED")

    def test_known_failure_can_allow_distinct_work_only_when_envelope_allows(self):
        e = example_envelope("non-stopping")
        e["stop_on_failure"] = False
        self.engine.create_envelope(e)
        self.accepted(envelope_id="non-stopping")
        self.engine.execute("a", "failure")
        self.accepted("next", envelope_id="non-stopping")
        with self.assertRaises(GateError):
            self.engine.execute("a")

    def test_advisory_objection_is_preserved_without_automatic_veto(self):
        self.accepted()
        self.engine.challenge("c", "a", "Consider another framing.")
        self.engine.execute("a")
        self.assertEqual(self.state()["challenges"][0]["status"], "OPEN")
        self.assertEqual(self.engine.reconcile("a")["status"], "VERIFIED")

    def test_material_objection_blocks_pending_and_dependent_actions(self):
        self.accepted()
        self.engine.challenge("c", "a", "The capacity fact is disputed.", "fixture-evidence", True)
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.assertEqual(self.engine.propose(self.proposal("dependent"))["status"], "REJECTED")
        self.evidence_free()

    def test_material_challenge_needs_evidence_reference(self):
        self.accepted()
        with self.assertRaises(GateError):
            self.engine.challenge("c", "a", "Claim", material=True)

    def test_retain_requires_reasoned_disposition_not_acknowledgment_only(self):
        self.accepted()
        self.engine.challenge("c", "a", "Capacity dispute", "evidence", True)
        with self.assertRaises(GateError):
            self.engine.disposition("c", "retain", "", "Unresolved interests remain.")
        self.engine.disposition("c", "retain", "Recorded authority confirms this allocation.",
                                "The challenger still disagrees with the priority.")
        self.engine.execute("a")
        c = self.state()["challenges"][0]
        self.assertEqual(c["status"], "RESOLVED")
        self.assertIn("disagrees", c["residual"])

    def test_pause_and_seek_authority_preserve_hold(self):
        self.accepted()
        self.engine.challenge("c", "a", "Concern")
        for choice in ("pause", "seek_authority"):
            self.engine.disposition("c", choice, "A relevant fact is unresolved.", "No effect yet.")
            with self.assertRaises(GateError):
                self.engine.execute("a")
        self.evidence_free()

    def test_revision_before_attempt_releases_only_unused_reservation(self):
        e = example_envelope("one", 1)
        self.engine.create_envelope(e)
        self.accepted(envelope_id="one", costs={"tokens": 8})
        self.engine.challenge("c", "a", "Use fewer tokens.", "budget-evidence", True)
        replacement = self.proposal("replacement", envelope_id="one", costs={"tokens": 9})
        self.assertEqual(self.engine.revise("c", replacement, "Replace pending plan.", "No prior effect.")["status"],
                         "RESERVED")
        self.assertEqual(self.engine.action("a")["status"], "SUPERSEDED")
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.engine.execute("replacement")
        grants = {r["id"]: r for r in self.state()["envelopes"]}
        self.assertEqual(grants["one"]["used"], 1)
        self.assertEqual(grants["one"]["charged"], {"tokens": 9})

    def test_out_of_scope_repair_is_rejected_without_clearing_challenge(self):
        self.accepted()
        self.engine.challenge("c", "a", "Change audience", "evidence", True)
        result = self.engine.revise("c", self.proposal("repair", audience="public"),
                                    "Proposed widening", "No public permission.")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.state()["challenges"][0]["status"], "OPEN")
        self.assertEqual(self.engine.action("a")["status"], "RESERVED")
        self.assertEqual(self.grant()["used"], 1)

    def test_repair_cannot_replace_its_authorization_envelope(self):
        self.accepted()
        self.engine.challenge("c", "a", "Concern", "evidence", True)
        self.engine.create_envelope(example_envelope("other"))
        with self.assertRaises(GateError):
            self.engine.revise("c", self.proposal("repair", envelope_id="other"), "Reason", "Residual")

    def test_post_effect_repair_is_separate_and_does_not_erase_original(self):
        self.accepted()
        self.engine.execute("a")
        self.engine.reconcile("a")
        self.engine.challenge("c", "a", "A bounded further repair is needed.", "evidence", True)
        result = self.engine.revise("c", self.proposal("repair"), "Separate permitted effect.", "Original remains.")
        self.assertEqual(result["status"], "RESERVED")
        self.assertEqual(self.engine.action("a")["status"], "VERIFIED")
        self.assertEqual(self.grant()["used"], 2)
        self.assertEqual(len(self.state()["effects"]), 1)

    def test_unknown_execution_cannot_be_repaired_into_a_replay(self):
        self.accepted()
        self.engine.execute("a", "interrupt")
        self.engine.challenge("c", "a", "Try again", "evidence", True)
        result = self.engine.revise("c", self.proposal("retry"), "Maybe retry", "Execution is unknown.")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.engine.action("a")["status"], "UNKNOWN")

    def test_resolving_one_challenge_does_not_erase_another(self):
        self.accepted()
        self.engine.challenge("one", "a", "Concern one", "evidence-one", True)
        self.engine.challenge("two", "a", "Concern two", "evidence-two", True)
        self.engine.disposition("one", "retain", "Reasoned disposition", "Residual")
        with self.assertRaises(GateError):
            self.engine.execute("a")

    def test_candidate_material_cannot_become_envelope_authority_by_repair(self):
        self.accepted()
        self.engine.challenge("c", "a", "Broader policy proposed.", "candidate-doc", True)
        p = self.proposal("repair", horizon="recurring")
        p["references"] = [{"source": "candidate-doc", "version": "2", "standing": "candidate"}]
        self.assertEqual(self.engine.revise("c", p, "Proposal only", "No adoption.")["status"], "REJECTED")

    def test_tampered_envelope_fails_before_execution(self):
        self.accepted()
        with sqlite3.connect(self.path) as db:
            e = example_envelope()
            e["max_attempts"] = 999
            db.execute("UPDATE envelopes SET body=? WHERE id='example'", (json.dumps(e),))
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.evidence_free()

    def test_tampered_proposal_fails_before_execution(self):
        self.accepted()
        with sqlite3.connect(self.path) as db:
            p = self.proposal(audience="public")
            db.execute("UPDATE actions SET body=? WHERE id='a'", (json.dumps(p),))
        with self.assertRaises(GateError):
            self.engine.execute("a")
        self.evidence_free()

    def test_restart_preserves_allowances_actions_and_challenges(self):
        self.accepted()
        self.engine.challenge("c", "a", "Unresolved", "evidence", True)
        restarted = Engine(self.path)
        self.assertEqual(restarted.export(), self.state())
        with self.assertRaises(GateError):
            restarted.execute("a")

    def test_competing_reservations_cannot_each_take_last_slot(self):
        e = example_envelope("one", 1)
        self.engine.create_envelope(e)
        barrier = Barrier(12)

        def reserve(index):
            barrier.wait()
            return self.engine.propose(self.proposal(str(index), envelope_id="one"))["status"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(reserve, range(12)))
        self.assertEqual(statuses.count("RESERVED"), 1)
        self.assertEqual(statuses.count("REJECTED"), 11)

    def test_competing_execution_of_same_id_has_one_inert_effect(self):
        self.accepted()
        barrier = Barrier(2)

        def execute(_):
            barrier.wait()
            try:
                return self.engine.execute("a")["status"]
            except GateError:
                return "BLOCKED"

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(execute, range(2)))
        self.assertEqual(sorted(statuses), ["BLOCKED", "RETURNED"])
        self.assertEqual(len(self.state()["effects"]), 1)
        self.assertEqual(sum(e["kind"] == "ATTEMPTED" for e in self.state()["events"]), 1)

    def test_seeded_sequence_keeps_cumulative_caps(self):
        e = example_envelope("sequence", 15)
        e["resource_limits"]["tokens"] = 31
        self.engine.create_envelope(e)
        rng = random.Random(61002)
        reserved = []
        for index in range(80):
            cost, minutes = rng.randrange(0, 8), rng.randrange(0, 16)
            p = self.proposal(str(index), envelope_id="sequence", costs={"tokens": cost},
                              burdens=[{"party": "assigned-team", "unit": "minutes", "amount": minutes}])
            if self.engine.propose(p)["status"] == "RESERVED":
                reserved.append((cost, minutes))
        self.assertLessEqual(len(reserved), 15)
        self.assertLessEqual(sum(c for c, _ in reserved), 31)
        self.assertLessEqual(sum(m for _, m in reserved), 60)
        self.assertGreater(len(reserved), 0)

    def test_target_strings_are_never_opened_or_written(self):
        target = Path(self.temp.name) / "must-not-exist.txt"
        e = example_envelope("path")
        e["permissions"][0]["target"] = str(target)
        self.engine.create_envelope(e)
        self.accepted(envelope_id="path", target=str(target))
        self.engine.execute("a")
        self.engine.reconcile("a")
        self.assertFalse(target.exists())

    def test_demo_is_inert_and_retains_rejected_proposals(self):
        result = demo()
        statuses = {a["id"]: a["status"] for a in result["actions"]}
        self.assertEqual(statuses, {"unaccepted": "REJECTED", "ordinary": "VERIFIED",
                                    "uncertain": "VERIFIED", "renamed-retry": "REJECTED"})
        self.assertFalse(result["external_effects"])
        self.assertEqual(len(result["effects"]), 2)

    def test_malformed_effect_content_leaves_unknown_not_verified(self):
        self.accepted()
        self.engine.execute("a")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE effects SET body='not-json' WHERE action_id='a'")
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")

    def test_effect_without_terminal_run_is_not_completion_proof(self):
        self.accepted()
        self.engine.execute("a")
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM runs WHERE action_id='a'")
        self.assertEqual(self.engine.reconcile("a")["status"], "UNKNOWN")

    def test_unknown_execution_blocks_an_already_reserved_next_action(self):
        self.accepted("one")
        self.accepted("two")
        self.engine.execute("one", "timeout_after")
        with self.assertRaises(GateError):
            self.engine.execute("two")
        self.engine.reconcile("one")
        self.assertEqual(self.engine.execute("two")["status"], "RETURNED")

    def test_unverified_return_blocks_renaming_and_reserved_continuation(self):
        self.accepted("one")
        self.accepted("two")
        self.engine.execute("one", "returned_only")
        self.assertEqual(self.engine.propose(self.proposal("renamed"))["status"], "REJECTED")
        with self.assertRaises(GateError):
            self.engine.execute("two")
        self.assertEqual(self.engine.reconcile("one")["status"], "UNKNOWN")

    def test_success_return_requires_reconciliation_before_next_effect(self):
        self.accepted("one")
        self.accepted("two")
        self.engine.execute("one")
        with self.assertRaises(GateError):
            self.engine.execute("two")
        self.engine.reconcile("one")
        self.assertEqual(self.engine.execute("two")["status"], "RETURNED")

    def test_failed_revision_does_not_release_the_original_reservation(self):
        self.accepted()
        self.engine.challenge("c", "a", "Alternative", "evidence", True)
        result = self.engine.revise("c", self.proposal("too-large", costs={"tokens": 11}),
                                    "Try another plan", "No new authority")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.engine.action("a")["status"], "RESERVED")
        self.assertEqual(self.grant()["used"], 1)
        self.assertEqual(self.grant()["charged"], {"tokens": 2})
        self.assertEqual(self.state()["challenges"][0]["status"], "OPEN")

    def test_post_attempt_repair_cannot_refund_an_exhausted_allowance(self):
        self.engine.create_envelope(example_envelope("one", 1))
        self.accepted(envelope_id="one")
        self.engine.execute("a")
        self.engine.reconcile("a")
        self.engine.challenge("c", "a", "Repair requested", "evidence", True)
        result = self.engine.revise("c", self.proposal("repair", envelope_id="one"),
                                    "Proposed repair", "Allowance remains exhausted")
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.engine.action("a")["status"], "VERIFIED")
        self.assertEqual(len(self.state()["effects"]), 1)

    def test_competing_reservations_cannot_overdraw_resources(self):
        e = example_envelope("budget", 20)
        self.engine.create_envelope(e)
        barrier = Barrier(12)

        def reserve(index):
            barrier.wait()
            p = self.proposal(str(index), envelope_id="budget", costs={"tokens": 3})
            return self.engine.propose(p)["status"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(reserve, range(12)))
        self.assertEqual(statuses.count("RESERVED"), 3)
        grants = {g["id"]: g for g in self.state()["envelopes"]}
        self.assertEqual(grants["budget"]["charged"]["tokens"], 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
