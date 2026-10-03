"""Fake-only dispatch ledger. No service, model or network client is present.

Approval and evidence are synthetic, trusted inputs, not authenticated facts.
The persisted attempt marker prevents this cooperative bridge from replaying
an ambiguous dispatch. It is not a remote exactly-once guarantee.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

from vendor.decision_repair import Engine, GateError, canonical, fields, quantity, text

MAX_MANIFEST = 65536
MAX_OUTPUT = 1048576
PENDING = ("IN_FLIGHT", "DISPATCH_ACCEPTED", "COMPLETION_OBSERVED",
           "UNKNOWN", "OUTPUT_CHECK_FAILED")
SCHEMA = """
CREATE TABLE bridge_profile (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
CREATE TABLE snapshots (
 action_id TEXT PRIMARY KEY REFERENCES actions(id), manifest_b64 TEXT NOT NULL
);
CREATE TABLE attempts (
 action_id TEXT PRIMARY KEY REFERENCES actions(id), nonce TEXT UNIQUE NOT NULL,
 request_id TEXT, completion TEXT, verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE observations (
 event_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id),
 body TEXT NOT NULL
);
"""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(GateError("nonfinite JSON")))


def manifest(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_MANIFEST * 2:
        raise GateError("manifest encoding is too large or not text")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise GateError("invalid base64") from error
    if len(raw) > MAX_MANIFEST:
        raise GateError("manifest too large")
    m = parse_json(raw.decode("utf-8"))
    fields(m, ("schema", "transport", "request", "outputs"))
    if type(m["schema"]) is not int or m["schema"] != 1 or m["transport"] != "fake":
        raise GateError("only schema 1, fake transport is supported")
    r = m["request"]
    fields(r, ("model", "reasoning_effort", "objective", "task_name",
               "preload_skills", "extended_context"))
    for key in ("model", "reasoning_effort", "objective", "task_name"):
        text(r[key], key)
    if not isinstance(r["preload_skills"], list) or any(
            not isinstance(v, str) or not v.strip() for v in r["preload_skills"]):
        raise GateError("preload_skills must be a list of nonempty strings")
    if type(r["extended_context"]) is not bool:
        raise GateError("extended_context must be boolean")
    if not isinstance(m["outputs"], list) or len(m["outputs"]) != 1:
        raise GateError("exactly one fixture output is required")
    output = m["outputs"][0]
    fields(output, ("path", "sha256"))
    if output["path"] != "result.json" or not isinstance(output["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", output["sha256"]):
        raise GateError("only the approved result.json fixture is supported")
    return raw, m


def scope(digest):
    return {"kind": "fake_dispatch", "target": "sha256:" + digest,
            "audience": "private-fixture", "horizon": "one-fixture-undertaking"}


class Bridge(Engine):
    """Versioned subclass with simulator execution/reconciliation disabled.

    The public CLI exposes only these operations. Python/DB owners can bypass
    the implementation, so this is a cooperative boundary, not a sandbox.
    """

    def __init__(self, database):
        self.database = str(Path(database).absolute())
        if not Path(self.database).is_file():
            raise GateError("initialize a fresh bridge database explicitly")
        try:
            with self._transaction() as db:
                self._profile(db)
        except sqlite3.OperationalError as error:
            raise GateError("not a version 2 fake bridge database") from error

    @staticmethod
    def _profile(db):
        Engine._require_disposition(db)
        row = db.execute("SELECT body FROM bridge_profile WHERE id=1").fetchone()
        if row is None:
            raise GateError("missing bridge profile")
        p = parse_json(row["body"])
        if p["schema"] != 2 or p["transport"] != "fake":
            raise GateError("unsupported bridge profile")
        raw, _ = manifest(p["manifest_b64"])
        if sha(raw) != p["manifest_sha256"]:
            raise GateError("profile manifest integrity mismatch")
        return p

    @classmethod
    def initialize(cls, database, manifest_b64, output_root, max_attempts, approval_ref):
        raw, _ = manifest(manifest_b64)
        quantity(max_attempts, "max_attempts")
        text(approval_ref, "approval_ref")
        if not approval_ref.startswith("fixture:"):
            raise GateError("this initializer accepts only synthetic fixture authorization")
        root = Path(output_root).absolute()
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        # Exclusive creation refuses both older simulator DBs and resetting caps.
        fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        e = Engine(database)
        e.create_envelope({
            "id": "dispatch", "purpose": "fake-dispatch-bridge",
            "approved_by": "synthetic-fixture-operator", "approval_ref": approval_ref,
            "standing": "authorized", "permissions": [scope(sha(raw))],
            "max_attempts": max_attempts, "resource_limits": {},
            "accepted_burdens": {}, "stop_on_failure": True})
        profile = {"schema": 2, "transport": "fake", "manifest_b64": manifest_b64,
                   "manifest_sha256": sha(raw), "output_root": str(root),
                   "root_dev": info.st_dev, "root_ino": info.st_ino}
        with e._transaction() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO bridge_profile VALUES(1,?)", (canonical(profile),))
            e._event(db, "FAKE_BRIDGE_INITIALIZED", "dispatch", profile)
        return cls(database)

    @staticmethod
    def _hold(db, envelope_id, ignore_challenge=None, ignore_action=None):
        pending = db.execute(
            "SELECT id FROM actions WHERE envelope_id=? AND status IN (?,?,?,?,?)",
            (envelope_id, *PENDING)).fetchall()
        if any(r["id"] != ignore_action for r in pending):
            return "unresolved dispatch in this undertaking"
        return Engine._hold(db, envelope_id, ignore_challenge, ignore_action)

    def create_envelope(self, *args, **kwargs):
        raise GateError("bridge authorization is fixed by fresh fixture initialization")

    def propose(self, *args, **kwargs):
        raise GateError("use manifest-bound reserve")

    def revise(self, *args, **kwargs):
        raise GateError("checked replacement is not implemented in this bridge increment")

    def execute(self, *args, **kwargs):
        raise GateError("inert simulator execute is disabled in bridge profile")

    def reconcile(self, *args, **kwargs):
        raise GateError("use correlated observations and explicit output verification")

    def reserve(self, action_id, manifest_b64):
        text(action_id, "action_id")
        if len(action_id) > 128:
            raise GateError("action identifier too long")
        raw, _ = manifest(manifest_b64)
        proposal = {
            "id": action_id, "envelope_id": "dispatch", "purpose": "fake-dispatch-bridge",
            **scope(sha(raw)), "costs": {}, "burdens": [], "material": False,
            "reasons": {}, "consequences": []}
        with self._transaction() as db:
            self._profile(db)
            self._fresh(db, action_id)
            self._store(db, proposal, self._assess(db, proposal))
            db.execute("INSERT INTO snapshots VALUES(?,?)", (action_id, manifest_b64))
        return self.action(action_id)

    def begin(self, action_id, manifest_b64):
        raw, _ = manifest(manifest_b64)
        with self._transaction() as db:
            profile = self._profile(db)
            row = self._one(db, "actions", action_id)
            proposal = self._proposal(row)
            snap = db.execute("SELECT manifest_b64 FROM snapshots WHERE action_id=?",
                              (action_id,)).fetchone()
            frozen_raw, frozen = manifest(snap["manifest_b64"])
            if (raw != frozen_raw or sha(raw) != profile["manifest_sha256"]
                    or proposal["target"] != "sha256:" + sha(raw)):
                raise GateError("request bytes changed or do not match fixture authorization")
            if row["status"] != "RESERVED" or row["attempted"]:
                raise GateError("only a never-attempted reservation may begin; no replay")
            envelope, _ = self._envelope(db, "dispatch")
            hold = self._hold(db, "dispatch")
            if envelope["status"] != "OPEN" or hold:
                raise GateError(hold or "undertaking is not open")
            nonce = uuid.uuid4().hex
            db.execute("INSERT INTO attempts(action_id,nonce) VALUES(?,?)", (action_id, nonce))
            db.execute("UPDATE actions SET status='IN_FLIGHT',attempted=1 WHERE id=?", (action_id,))
            permit = {"transport": "fake", "action_id": action_id, "nonce": nonce,
                      "manifest_sha256": sha(raw), "request": frozen["request"],
                      "output_root": profile["output_root"], "root_dev": profile["root_dev"],
                      "root_ino": profile["root_ino"]}
            self._event(db, "ATTEMPT_COMMITTED", action_id, permit)
        return permit

    def uncertain(self, action_id, nonce, reason):
        text(reason, "reason")
        with self._transaction() as db:
            row = self._one(db, "actions", action_id)
            attempt = self._attempt(db, action_id)
            if attempt["nonce"] != nonce:
                raise GateError("uncertainty report has wrong nonce")
            if row["status"] == "IN_FLIGHT":
                db.execute("UPDATE actions SET status='UNKNOWN',note=? WHERE id=?", (reason, action_id))
            self._event(db, "UNCERTAINTY_REPORTED", action_id, {"reason": reason})
        return self.action(action_id)

    @staticmethod
    def _attempt(db, action_id):
        row = db.execute("SELECT * FROM attempts WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            raise GateError("no committed attempt")
        return row

    def observe(self, action_id, observation):
        """Rejected and duplicate events are recorded without promoting status."""
        with self._transaction() as db:
            row = self._one(db, "actions", action_id)
            attempt = self._attempt(db, action_id)
            try:
                fields(observation, ("event_id", "transport", "kind", "action_id", "nonce",
                                     "request_id"), ("success", "outputs"))
                for key in ("event_id", "kind", "request_id"):
                    text(observation[key], key)
                if (observation["transport"] != "fake" or observation["action_id"] != action_id
                        or observation["nonce"] != attempt["nonce"]):
                    raise GateError("observation correlation mismatch")
                if observation["request_id"] != "fake-" + attempt["nonce"]:
                    raise GateError("wrong fake request id")
                kind = observation["kind"]
                if kind not in ("accepted", "rejected", "completed"):
                    raise GateError("unknown observation kind")
                if kind == "completed":
                    if type(observation.get("success")) is not bool or not isinstance(
                            observation.get("outputs"), list):
                        raise GateError("completion needs boolean success and output list")
                elif "success" in observation or "outputs" in observation:
                    raise GateError("receipt cannot claim completion or output verification")
                old = db.execute("SELECT body FROM observations WHERE event_id=?",
                                 (observation["event_id"],)).fetchone()
                if old is not None:
                    if old["body"] != canonical(observation):
                        raise GateError("event id reused with different evidence")
                    result = {"accepted": True, "duplicate": True}
                else:
                    if kind in ("accepted", "rejected"):
                        if row["status"] not in ("IN_FLIGHT", "UNKNOWN"):
                            raise GateError("late or contradictory dispatch receipt")
                        state = "DISPATCH_ACCEPTED" if kind == "accepted" else "FAILED"
                        if kind == "accepted":
                            db.execute("UPDATE attempts SET request_id=? WHERE action_id=?",
                                       (observation["request_id"], action_id))
                    else:
                        if row["status"] != "DISPATCH_ACCEPTED" or attempt["request_id"] is None:
                            raise GateError("completion must follow a correlated acceptance receipt")
                        state = "COMPLETION_OBSERVED" if observation["success"] else "FAILED"
                        db.execute("UPDATE attempts SET completion=? WHERE action_id=?",
                                   (canonical(observation), action_id))
                    db.execute("INSERT INTO observations VALUES(?,?,?)",
                               (observation["event_id"], action_id, canonical(observation)))
                    db.execute("UPDATE actions SET status=?,note=? WHERE id=?",
                               (state, "synthetic observation only", action_id))
                    if state == "FAILED":
                        db.execute("UPDATE envelopes SET status='HALTED' "
                                   "WHERE id='dispatch' AND status='OPEN'")
                    result = {"accepted": True, "duplicate": False}
                self._event(db, "OBSERVATION_ACCEPTED", action_id, observation)
            except GateError as error:
                result = {"accepted": False, "reason": str(error)}
                self._event(db, "OBSERVATION_REJECTED", action_id,
                            {"observation": observation, "reason": str(error)})
        return {**result, "action": self.action(action_id)}

    @staticmethod
    def _read_output(profile, nonce, approved):
        if not re.fullmatch(r"[0-9a-f]{32}", nonce) or approved["path"] != "result.json":
            raise GateError("unsafe output locator")
        descriptors = []
        try:
            root = os.open(profile["output_root"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(root)
            info = os.fstat(root)
            if (info.st_dev, info.st_ino) != (profile["root_dev"], profile["root_ino"]):
                raise GateError("output root identity changed")
            directory = os.open(nonce, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            descriptors.append(directory)
            file = os.open(approved["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=directory)
            descriptors.append(file)
            before = os.fstat(file)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise GateError("output must be a regular, non-hardlinked fixture file")
            if before.st_size > MAX_OUTPUT:
                raise GateError("output exceeds size bound")
            chunks, length = [], 0
            while True:
                chunk = os.read(file, min(65536, MAX_OUTPUT + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > MAX_OUTPUT:
                    raise GateError("output grew beyond size bound")
            after = os.fstat(file)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise GateError("output changed during verification")
            measured = sha(b"".join(chunks))
            if measured != approved["sha256"]:
                raise GateError("output bytes do not match approved fixture hash")
            return {"path": approved["path"], "sha256": measured, "bytes": length,
                    "dev": after.st_dev, "ino": after.st_ino}
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def verify(self, action_id):
        with self._transaction() as db:
            profile = self._profile(db)
            row = self._one(db, "actions", action_id)
            attempt = self._attempt(db, action_id)
            if row["status"] not in ("COMPLETION_OBSERVED", "OUTPUT_CHECK_FAILED"):
                raise GateError("successful observed completion is required before verification")
            completion = parse_json(attempt["completion"])
            _, m = manifest(profile["manifest_b64"])
            try:
                if not completion["success"] or completion["outputs"] != m["outputs"]:
                    raise GateError("reported output list differs from approved fixture contract")
                proof = self._read_output(profile, attempt["nonce"], m["outputs"][0])
                db.execute("UPDATE attempts SET verified=1 WHERE action_id=?", (action_id,))
                db.execute("UPDATE actions SET status='OUTPUTS_VERIFIED',note=? WHERE id=?",
                           ("fixed fixture bytes verified at this instant; no real task outcome", action_id))
                self._event(db, "FIXTURE_OUTPUT_VERIFIED", action_id, proof)
            except (GateError, OSError) as error:
                db.execute("UPDATE actions SET status='OUTPUT_CHECK_FAILED',note=? WHERE id=?",
                           (str(error), action_id))
                self._event(db, "FIXTURE_OUTPUT_CHECK_FAILED", action_id, {"reason": str(error)})
        return self.action(action_id)

    def action(self, identity):
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            lifecycle = self._lifecycle(db, row)
            result = {"id": identity, "proposal": self._proposal(row),
                      "status": row["status"], "attempted": bool(row["attempted"]),
                      "receipt": None, "external_delivery": False, "note": row["note"],
                      **lifecycle,
                      "next_fake_attempt_permitted": not lifecycle["operational_blocking_reasons"]}
            attempt = db.execute("SELECT * FROM attempts WHERE action_id=?", (identity,)).fetchone()
            completion = parse_json(attempt["completion"]) if attempt and attempt["completion"] else None
            result.update({
                "transport": "fake", "real_effects": False,
                "dispatch_accepted": bool(attempt and attempt["request_id"]),
                "completion_observed": completion is not None,
                "completion_success": completion["success"] if completion else None,
                "outputs_verified": bool(attempt and attempt["verified"]),
                "nonce": attempt["nonce"] if attempt else None})
        return result

    def export(self):
        result = {"schema": 2, "adapter": "fake-dispatch-bridge",
                  "disposition_schema": 1,
                  "external_effects": False, "real_effects": False,
                  "fixture_files_written": "possible"}
        with self._transaction() as db:
            for table in ("envelopes", "actions", "challenges", "effects", "runs", "events", "withdrawals"):
                order = "sequence" if table == "events" else (
                    "action_id" if table in ("effects", "runs", "withdrawals") else "id")
                records = []
                for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                    item = dict(row)
                    for field in ("body", "receipt", "charged", "burden_used"):
                        if field in item and item[field] is not None:
                            item[field] = parse_json(item[field])
                    records.append(item)
                result[table] = records
            result["bridge_profile"] = self._profile(db)
            for table in ("snapshots", "attempts", "observations"):
                result[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY action_id")]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("operation", choices=("init", "reserve", "begin", "observe", "uncertain",
                                             "verify", "inspect", "export", "close",
                                             "challenge", "disposition", "withdraw"))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_MANIFEST * 3 + 1)
        if len(raw) > MAX_MANIFEST * 3:
            raise GateError("CLI payload too large")
        p = parse_json(raw)
        if not isinstance(p, dict):
            raise GateError("CLI payload must be an object")
        if args.operation == "init":
            b = Bridge.initialize(args.db, **p)
            result = b.export()
        else:
            b = Bridge(args.db)
            methods = {"reserve": b.reserve, "begin": b.begin, "observe": b.observe,
                       "uncertain": b.uncertain, "verify": b.verify, "inspect": b.action,
                       "export": b.export, "close": b.close_envelope,
                       "challenge": b.challenge, "disposition": b.disposition,
                       "withdraw": b.withdraw}
            result = methods[args.operation](**p)
        print(canonical({"ok": True, "result": result}))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        print(canonical({"ok": False, "error": str(error)}))
        sys.exit(2)


if __name__ == "__main__":
    main()
