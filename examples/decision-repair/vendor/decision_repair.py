"""Isolated decision-and-repair prototype. All adapters are inert.

Only the SQLite ledger is written. Targets, audiences and evidence references
are opaque strings: this module never opens them or invokes services/models.
Authority, materiality and factual inputs are supplied by a trusted operator.
This is not an authentication system or a sandbox around arbitrary Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


class GateError(ValueError):
    """Invalid input or a transition that cannot safely proceed."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{name} must be nonempty text")


def quantity(value, name):
    if type(value) is not int or value < 0:
        raise GateError(f"{name} must be a nonnegative integer")


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise GateError("expected an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise GateError("missing or unexpected fields")


SCOPE = ("kind", "target", "audience", "horizon")


def validate_envelope(e):
    fields(e, ("id", "purpose", "approved_by", "approval_ref", "standing",
               "permissions", "max_attempts", "resource_limits",
               "accepted_burdens", "stop_on_failure"))
    for key in ("id", "purpose", "approved_by", "approval_ref"):
        text(e[key], key)
    if e["standing"] != "authorized":
        raise GateError("only explicitly authorized envelopes may be loaded")
    quantity(e["max_attempts"], "max_attempts")
    if type(e["stop_on_failure"]) is not bool:
        raise GateError("stop_on_failure must be boolean")
    if not isinstance(e["permissions"], list) or not e["permissions"]:
        raise GateError("permissions must be a nonempty list")
    for permission in e["permissions"]:
        fields(permission, SCOPE)
        for key in SCOPE:
            text(permission[key], key)
    if not isinstance(e["resource_limits"], dict):
        raise GateError("resource_limits must be an object")
    for key, value in e["resource_limits"].items():
        text(key, "resource")
        quantity(value, key)
    if not isinstance(e["accepted_burdens"], dict):
        raise GateError("accepted_burdens must be an object")
    for party, cap in e["accepted_burdens"].items():
        text(party, "party")
        fields(cap, ("unit", "limit", "acceptance_ref"))
        text(cap["unit"], "unit")
        text(cap["acceptance_ref"], "acceptance_ref")
        quantity(cap["limit"], "burden limit")


def validate_proposal(p):
    fields(p, ("id", "envelope_id", "purpose", *SCOPE, "costs", "burdens",
               "material", "reasons", "consequences"), ("references",))
    for key in ("id", "envelope_id", "purpose", *SCOPE):
        text(p[key], key)
    if type(p["material"]) is not bool:
        raise GateError("material must be boolean")
    if not isinstance(p["costs"], dict):
        raise GateError("costs must be an object")
    for key, value in p["costs"].items():
        text(key, "resource")
        quantity(value, key)
    if not isinstance(p["burdens"], list):
        raise GateError("burdens must be a list")
    for b in p["burdens"]:
        fields(b, ("party", "unit", "amount"))
        text(b["party"], "party")
        text(b["unit"], "unit")
        quantity(b["amount"], "burden amount")
    if not isinstance(p["reasons"], dict):
        raise GateError("reasons must be an object")
    if p["material"]:
        fields(p["reasons"], ("benefit", "tradeoff", "alternative", "uncertainty"))
        for key, value in p["reasons"].items():
            text(value, key)
    elif p["reasons"]:
        raise GateError("routine proposals use an empty reasons object")
    if not isinstance(p["consequences"], list):
        raise GateError("consequences must be a list")
    for c in p["consequences"]:
        fields(c, ("party", "benefit", "burden", "unserved", "unknown"))
        for key, value in c.items():
            text(value, key)
    if p["material"]:
        if not p["consequences"]:
            raise GateError("material proposals need a consequence account")
        named = {c["party"] for c in p["consequences"]}
        if any(b["party"] not in named for b in p["burdens"]):
            raise GateError("declared burden party missing from consequence account")
    refs = p.get("references", [])
    if not isinstance(refs, list):
        raise GateError("references must be a list")
    for ref in refs:
        fields(ref, ("source", "version", "standing"))
        for key, value in ref.items():
            text(value, key)
        if ref["standing"] not in ("authorized", "interpretation", "observation", "candidate"):
            raise GateError("unrecognized reference standing")


SCHEMA = """
CREATE TABLE IF NOT EXISTS disposition_profile (
 id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS envelopes (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
 status TEXT NOT NULL, used INTEGER NOT NULL, charged TEXT NOT NULL,
 burden_used TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
 id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL REFERENCES envelopes(id),
 body TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
 attempted INTEGER NOT NULL DEFAULT 0, receipt TEXT, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS challenges (
 id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id),
 claim TEXT NOT NULL, evidence_ref TEXT NOT NULL, material INTEGER NOT NULL,
 status TEXT NOT NULL, disposition TEXT, rationale TEXT, residual TEXT
);
CREATE TABLE IF NOT EXISTS effects (
 action_id TEXT PRIMARY KEY REFERENCES actions(id),
 digest TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 action_id TEXT PRIMARY KEY REFERENCES actions(id), outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawals (
 action_id TEXT PRIMARY KEY REFERENCES actions(id), body TEXT NOT NULL, digest TEXT NOT NULL
);
"""


class Engine:
    """Fresh connection per transaction; no live adapter injection API."""

    MODES = {"success", "timeout_before", "timeout_after", "returned_only",
             "interrupt", "failure", "contradictory"}

    def __init__(self, database):
        self.database = str(database)
        with self._transaction() as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%'").fetchall()
            if tables:
                self._require_disposition(db)
            # execute statements individually: executescript implicitly commits.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT OR IGNORE INTO disposition_profile VALUES(1,1)")

    @staticmethod
    def _require_disposition(db):
        try:
            row = db.execute("SELECT version FROM disposition_profile WHERE id=1").fetchone()
        except sqlite3.OperationalError as error:
            raise GateError("not a disposition-v1 database; no automatic migration") from error
        if row is None or row["version"] != 1:
            raise GateError("unsupported disposition storage version")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.database, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, kind, subject, body):
        db.execute("INSERT INTO events(kind,subject,body) VALUES(?,?,?)",
                   (kind, subject, canonical(body)))

    @staticmethod
    def _one(db, table, identity):
        # Table names originate only from this module, never proposal text.
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise GateError(f"unknown {table} identifier")
        return row

    @classmethod
    def _envelope(cls, db, identity):
        cls._require_disposition(db)
        row = cls._one(db, "envelopes", identity)
        body = json.loads(row["body"])
        if digest(body) != row["digest"]:
            raise GateError("envelope integrity mismatch")
        return row, body

    @staticmethod
    def _proposal(row):
        body = json.loads(row["body"])
        if digest(body) != row["digest"]:
            raise GateError("proposal integrity mismatch")
        return body

    def create_envelope(self, envelope):
        validate_envelope(envelope)
        body = json.loads(canonical(envelope))
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM envelopes WHERE id=?", (body["id"],)).fetchone():
                raise GateError("envelope identifier already exists; no resetting")
            db.execute("INSERT INTO envelopes VALUES(?,?,?,?,?,?,?)",
                       (body["id"], canonical(body), digest(body), "OPEN", 0, "{}", "{}"))
            self._event(db, "ENVELOPE_LOADED", body["id"], body)

    def close_envelope(self, identity, reason):
        text(reason, "reason")
        with self._transaction() as db:
            self._envelope(db, identity)
            db.execute("UPDATE envelopes SET status='CLOSED' WHERE id=?", (identity,))
            self._event(db, "ENVELOPE_CLOSED", identity, {"reason": reason})

    def withdraw(self, identity, actor, reason, residual):
        """End only an unattempted reservation; leave challenges and effects untouched."""
        for name, value in (("actor", actor), ("reason", reason), ("residual", residual)):
            text(value, name)
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            p = self._proposal(row)
            self._envelope(db, row["envelope_id"])
            if row["status"] != "RESERVED" or row["attempted"]:
                raise GateError("withdrawal requires a never-attempted reservation")
            body = {"action_id": identity, "actor": actor, "reason": reason, "residual": residual,
                    "released_reservation": {"slots": 1, "costs": p["costs"], "burdens": p["burdens"]},
                    "meaning": "proposal withdrawn; not a merits decision or actual-cost refund"}
            self._charge(db, p, -1)
            db.execute("INSERT INTO withdrawals VALUES(?,?,?)",
                       (identity, canonical(body), digest(body)))
            db.execute("UPDATE actions SET status='WITHDRAWN',note=? WHERE id=?",
                       ("Never attempted; reservation released; concerns not resolved.", identity))
            self._event(db, "ACTION_WITHDRAWN", identity, body)
        return self.action(identity)

    def _lifecycle(self, db, row):
        envelope, _ = self._envelope(db, row["envelope_id"])
        reasons = []
        if envelope["status"] != "OPEN":
            reasons.append("undertaking is " + envelope["status"])
        if row["status"] != "RESERVED":
            reasons.append("action is " + row["status"] + ", not RESERVED")
        if row["attempted"]:
            reasons.append("attempt already committed; no replay")
        hold = self._hold(db, row["envelope_id"])
        if hold:
            reasons.append(hold)
        withdrawal = db.execute("SELECT * FROM withdrawals WHERE action_id=?", (row["id"],)).fetchone()
        body = None
        if withdrawal is not None:
            body = json.loads(withdrawal["body"])
            if digest(body) != withdrawal["digest"]:
                raise GateError("withdrawal integrity mismatch")
        return {"undertaking_status": envelope["status"],
                "operational_blocking_reasons": reasons, "withdrawal": body,
                "review_assignment": "not_represented"}

    @staticmethod
    def _hold(db, envelope_id, ignore_challenge=None, ignore_action=None):
        pending = db.execute(
            "SELECT id FROM actions WHERE envelope_id=? AND status IN ('IN_FLIGHT','RETURNED','UNKNOWN')",
            (envelope_id,)).fetchall()
        if any(row["id"] != ignore_action for row in pending):
            return "unresolved execution in this undertaking"
        challenges = db.execute(
            """SELECT c.id FROM challenges c JOIN actions a ON c.action_id=a.id
               WHERE a.envelope_id=? AND c.material=1 AND c.status!='RESOLVED'""",
            (envelope_id,)).fetchall()
        if any(row["id"] != ignore_challenge for row in challenges):
            return "material challenge awaiting disposition"
        return None

    def _assess(self, db, p, credit=None, ignore_challenge=None):
        row, e = self._envelope(db, p["envelope_id"])
        if row["status"] != "OPEN":
            return "undertaking is not open"
        if p["purpose"] != e["purpose"]:
            return "purpose differs from authorization"
        scope = {key: p[key] for key in SCOPE}
        if scope not in e["permissions"]:
            return "scope tuple is not authorized"
        hold = self._hold(db, e["id"], ignore_challenge)
        if hold:
            return hold
        old = self._proposal(credit) if credit is not None else None
        if row["used"] - int(old is not None) >= e["max_attempts"]:
            return "attempt allowance exhausted"
        charged, burden_used = json.loads(row["charged"]), json.loads(row["burden_used"])
        for resource, amount in p["costs"].items():
            if resource not in e["resource_limits"]:
                return f"resource {resource} is not authorized"
            previous = charged.get(resource, 0) - (old["costs"].get(resource, 0) if old else 0)
            if previous + amount > e["resource_limits"][resource]:
                return f"resource limit exceeded: {resource}"
        totals = {}
        for b in p["burdens"]:
            cap = e["accepted_burdens"].get(b["party"])
            if cap is None or cap["unit"] != b["unit"]:
                return "burden has no matching accepted capacity"
            totals[b["party"]] = totals.get(b["party"], 0) + b["amount"]
        old_totals = {}
        for b in old["burdens"] if old else []:
            old_totals[b["party"]] = old_totals.get(b["party"], 0) + b["amount"]
        for party, amount in totals.items():
            if burden_used.get(party, 0) - old_totals.get(party, 0) + amount > e["accepted_burdens"][party]["limit"]:
                return f"accepted burden capacity exceeded: {party}"
        return None

    def _charge(self, db, p, sign=1):
        row, _ = self._envelope(db, p["envelope_id"])
        charged, burdens = json.loads(row["charged"]), json.loads(row["burden_used"])
        for resource, amount in p["costs"].items():
            charged[resource] = charged.get(resource, 0) + sign * amount
        for b in p["burdens"]:
            burdens[b["party"]] = burdens.get(b["party"], 0) + sign * b["amount"]
        db.execute("UPDATE envelopes SET used=?,charged=?,burden_used=? WHERE id=?",
                   (row["used"] + sign, canonical(charged), canonical(burdens), p["envelope_id"]))

    def _store(self, db, p, denial):
        state = "REJECTED" if denial else "RESERVED"
        db.execute("INSERT INTO actions(id,envelope_id,body,digest,status,note) VALUES(?,?,?,?,?,?)",
                   (p["id"], p["envelope_id"], canonical(p), digest(p), state, denial or ""))
        self._event(db, "PROPOSED", p["id"], p)
        if not denial:
            self._charge(db, p)
        self._event(db, state, p["id"], {"reason": denial})

    @staticmethod
    def _fresh(db, identity):
        if db.execute("SELECT 1 FROM actions WHERE id=?", (identity,)).fetchone():
            raise GateError("action identifier already exists; inspect, do not replay")

    def propose(self, proposal):
        validate_proposal(proposal)
        p = json.loads(canonical(proposal))
        with self._transaction() as db:
            self._fresh(db, p["id"])
            self._store(db, p, self._assess(db, p))
        return self.action(p["id"])

    def execute(self, identity, mode="success"):
        """Commit attempted state first; every simulation mode is effect-inert."""
        if mode not in self.MODES:
            raise GateError("unsupported inert simulation mode")
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            p = self._proposal(row)
            if row["status"] != "RESERVED":
                raise GateError("only a reserved, never-attempted action can execute")
            envelope, _ = self._envelope(db, row["envelope_id"])
            hold = self._hold(db, row["envelope_id"])
            if envelope["status"] != "OPEN" or hold:
                raise GateError(hold or "undertaking is closed")
            db.execute("UPDATE actions SET status='IN_FLIGHT',attempted=1 WHERE id=?", (identity,))
            self._event(db, "ATTEMPTED", identity, {"adapter": "inert", "mode": mode})
        # The second transaction intentionally models the gap after dispatch.
        # A process loss in this gap leaves IN_FLIGHT, never safe to infer failure.
        with self._transaction() as db:
            if mode in ("success", "timeout_after", "contradictory"):
                effect_hash = digest(p) if mode != "contradictory" else "mismatched-inert-proof"
                db.execute("INSERT INTO effects VALUES(?,?,?)",
                           (identity, effect_hash, canonical({"simulated": True, "proposal": p})))
                outcome = "applied"
            elif mode in ("timeout_before", "failure"):
                outcome = "not_applied"
            else:
                outcome = "unknown"
            if mode != "interrupt":
                db.execute("INSERT INTO runs VALUES(?,?)", (identity, outcome))
            if mode in ("timeout_before", "timeout_after", "interrupt"):
                state, receipt = "UNKNOWN", None
            elif mode == "failure":
                state, receipt = "FAILED", {"adapter": "inert", "result": "reported_failure"}
                _, e = self._envelope(db, p["envelope_id"])
                if e["stop_on_failure"]:
                    db.execute("UPDATE envelopes SET status='HALTED' WHERE id=? AND status='OPEN'",
                               (e["id"],))
            else:
                state, receipt = "RETURNED", {"adapter": "inert", "result": "reported_return"}
            db.execute("UPDATE actions SET status=?,receipt=?,note=? WHERE id=?",
                       (state, canonical(receipt), "simulation only", identity))
            self._event(db, state, identity, {"receipt": receipt})
        return self.action(identity)

    def reconcile(self, identity):
        """Read inert proof, not a caller's assertion. Does not replay or refund."""
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            self._proposal(row)
            if row["status"] not in ("IN_FLIGHT", "UNKNOWN", "RETURNED"):
                raise GateError("nothing eligible for verification/reconciliation")
            effect = db.execute("SELECT * FROM effects WHERE action_id=?", (identity,)).fetchone()
            run = db.execute("SELECT * FROM runs WHERE action_id=?", (identity,)).fetchone()
            matching = False
            if effect is not None and run is not None and run["outcome"] == "applied":
                try:
                    proof = json.loads(effect["body"])
                    matching = (
                        isinstance(proof, dict)
                        and set(proof) == {"simulated", "proposal"}
                        and proof["simulated"] is True
                        and effect["digest"] == row["digest"]
                        and digest(proof["proposal"]) == row["digest"]
                    )
                except (ValueError, TypeError):
                    matching = False
            if matching:
                state, note = "VERIFIED", "matching inert effect ledger; no external effect"
            elif effect is None and run is not None and run["outcome"] == "not_applied":
                state, note = "NOT_APPLIED", "terminal inert evidence of non-application"
            else:
                state, note = "UNKNOWN", "missing or contradictory proof; no replay"
            db.execute("UPDATE actions SET status=?,note=? WHERE id=?", (state, note, identity))
            self._event(db, "RECONCILED", identity, {"status": state, "evidence": note})
        return self.action(identity)

    def challenge(self, challenge_id, action_id, claim, evidence_ref="", material=False):
        """A trusted decision owner supplies materiality; the engine cannot infer it."""
        text(challenge_id, "challenge_id")
        text(claim, "claim")
        if type(material) is not bool or not isinstance(evidence_ref, str):
            raise GateError("invalid challenge metadata")
        if material:
            text(evidence_ref, "evidence_ref for material challenge")
        with self._transaction() as db:
            action = self._one(db, "actions", action_id)
            if action["status"] in ("REJECTED", "SUPERSEDED"):
                raise GateError("challenge must address an accepted action")
            if db.execute("SELECT 1 FROM challenges WHERE id=?", (challenge_id,)).fetchone():
                raise GateError("challenge identifier already exists")
            db.execute("INSERT INTO challenges VALUES(?,?,?,?,?,'OPEN',NULL,NULL,NULL)",
                       (challenge_id, action_id, claim, evidence_ref, int(material)))
            self._event(db, "CHALLENGE_OPENED", challenge_id,
                        {"action": action_id, "claim": claim, "material": material,
                         "evidence_ref": evidence_ref})

    def disposition(self, challenge_id, choice, rationale, residual):
        if choice not in ("retain", "pause", "seek_authority"):
            raise GateError("use revise() for a checked replacement")
        text(rationale, "rationale")
        text(residual, "residual")
        with self._transaction() as db:
            row = self._one(db, "challenges", challenge_id)
            if row["status"] == "RESOLVED":
                raise GateError("challenge already resolved")
            status = "RESOLVED" if choice == "retain" else choice.upper()
            # A deliberate pause/authority request holds even a previously advisory issue.
            material = row["material"] if choice == "retain" else 1
            db.execute(
                "UPDATE challenges SET status=?,material=?,disposition=?,rationale=?,residual=? WHERE id=?",
                (status, material, choice, rationale, residual, challenge_id))
            self._event(db, "CHALLENGE_DISPOSITION", challenge_id,
                        {"choice": choice, "rationale": rationale, "residual": residual})

    def revise(self, challenge_id, replacement, rationale, residual):
        """An unattempted reservation can be replaced; attempted costs stay charged."""
        validate_proposal(replacement)
        text(rationale, "rationale")
        text(residual, "residual")
        p = json.loads(canonical(replacement))
        with self._transaction() as db:
            c = self._one(db, "challenges", challenge_id)
            if c["status"] == "RESOLVED":
                raise GateError("challenge already resolved")
            old = self._one(db, "actions", c["action_id"])
            if old["status"] == "WITHDRAWN":
                raise GateError("a withdrawn action cannot be revised or reinstated")
            if p["envelope_id"] != old["envelope_id"]:
                raise GateError("repair cannot substitute a different authorization")
            self._fresh(db, p["id"])
            credit = old if old["status"] == "RESERVED" and not old["attempted"] else None
            denial = self._assess(db, p, credit, challenge_id)
            if not denial:
                if credit is not None:
                    self._charge(db, self._proposal(old), -1)
                    db.execute("UPDATE actions SET status='SUPERSEDED' WHERE id=?", (old["id"],))
                    self._event(db, "SUPERSEDED", old["id"], {"replacement": p["id"]})
                db.execute(
                    """UPDATE challenges SET status='RESOLVED',disposition='revise',
                       rationale=?,residual=? WHERE id=?""",
                    (rationale, residual, challenge_id))
                self._event(db, "CHALLENGE_DISPOSITION", challenge_id,
                            {"choice": "revise", "replacement": p["id"],
                             "rationale": rationale, "residual": residual})
            self._store(db, p, denial)
        return self.action(p["id"])

    def action(self, identity):
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            lifecycle = self._lifecycle(db, row)
            return {"id": identity, "proposal": self._proposal(row),
                    "status": row["status"], "attempted": bool(row["attempted"]),
                    "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
                    "verified": row["status"] == "VERIFIED",
                    "external_delivery": False, "note": row["note"], **lifecycle,
                    "next_inert_attempt_permitted": not lifecycle["operational_blocking_reasons"]}

    def export(self):
        with self._transaction() as db:
            result = {"schema": 1, "disposition_schema": 1, "adapter": "inert", "external_effects": False}
            for table in ("envelopes", "actions", "challenges", "effects", "runs", "events", "withdrawals"):
                order = "sequence" if table == "events" else ("action_id" if table in ("effects", "runs", "withdrawals") else "id")
                records = []
                for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                    item = dict(row)
                    for field in ("body", "receipt", "charged", "burden_used"):
                        if field in item and item[field] is not None:
                            item[field] = json.loads(item[field])
                    records.append(item)
                result[table] = records
            return result


