"""Offline tests only. All subprocesses are local Node/Python fixture processes."""
import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from bridge_store import Bridge, GateError, MAX_MANIFEST, MAX_OUTPUT, manifest, sha
from vendor.decision_repair import Engine

HERE = Path(__file__).resolve().parent
FIXTURE = (HERE / "fixture-output.json").read_bytes()


def encode(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()


def fixture_manifest():
    return {"schema": 1, "transport": "fake", "request": {
        "model": "fixture-model", "reasoning_effort": "high",
        "objective": "Fixed fixture, café and 水; no service calls.",
        "task_name": "bridge-test", "preload_skills": [], "extended_context": False},
        "outputs": [{"path": "result.json", "sha256": sha(FIXTURE)}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fake-bridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "outputs"
        self.root.mkdir()
        self.db = self.folder / "ledger.sqlite"
        self.m = fixture_manifest()
        self.encoded = encode(self.m)
        self.manifest_path = self.folder / "manifest.json"
        self.manifest_path.write_bytes(base64.b64decode(self.encoded))
        self.b = Bridge.initialize(self.db, self.encoded, self.root, 3, "fixture:unit-tests")

    def node_args(self, identity="a", mode="success"):
        return ["node", str(HERE / "bridge.mjs"), "run", "--db", str(self.db),
                "--manifest", str(self.manifest_path), "--action", identity, "--mode", mode]

    def node(self, identity="a", mode="success", ok=True):
        proc = subprocess.run(self.node_args(identity, mode), capture_output=True, text=True, timeout=40)
        self.assertEqual(proc.returncode, 0 if ok else 2, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["ok"], ok)
        return data.get("result", data)

    def cli(self, op, payload, ok=True):
        p = subprocess.run([sys.executable, str(HERE / "bridge_store.py"), "--db", str(self.db), op],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0 if ok else 2, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def journal(self):
        file = self.root / "fake-invocations.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def begin(self, identity="a"):
        self.assertEqual(self.b.reserve(identity, self.encoded)["status"], "RESERVED")
        return self.b.begin(identity, self.encoded)

    def event(self, permit, kind="accepted", **updates):
        result = {"transport": "fake", "action_id": permit["action_id"], "nonce": permit["nonce"],
                  "request_id": "fake-" + permit["nonce"], "kind": kind,
                  "event_id": permit["nonce"] + "-" + kind}
        if kind == "completed":
            result.update(success=True, outputs=copy.deepcopy(self.m["outputs"]))
        result.update(updates)
        return result

    def completed(self, write=True):
        p = self.begin()
        self.assertTrue(self.b.observe("a", self.event(p))["accepted"])
        self.assertTrue(self.b.observe("a", self.event(p, "completed"))["accepted"])
        if write:
            folder = self.root / p["nonce"]
            folder.mkdir()
            (folder / "result.json").write_bytes(FIXTURE)
        return p

    def assert_output_denied(self):
        result = self.b.verify("a")
        self.assertEqual(result["status"], "OUTPUT_CHECK_FAILED")
        self.assertTrue(result["completion_observed"])
        self.assertFalse(result["outputs_verified"])
        self.assertEqual(self.b.reserve("next", self.encoded)["status"], "REJECTED")

    def test_node_success_exact_request_and_independent_facts(self):
        r = self.node()
        a = r["action"]
        self.assertEqual(a["status"], "OUTPUTS_VERIFIED")
        self.assertTrue(all(a[k] for k in ("dispatch_accepted", "completion_observed",
                                           "completion_success", "outputs_verified")))
        self.assertFalse(a["real_effects"])
        self.assertNotIn("verified", a)
        self.assertEqual(self.journal()[0]["request"], self.m["request"])
        self.assertEqual(len(self.journal()), 1)
        e = self.b.export()
        self.assertEqual(e["schema"], 2)
        self.assertEqual(e["effects"], [])
        self.assertEqual(e["runs"], [])

    def test_new_cli_process_restores_state(self):
        self.node()
        a = self.cli("inspect", {"identity": "a"})["result"]
        self.assertEqual(a["status"], "OUTPUTS_VERIFIED")
        self.assertTrue(a["outputs_verified"])

    def test_fixed_permit_not_caller_reconstructed(self):
        p = self.begin()
        self.assertEqual(p["request"], self.m["request"])
        self.assertEqual(p["manifest_sha256"], sha(base64.b64decode(self.encoded)))
        self.assertEqual(self.b.action("a")["status"], "IN_FLIGHT")
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 1)

    def test_changed_objective_model_effort_rejected(self):
        for key in ("objective", "model", "reasoning_effort"):
            with self.subTest(key=key):
                m = copy.deepcopy(self.m)
                m["request"][key] += "-altered"
                self.manifest_path.write_bytes(base64.b64decode(encode(m)))
                self.assertEqual(self.node(key)["action"]["status"], "REJECTED")
        self.assertEqual(self.journal(), [])

    def test_byte_reserialization_is_not_same_authorization(self):
        self.manifest_path.write_text(json.dumps(self.m, indent=2))
        self.assertEqual(self.node()["action"]["status"], "REJECTED")
        self.assertEqual(self.journal(), [])

    def test_drift_between_reserve_and_begin_rejected(self):
        self.b.reserve("a", self.encoded)
        changed = copy.deepcopy(self.m)
        changed["request"]["objective"] = "changed after reservation"
        with self.assertRaisesRegex(GateError, "bytes changed"):
            self.b.begin("a", encode(changed))
        self.assertFalse(self.b.action("a")["attempted"])

    def test_manifest_validation_variants(self):
        variants = []
        for key, value in (("transport", "live"), ("schema", True), ("schema", 2),
                           ("outputs", []), ("unexpected", "ignored?")):
            m = copy.deepcopy(self.m)
            m[key] = value
            variants.append(m)
        for key, value in (("extended_context", 1), ("preload_skills", "skill"),
                           ("preload_skills", [False]), ("objective", ""), ("tool", "live")):
            m = copy.deepcopy(self.m)
            m["request"][key] = value
            variants.append(m)
        for value in ("../result.json", "/tmp/result.json", "elsewhere.json"):
            m = copy.deepcopy(self.m)
            m["outputs"][0]["path"] = value
            variants.append(m)
        for m in variants:
            with self.subTest(manifest=m):
                with self.assertRaises((GateError, ValueError)):
                    manifest(encode(m))

    def test_duplicate_keys_nonfinite_invalid_encoding_and_size(self):
        for raw in (b'{"schema":1,"schema":1}', b'{"x":NaN}', b"\xff", b"x" * (MAX_MANIFEST + 1)):
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises((GateError, ValueError)):
                    manifest(base64.b64encode(raw).decode())
        with self.assertRaises(GateError):
            manifest("not-base64!")

    def test_zero_allowance_prevents_dispatch(self):
        zero = self.folder / "zero.db"
        self.b = Bridge.initialize(zero, self.encoded, self.root, 0, "fixture:zero")
        self.db = zero
        self.assertEqual(self.node()["action"]["status"], "REJECTED")
        self.assertEqual(self.journal(), [])

    def test_boolean_allowance_is_not_integer(self):
        with self.assertRaises(GateError):
            Bridge.initialize(self.folder / "bool.db", self.encoded, self.root, True, "fixture:bool")

    def test_initializer_rejects_symlink_root(self):
        link = self.folder / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            Bridge.initialize(self.folder / "link.db", self.encoded, link, 1, "fixture:link")
        self.assertFalse((self.folder / "link.db").exists())

    def test_closed_before_reserve_no_fake_dispatch(self):
        self.b.close_envelope("dispatch", "fixture closed")
        self.assertEqual(self.node()["action"]["status"], "REJECTED")
        self.assertEqual(self.journal(), [])

    def test_closed_after_reserve_prevents_begin(self):
        self.b.reserve("a", self.encoded)
        self.b.close_envelope("dispatch", "closed before attempt")
        with self.assertRaises(GateError):
            self.b.begin("a", self.encoded)
        self.assertFalse(self.b.action("a")["attempted"])

    def test_close_after_begin_still_accepts_evidence(self):
        p = self.begin()
        self.b.close_envelope("dispatch", "cannot cancel a committed attempt")
        self.assertTrue(self.b.observe("a", self.event(p))["accepted"])
        self.assertEqual(self.b.action("a")["status"], "DISPATCH_ACCEPTED")
        self.assertEqual(self.b.reserve("new", self.encoded)["status"], "REJECTED")

    def test_cap_cannot_be_reset_by_renaming(self):
        for name in ("a", "b", "c"):
            self.assertEqual(self.node(name)["action"]["status"], "OUTPUTS_VERIFIED")
        self.assertEqual(self.node("renamed")["action"]["status"], "REJECTED")
        self.assertEqual(len(self.journal()), 3)
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 3)

    def test_same_action_never_replayed(self):
        self.node()
        self.node(ok=False)
        self.assertEqual(len(self.journal()), 1)

    def test_reinitialize_database_refused(self):
        with self.assertRaises(FileExistsError):
            Bridge.initialize(self.db, self.encoded, self.root, 999, "fixture:reset")
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 0)

    def test_nonfixture_authority_refused(self):
        with self.assertRaises(GateError):
            Bridge.initialize(self.folder / "other.db", self.encoded, self.root, 1, "historical-study")
        self.assertFalse((self.folder / "other.db").exists())

    def test_legacy_database_not_reinterpreted(self):
        old = self.folder / "legacy.db"
        Engine(old)
        with self.assertRaisesRegex(GateError, "version 2"):
            Bridge(old)
        with self.assertRaises(FileExistsError):
            Bridge.initialize(old, self.encoded, self.root, 1, "fixture:no-migration")

    def test_simulator_and_envelope_mutation_apis_disabled(self):
        for method in (self.b.execute, self.b.reconcile, self.b.propose,
                       self.b.revise, self.b.create_envelope):
            with self.subTest(method=method.__name__), self.assertRaises(GateError):
                method("unused")

    def test_receipt_only_does_not_verify_or_complete(self):
        a = self.node(mode="receipt_only")["action"]
        self.assertTrue(a["dispatch_accepted"])
        self.assertFalse(a["completion_observed"])
        self.assertFalse(a["outputs_verified"])
        with self.assertRaises(GateError):
            self.b.verify("a")
        self.assertEqual(self.node("next")["action"]["status"], "REJECTED")
        self.assertEqual(len(self.journal()), 1)

    def test_lost_receipt_after_acceptance_unknown_no_retry(self):
        a = self.node(mode="timeout_after")["action"]
        self.assertEqual(a["status"], "UNKNOWN")
        self.assertFalse(a["dispatch_accepted"])
        self.assertTrue((self.root / a["nonce"] / "result.json").exists())
        self.node(ok=False)
        self.assertEqual(self.node("renamed")["action"]["status"], "REJECTED")
        self.assertEqual(len(self.journal()), 1)
        self.assertEqual(self.cli("inspect", {"identity": "a"})["result"]["status"], "UNKNOWN")

    def test_process_exit_after_fake_acceptance_before_receipt_storage(self):
        # This is a new local Node process, not a model call. Exit deliberately in
        # the effect/receipt gap without running the bridge's catch/observe path.
        code = """
import {ledger} from './bridge.mjs';
import {fakeDispatch} from './fake_dispatcher.mjs';
import fs from 'node:fs';
const [db, file] = process.argv.slice(1);
const manifest_b64 = fs.readFileSync(file).toString('base64');
ledger(db, 'reserve', {action_id:'a', manifest_b64});
const permit = ledger(db, 'begin', {action_id:'a', manifest_b64});
fakeDispatch(permit.request, permit, 'success');
process.exit(17);
"""
        proc = subprocess.run(["node", "--input-type=module", "-e", code,
                               str(self.db), str(self.manifest_path)],
                              cwd=HERE, capture_output=True, text=True, timeout=40)
        self.assertEqual(proc.returncode, 17, proc.stdout + proc.stderr)
        a = self.cli("inspect", {"identity": "a"})["result"]
        self.assertEqual(a["status"], "IN_FLIGHT")
        self.assertFalse(a["dispatch_accepted"])
        self.assertFalse(a["outputs_verified"])
        self.assertEqual(len(self.journal()), 1)
        self.node(ok=False)
        self.assertEqual(self.node("renamed")["action"]["status"], "REJECTED")
        self.assertEqual(len(self.journal()), 1)

    def test_timeout_before_also_unknown_not_refunded(self):
        a = self.node(mode="timeout_before")["action"]
        self.assertEqual(a["status"], "UNKNOWN")
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 1)
        self.assertEqual(self.node("next")["action"]["status"], "REJECTED")

    def test_late_correlated_receipt_resolves_unknown_without_dispatch(self):
        p = self.begin()
        self.b.uncertain("a", p["nonce"], "lost receipt")
        self.assertTrue(self.b.observe("a", self.event(p))["accepted"])
        self.assertEqual(self.b.action("a")["status"], "DISPATCH_ACCEPTED")
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 1)

    def test_wrong_nonce_rejected(self):
        r = self.node(mode="wrong_nonce")
        self.assertEqual(r["action"]["status"], "UNKNOWN")
        self.assertTrue(all(not o["accepted"] for o in r["observations"]))
        self.assertFalse(r["action"]["dispatch_accepted"])

    def test_wrong_request_id_rejected(self):
        self.assertEqual(self.node(mode="wrong_id")["action"]["status"], "UNKNOWN")

    def test_wrong_action_or_transport_rejected(self):
        p = self.begin()
        for change in ({"action_id": "another"}, {"transport": "live"}):
            with self.subTest(change=change):
                self.assertFalse(self.b.observe("a", self.event(p, **change))["accepted"])
        self.assertEqual(self.b.action("a")["status"], "IN_FLIGHT")

    def test_receipt_cannot_claim_verified_completion(self):
        p = self.begin()
        self.assertFalse(self.b.observe("a", self.event(p, success=True, outputs=[]))["accepted"])
        self.assertFalse(self.b.action("a")["completion_observed"])

    def test_out_of_order_then_explicit_resubmission(self):
        r = self.node(mode="out_of_order")
        self.assertEqual(r["action"]["status"], "DISPATCH_ACCEPTED")
        self.assertFalse(r["observations"][0]["accepted"])
        p = {"action_id": "a", "nonce": r["action"]["nonce"]}
        c = self.event(p, "completed", event_id=p["nonce"] + "-completion")
        self.assertTrue(self.b.observe("a", c)["accepted"])
        self.assertEqual(self.b.verify("a")["status"], "OUTPUTS_VERIFIED")
        self.assertEqual(len(self.journal()), 1)

    def test_duplicate_events_idempotent(self):
        r = self.node(mode="duplicate")
        self.assertEqual(r["action"]["status"], "OUTPUTS_VERIFIED")
        self.assertEqual([o["duplicate"] for o in r["observations"]], [False, True, False, True])
        self.assertEqual(len(self.b.export()["observations"]), 2)
        self.assertEqual(len(self.journal()), 1)

    def test_event_id_collision_rejected(self):
        p = self.begin()
        first = self.event(p)
        self.b.observe("a", first)
        changed = self.event(p, "rejected", event_id=first["event_id"])
        self.assertFalse(self.b.observe("a", changed)["accepted"])
        self.assertEqual(self.b.action("a")["status"], "DISPATCH_ACCEPTED")

    def test_failed_dispatch_halts_without_refund(self):
        a = self.node(mode="rejected")["action"]
        self.assertEqual(a["status"], "FAILED")
        self.assertFalse(a["dispatch_accepted"])
        self.assertEqual(self.b.export()["envelopes"][0]["status"], "HALTED")
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 1)
        self.assertEqual(self.node("next")["action"]["status"], "REJECTED")

    def test_failed_completion_not_verified(self):
        a = self.node(mode="completion_failed")["action"]
        self.assertTrue(a["dispatch_accepted"])
        self.assertTrue(a["completion_observed"])
        self.assertFalse(a["completion_success"])
        self.assertFalse(a["outputs_verified"])
        self.assertEqual(a["status"], "FAILED")
        with self.assertRaises(GateError):
            self.b.verify("a")

    def test_verified_state_not_downgraded_by_late_claim(self):
        p = self.completed()
        self.b.verify("a")
        self.assertFalse(self.b.observe("a", self.event(p, "rejected"))["accepted"])
        self.b.uncertain("a", p["nonce"], "late uncertainty report")
        self.assertEqual(self.b.action("a")["status"], "OUTPUTS_VERIFIED")

    def test_uncertainty_wrong_nonce_rejected(self):
        self.begin()
        with self.assertRaises(GateError):
            self.b.uncertain("a", "wrong", "wrong attempt")

    def test_inflight_holds_previously_reserved_other_work(self):
        self.b.reserve("a", self.encoded)
        self.b.reserve("b", self.encoded)
        self.b.begin("a", self.encoded)
        with self.assertRaises(GateError):
            self.b.begin("b", self.encoded)

    def test_material_challenge_prevents_begin_until_disposition(self):
        self.b.reserve("a", self.encoded)
        self.b.challenge("c", "a", "fixture concern", "fixture:observation", True)
        with self.assertRaises(GateError):
            self.b.begin("a", self.encoded)
        self.b.disposition("c", "retain", "fixture rationale", "remaining uncertainty")
        self.assertEqual(self.b.begin("a", self.encoded)["transport"], "fake")

    def test_challenge_after_attempt_does_not_fabricate_cancellation(self):
        p = self.begin()
        self.b.challenge("c", "a", "fixture concern", "fixture:observation", True)
        self.assertTrue(self.b.observe("a", self.event(p))["accepted"])
        self.assertEqual(self.b.reserve("b", self.encoded)["status"], "REJECTED")

    def test_completed_is_not_yet_verified_and_holds(self):
        self.completed()
        a = self.b.action("a")
        self.assertEqual(a["status"], "COMPLETION_OBSERVED")
        self.assertFalse(a["outputs_verified"])
        self.assertEqual(self.b.reserve("b", self.encoded)["status"], "REJECTED")

    def test_unsafe_reported_path_not_opened(self):
        r = self.node(mode="unsafe_output")
        self.assertEqual(r["action"]["status"], "OUTPUT_CHECK_FAILED")
        self.assertIn("output list differs", r["action"]["note"])
        self.assertTrue(r["action"]["completion_observed"])

    def test_absolute_unapproved_extra_and_wrong_hash_reports_rejected(self):
        from unittest.mock import patch
        for output in ([{"path": "/etc/passwd", "sha256": sha(FIXTURE)}],
                       [{"path": "elsewhere.json", "sha256": sha(FIXTURE)}],
                       [{"path": "result.json", "sha256": "0" * 64}],
                       self.m["outputs"] * 2):
            # Separate DB fixture per contract mutation to preserve immutable completion evidence.
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "out"
                root.mkdir()
                b = Bridge.initialize(Path(temp) / "db", self.encoded, root, 1, "fixture:paths")
                b.reserve("a", self.encoded)
                q = b.begin("a", self.encoded)
                b.observe("a", self.event(q))
                b.observe("a", self.event(q, "completed", outputs=output))
                with patch.object(Bridge, "_read_output", side_effect=AssertionError("unsafe read")):
                    self.assertEqual(b.verify("a")["status"], "OUTPUT_CHECK_FAILED")

    def test_missing_output_rejected(self):
        self.completed(write=False)
        self.assert_output_denied()

    def test_tampered_output_bytes_rejected(self):
        p = self.completed()
        (self.root / p["nonce"] / "result.json").write_bytes(b"wrong bytes")
        self.assert_output_denied()

    def test_oversized_output_rejected(self):
        p = self.completed()
        with (self.root / p["nonce"] / "result.json").open("wb") as f:
            f.truncate(MAX_OUTPUT + 1)
        self.assert_output_denied()

    def test_output_growth_during_read_is_bounded(self):
        from unittest.mock import patch
        self.completed()
        with patch("bridge_store.os.read", return_value=b"x" * 65536):
            self.assert_output_denied()
        self.assertIn("grew beyond", self.b.action("a")["note"])

    def test_output_mutation_during_read_is_not_verified(self):
        from unittest.mock import patch
        p = self.completed()
        output = self.root / p["nonce"] / "result.json"
        original = os.read
        changed = False

        def mutating_read(fd, size):
            nonlocal changed
            data = original(fd, size)
            if data and not changed:
                changed = True
                # Change size and timestamps after the original bytes were read.
                with output.open("ab") as f:
                    f.write(b"changed")
            return data

        with patch("bridge_store.os.read", side_effect=mutating_read):
            self.assert_output_denied()
        self.assertIn("changed during verification", self.b.action("a")["note"])

    def test_symlink_output_rejected(self):
        p = self.completed()
        outside = self.folder / "outside.json"
        outside.write_bytes(FIXTURE)
        target = self.root / p["nonce"] / "result.json"
        target.unlink()
        target.symlink_to(outside)
        self.assert_output_denied()

    def test_hardlinked_output_rejected(self):
        p = self.completed()
        os.link(self.root / p["nonce"] / "result.json", self.folder / "outside.json")
        self.assert_output_denied()

    def test_symlink_nonce_directory_rejected(self):
        p = self.completed()
        folder = self.root / p["nonce"]
        moved = self.folder / "moved"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        self.assert_output_denied()

    def test_replaced_root_identity_rejected(self):
        p = self.completed()
        self.root.rename(self.folder / "old")
        self.root.mkdir()
        (self.root / p["nonce"]).mkdir()
        (self.root / p["nonce"] / "result.json").write_bytes(FIXTURE)
        self.assert_output_denied()

    def test_symlink_root_rejected(self):
        self.completed()
        moved = self.folder / "old"
        self.root.rename(moved)
        self.root.symlink_to(moved, target_is_directory=True)
        self.assert_output_denied()

    def test_fifo_output_rejected_without_blocking(self):
        p = self.completed()
        file = self.root / p["nonce"] / "result.json"
        file.unlink()
        os.mkfifo(file)
        result = self.cli("verify", {"action_id": "a"})
        self.assertEqual(result["result"]["status"], "OUTPUT_CHECK_FAILED")

    def test_directory_instead_of_output_rejected(self):
        p = self.completed()
        file = self.root / p["nonce"] / "result.json"
        file.unlink()
        file.mkdir()
        self.assert_output_denied()

    def test_verification_can_recheck_repaired_fixture_without_dispatch(self):
        p = self.completed(write=False)
        self.assertEqual(self.b.verify("a")["status"], "OUTPUT_CHECK_FAILED")
        (self.root / p["nonce"]).mkdir()
        (self.root / p["nonce"] / "result.json").write_bytes(FIXTURE)
        self.assertEqual(self.b.verify("a")["status"], "OUTPUTS_VERIFIED")
        self.assertEqual(self.journal(), [])
        kinds = [e["kind"] for e in self.b.export()["events"]]
        self.assertIn("FIXTURE_OUTPUT_CHECK_FAILED", kinds)
        self.assertIn("FIXTURE_OUTPUT_VERIFIED", kinds)

    def test_symlink_manifest_refused_before_reserve(self):
        other = self.folder / "other.json"
        self.manifest_path.rename(other)
        self.manifest_path.symlink_to(other)
        self.node(ok=False)
        self.assertEqual(self.b.export()["actions"], [])

    def test_oversized_manifest_refused_before_reserve(self):
        self.manifest_path.write_bytes(b"x" * (MAX_MANIFEST + 1))
        self.node(ok=False)
        self.assertEqual(self.b.export()["actions"], [])

    def test_fifo_manifest_refused_without_blocking(self):
        self.manifest_path.unlink()
        os.mkfifo(self.manifest_path)
        self.node(ok=False)
        self.assertEqual(self.b.export()["actions"], [])

    def test_symlink_journal_not_written(self):
        sentinel = self.folder / "sentinel"
        sentinel.write_text("unchanged")
        (self.root / "fake-invocations.jsonl").symlink_to(sentinel)
        self.assertEqual(self.node()["action"]["status"], "UNKNOWN")
        self.assertEqual(sentinel.read_text(), "unchanged")

    def test_fifo_journal_does_not_hang(self):
        os.mkfifo(self.root / "fake-invocations.jsonl")
        self.assertEqual(self.node()["action"]["status"], "UNKNOWN")

    def test_unsupported_node_mode_and_live_flag_refused(self):
        self.node(mode="live", ok=False)
        p = subprocess.run(self.node_args() + ["--live", "true"], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.b.export()["actions"], [])

    def test_cli_rejects_unexpected_payload_fields(self):
        self.cli("reserve", {"action_id": "a", "manifest_b64": self.encoded, "live": True}, ok=False)
        self.assertEqual(self.b.export()["actions"], [])

    def test_profile_and_proposal_integrity_checks(self):
        self.b.reserve("a", self.encoded)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE actions SET digest='wrong' WHERE id='a'")
        with self.assertRaisesRegex(GateError, "integrity"):
            self.b.begin("a", self.encoded)

    def test_demo_is_fake_and_cleans_its_temporary_root(self):
        p = subprocess.run(["node", str(HERE / "bridge.mjs"), "demo"],
                           capture_output=True, text=True, timeout=40)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = json.loads(p.stdout)["result"]
        self.assertEqual(r["action"]["status"], "OUTPUTS_VERIFIED")
        self.assertFalse(Path(r["ledger"]["bridge_profile"]["output_root"]).exists())


