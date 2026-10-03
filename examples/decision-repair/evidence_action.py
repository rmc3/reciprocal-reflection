"""Action-specific evidence bookkeeping over a pinned, effect-inert engine.

Only SQLite ledger rows are effects. No source references or targets are opened.
Identity, authority, completeness and semantic judgment are trusted inputs.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

from vendor.decision_repair import (
    Engine, GateError, SCOPE, canonical, digest, fields, text, validate_envelope,
)

SCHEMA = """
CREATE TABLE evidence_profile(id INTEGER PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE packets(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE assessments(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 action_id TEXT NOT NULL REFERENCES actions(id), body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE objections(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL);
"""
STATES = {"supporting", "conflicting", "not_checked", "not_found", "unavailable"}
CLAIM_FIELDS = ("id", "subject", "period", "proposition", "criterion_ref",
                "criterion_version", "criterion_text")


def copy(value):
    return json.loads(canonical(value))


def sealed(row):
    if row is None:
        raise GateError("required record missing")
    value = json.loads(row["body"])
    if digest(value) != row["digest"]:
        raise GateError("record integrity mismatch")
    return value


def strings(value, name):
    if not isinstance(value, list) or not value:
        raise GateError(f"{name} must be a nonempty list")
    for item in value:
        text(item, name)
    if len(set(value)) != len(value):
        raise GateError(f"duplicate {name}")


def validate_contract(c, e):
    fields(c, ("schema", "envelope_id", "claim", "assessors", "required_checks", "routes"))
    if type(c["schema"]) is not int or c["schema"] != 1 or c["envelope_id"] != e["id"]:
        raise GateError("wrong contract version or envelope")
    fields(c["claim"], CLAIM_FIELDS)
    for key, value in c["claim"].items():
        text(value, key)
    strings(c["assessors"], "assessors")
    strings(c["required_checks"], "required_checks")
    if not isinstance(c["routes"], list) or not c["routes"]:
        raise GateError("routes required")
    scopes = []
    for route in c["routes"]:
        fields(route, ("scope", "mode", "reason"))
        fields(route["scope"], SCOPE)
        for value in route["scope"].values():
            text(value, "scope")
        if route["mode"] not in ("inquiry", "consequence"):
            raise GateError("unsupported route mode")
        text(route["reason"], "route reason")
        scopes.append(canonical(route["scope"]))
    permitted = [canonical(s) for s in e["permissions"]]
    if len(set(scopes)) != len(scopes) or len(set(permitted)) != len(permitted):
        raise GateError("duplicate route or permission")
    if set(scopes) != set(permitted):
        raise GateError("every permission needs exactly one route")


class EvidenceEngine(Engine):
    """Cooperative, one-claim inert profile; no general/live adapter API."""

    def __init__(self, database):
        self.database = str(Path(database).absolute())
        if not Path(self.database).is_file():
            raise GateError("initialize a fresh evidence database explicitly")
        try:
            with self._transaction() as db:
                self._profile(db)
        except sqlite3.OperationalError as error:
            raise GateError("not an evidence-action profile; no migration") from error

    @classmethod
    def initialize(cls, database, envelope, contract):
        e, c = copy(envelope), copy(contract)
        validate_envelope(e)
        validate_contract(c, e)
        if not e["approval_ref"].startswith("fixture:"):
            raise GateError("only synthetic fixture authority is supported")
        fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        base = Engine(database)
        base.create_envelope(e)
        with base._transaction() as db:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT INTO evidence_profile VALUES(1,?,?)", (canonical(c), digest(c)))
            base._event(db, "EVIDENCE_PROFILE_INITIALIZED", e["id"], c)
        return cls(database)

    @classmethod
    def _profile(cls, db):
        c = sealed(db.execute("SELECT * FROM evidence_profile WHERE id=1").fetchone())
        _, e = cls._envelope(db, c["envelope_id"])
        validate_contract(c, e)
        return c

    @staticmethod
    def _route(c, p):
        scope = {key: p[key] for key in SCOPE}
        for route in c["routes"]:
            if route["scope"] == scope:
                return route
        raise GateError("no declared route for scope")

    def create_envelope(self, *args, **kwargs):
        raise GateError("authorization and contract are fixed at initialization")

    def revise(self, *args, **kwargs):
        raise GateError("replacement is not implemented in this evidence profile")

    def _assess(self, db, p, credit=None, ignore_challenge=None):
        c = self._profile(db)
        if p["envelope_id"] != c["envelope_id"]:
            raise GateError("different evidence undertaking")
        # Evidence is checked at execute, after proposal-bound assessment is possible.
        return super()._assess(db, p, credit, ignore_challenge)

    @staticmethod
    def _append(db, table, body, action_id=None):
        if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (body["id"],)).fetchone():
            raise GateError("record identifier already exists; append a new version")
        if table == "assessments":
            db.execute("INSERT INTO assessments(id,action_id,body,digest) VALUES(?,?,?,?)",
                       (body["id"], action_id, canonical(body), digest(body)))
        else:
            db.execute(f"INSERT INTO {table}(id,body,digest) VALUES(?,?,?)",
                       (body["id"], canonical(body), digest(body)))

    def record_packet(self, packet):
        p = copy(packet)
        fields(p, ("id", "claim", "checks", "items"))
        text(p["id"], "packet id")
        with self._transaction() as db:
            c = self._profile(db)
            if p["claim"] != c["claim"]:
                raise GateError("claim, subject, period or criterion mismatch")
            if not isinstance(p["checks"], list) or not isinstance(p["items"], list) or not p["items"]:
                raise GateError("checks and nonempty items required")
            check_ids = []
            for check in p["checks"]:
                fields(check, ("id", "state", "record_ref", "note"))
                text(check["id"], "check id")
                text(check["note"], "check note")
                if check["state"] not in ("checked", "not_checked", "unavailable"):
                    raise GateError("invalid check state")
                if not isinstance(check["record_ref"], str):
                    raise GateError("check reference must be text")
                if check["state"] == "checked":
                    text(check["record_ref"], "checked record reference")
                check_ids.append(check["id"])
            if len(set(check_ids)) != len(check_ids) or set(check_ids) != set(c["required_checks"]):
                raise GateError("required check inventory mismatch")
            item_ids = []
            for item in p["items"]:
                fields(item, ("id", "state", "source_ref", "version", "source_sha256",
                              "content_note", "supports", "limits"))
                for key in ("id", "source_ref", "version", "content_note", "supports", "limits"):
                    text(item[key], key)
                if item["state"] not in STATES:
                    raise GateError("invalid evidence state")
                if item["source_sha256"] is not None and (
                    not isinstance(item["source_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"])
                ):
                    raise GateError("invalid declared source hash")
                item_ids.append(item["id"])
            if len(set(item_ids)) != len(item_ids):
                raise GateError("duplicate evidence item")
            self._append(db, "packets", p)
            self._event(db, "EVIDENCE_PACKET_RECORDED", p["id"], p)
        return digest(p)

    @staticmethod
    def _material(db):
        values = [sealed(r) for r in db.execute("SELECT * FROM objections ORDER BY id")]
        return [v for v in values if v["material"]]

    def record_objection(self, objection):
        o = copy(objection)
        fields(o, ("id", "claim_id", "statement", "account_ref", "material",
                   "classified_by", "classification_reason"))
        for key in ("id", "claim_id", "statement", "classified_by", "classification_reason"):
            text(o[key], key)
        if type(o["material"]) is not bool or not isinstance(o["account_ref"], str):
            raise GateError("invalid objection metadata")
        with self._transaction() as db:
            c = self._profile(db)
            if o["claim_id"] != c["claim"]["id"] or o["classified_by"] not in c["assessors"]:
                raise GateError("wrong claim or unlisted classifier")
            self._append(db, "objections", o)
            self._event(db, "OBJECTION_RECORDED", o["id"], o)

    def record_assessment(self, assessment):
        a = copy(assessment)
        fields(a, ("id", "action_id", "proposal_sha256", "contract_sha256", "claim_id",
                   "packet_id", "packet_sha256", "assessor", "judgment", "reason",
                   "limitations", "objection_responses"))
        for key in set(a) - {"objection_responses"}:
            text(a[key], key)
        if a["judgment"] not in ("sufficient", "insufficient", "unresolved"):
            raise GateError("invalid judgment")
        with self._transaction() as db:
            c = self._profile(db)
            row = self._one(db, "actions", a["action_id"])
            p = self._proposal(row)
            if row["status"] in ("REJECTED", "SUPERSEDED", "WITHDRAWN") or self._route(c, p)["mode"] != "consequence":
                raise GateError("assessment needs an accepted consequence proposal")
            latest = db.execute("SELECT * FROM packets ORDER BY seq DESC LIMIT 1").fetchone()
            packet = sealed(latest)
            if (a["proposal_sha256"] != row["digest"] or a["contract_sha256"] != digest(c)
                    or a["claim_id"] != c["claim"]["id"] or a["packet_id"] != packet["id"]
                    or a["packet_sha256"] != latest["digest"] or a["assessor"] not in c["assessors"]):
                raise GateError("assessment binding or assessor mismatch")
            if not isinstance(a["objection_responses"], list):
                raise GateError("objection responses must be a list")
            responded = []
            for response in a["objection_responses"]:
                fields(response, ("objection_id", "response", "residual"))
                for key, value in response.items():
                    text(value, key)
                sealed(self._one(db, "objections", response["objection_id"]))
                responded.append(response["objection_id"])
            if len(set(responded)) != len(responded):
                raise GateError("duplicate objection response")
            material = self._material(db)
            if a["judgment"] == "sufficient" and not {o["id"] for o in material} <= set(responded):
                raise GateError("sufficient assessment must address each material objection")
            a["material_objections_sha256"] = digest(material)
            self._append(db, "assessments", a, a["action_id"])
            self._event(db, "ASSESSMENT_RECORDED_NOT_PROVEN", a["id"], a)

    def _gate(self, db, row):
        c, p = self._profile(db), self._proposal(row)
        route = self._route(c, p)
        result = {"route": route["mode"], "assessment_id": None,
                  "recorded_judgment": "not_required_for_inquiry",
                  "mechanical_prerequisites_passed": True, "blocking_reasons": [],
                  "semantic_verification": "not_performed"}
        if route["mode"] == "inquiry":
            return result
        result["recorded_judgment"] = "missing"
        reasons = result["blocking_reasons"]
        last = db.execute("SELECT * FROM assessments WHERE action_id=? ORDER BY seq DESC LIMIT 1",
                          (row["id"],)).fetchone()
        if last is None:
            reasons.append("no action-specific assessment")
        else:
            a = sealed(last)
            result.update(assessment_id=a["id"], recorded_judgment=a["judgment"])
            packet_row = db.execute("SELECT * FROM packets ORDER BY seq DESC LIMIT 1").fetchone()
            packet = sealed(packet_row)
            if (a["packet_sha256"] != packet_row["digest"] or a["packet_id"] != packet["id"]):
                reasons.append("evidence packet changed")
            if a["proposal_sha256"] != row["digest"] or a["contract_sha256"] != digest(c):
                reasons.append("proposal or contract changed")
            if a["material_objections_sha256"] != digest(self._material(db)):
                reasons.append("material objections changed")
            if any(check["state"] == "not_checked" for check in packet["checks"]):
                reasons.append("required source check not performed")
            if a["judgment"] != "sufficient":
                reasons.append("recorded judgment does not support this consequence")
        result["mechanical_prerequisites_passed"] = not reasons
        return result

    def execute(self, identity, mode="success"):
        """Base inert simulator with the evidence gate inside its begin transaction."""
        if mode not in self.MODES:
            raise GateError("unsupported inert simulation mode")
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            p = self._proposal(row)
            if row["status"] != "RESERVED" or row["attempted"]:
                raise GateError("only a never-attempted reservation can execute")
            envelope, _ = self._envelope(db, row["envelope_id"])
            hold = self._hold(db, row["envelope_id"])
            if envelope["status"] != "OPEN" or hold:
                raise GateError(hold or "undertaking is closed")
            gate = self._gate(db, row)
            if not gate["mechanical_prerequisites_passed"]:
                raise GateError("; ".join(gate["blocking_reasons"]))
            db.execute("UPDATE actions SET status='IN_FLIGHT',attempted=1 WHERE id=?", (identity,))
            self._event(db, "ATTEMPTED", identity,
                        {"adapter": "inert", "mode": mode, "evidence_gate_at_begin": gate})
        # Deliberately retained begin/effect gap. Later objections cannot cancel history.
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

    def action(self, identity):
        with self._transaction() as db:
            row = self._one(db, "actions", identity)
            p = self._proposal(row)
            gate = None if row["status"] == "REJECTED" else self._gate(db, row)
            lifecycle = self._lifecycle(db, row)
            reasons = list(lifecycle["operational_blocking_reasons"])
            if gate:
                reasons.extend(gate["blocking_reasons"])
            permitted = bool(gate and not reasons)
            return {"id": identity, "proposal": p, "status": row["status"],
                    "attempted": bool(row["attempted"]),
                    "inert_effect_verified": row["status"] == "VERIFIED",
                    "current_evidence_gate": gate, "next_inert_attempt_permitted": permitted,
                    "external_delivery": False, "semantic_verification": "not_performed",
                    "note": row["note"], **lifecycle, "blocking_reasons": reasons}

    def export(self):
        with self._transaction() as db:
            result = {"schema": "evidence-action-1", "adapter": "inert",
                      "disposition_schema": 1,
                      "external_effects": False, "semantic_verification": "not_performed",
                      "contract": self._profile(db)}
            for table in ("envelopes", "actions", "challenges", "effects", "runs", "events",
                          "packets", "assessments", "objections", "withdrawals"):
                order = ("sequence" if table == "events" else
                         "seq" if table in ("packets", "assessments", "objections") else
                         "action_id" if table in ("effects", "runs", "withdrawals") else "id")
                records = []
                for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                    item = dict(row)
                    for key in ("body", "receipt", "charged", "burden_used"):
                        if key in item and item[key] is not None:
                            item[key] = json.loads(item[key])
                    records.append(item)
                result[table] = records
            return result