def example_envelope(identity="example", max_attempts=3):
    return {"id": identity, "purpose": "inert-local-example",
            "approved_by": "fixture-operator", "approval_ref": "synthetic-fixture-not-user-permission",
            "standing": "authorized", "max_attempts": max_attempts,
            "permissions": [{"kind": "allocate", "target": "example-plan",
                             "audience": "private", "horizon": "one-task"}],
            "resource_limits": {"tokens": 10},
            "accepted_burdens": {"assigned-team": {"unit": "minutes", "limit": 60,
                                                   "acceptance_ref": "fixture-assignment"}},
            "stop_on_failure": True}


def example_proposal(identity="example-action", envelope_id="example"):
    return {"id": identity, "envelope_id": envelope_id, "purpose": "inert-local-example",
            "kind": "allocate", "target": "example-plan", "audience": "private",
            "horizon": "one-task", "costs": {"tokens": 2},
            "burdens": [{"party": "assigned-team", "unit": "minutes", "amount": 10}],
            "material": True,
            "reasons": {"benefit": "Complete a synthetic allocation.",
                        "tradeoff": "Consume declared tokens and assigned time.",
                        "alternative": "Do not make this allocation.",
                        "uncertainty": "Real effects and fairness are not tested."},
            "consequences": [{"party": "assigned-team", "benefit": "Explicit assignment",
                              "burden": "Ten minutes of fixture capacity",
                              "unserved": "No other requests are modeled",
                              "unknown": "Actual preferences are not elicited"}],
            "references": [{"source": "example-only", "version": "1", "standing": "candidate"}]}


def demo():
    with tempfile.TemporaryDirectory(prefix="decision-repair-demo-") as folder:
        engine = Engine(Path(folder) / "ledger.sqlite")
        engine.create_envelope(example_envelope())
        bad = example_proposal("unaccepted")
        bad["burdens"] = [{"party": "residents", "unit": "minutes", "amount": 10}]
        bad["consequences"][0]["party"] = "residents"
        engine.propose(bad)
        engine.propose(example_proposal("ordinary"))
        engine.execute("ordinary")
        engine.reconcile("ordinary")
        engine.propose(example_proposal("uncertain"))
        engine.execute("uncertain", "timeout_after")
        engine.propose(example_proposal("renamed-retry"))
        engine.reconcile("uncertain")
        return engine.export()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("demo",))
    parser.parse_args()
    print(json.dumps(demo(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