class ConcurrentBridgeTests(unittest.TestCase):
    # Reuse fixture helpers without duplicating the inherited functional tests.
    setUp = BridgeTests.setUp
    node_args = BridgeTests.node_args
    journal = BridgeTests.journal

    def processes(self, actions):
        processes = [subprocess.Popen(self.node_args(a), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for a in actions]
        replies = []
        for p in processes:
            stdout, stderr = p.communicate(timeout=60)
            self.assertIn(p.returncode, (0, 2), stdout + stderr)
            replies.append(json.loads(stdout))
        return replies

    def test_concurrent_last_slot_at_most_one_dispatch(self):
        # Reserve two slots without attempting them. Only one slot remains.
        self.b.reserve("parked-1", self.encoded)
        self.b.reserve("parked-2", self.encoded)
        replies = self.processes(["a", "b", "c", "d"])
        self.assertEqual(sum(r["ok"] and r["result"]["action"]["outputs_verified"] for r in replies), 1)
        self.assertEqual(len(self.journal()), 1)
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 3)

    def test_concurrent_same_action_at_most_one_dispatch(self):
        replies = self.processes(["same"] * 4)
        self.assertEqual(sum(r["ok"] for r in replies), 1)
        self.assertEqual(len(self.journal()), 1)
        self.assertEqual(self.b.export()["envelopes"][0]["used"], 1)

    def test_concurrent_begin_of_reserved_action_only_one_permit(self):
        self.b.reserve("a", self.encoded)
        command = [sys.executable, str(HERE / "bridge_store.py"), "--db", str(self.db), "begin"]
        processes = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(4)]
        payload = json.dumps({"action_id": "a", "manifest_b64": self.encoded})
        # Send all input before collecting so these transactions genuinely overlap.
        for p in processes:
            p.stdin.write(payload)
            p.stdin.close()
            p.stdin = None
        replies = [json.loads(p.communicate(timeout=30)[0]) for p in processes]
        self.assertEqual(sum(r["ok"] for r in replies), 1)
        self.assertEqual(len(self.b.export()["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
