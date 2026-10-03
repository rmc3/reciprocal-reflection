"""Explicitly synthetic software fixtures; no real people or debt decisions."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from evidence_action import EvidenceEngine, GateError, copy, digest


def scope(kind):
    return {"kind": kind, "target": "fixture:subject-A:period-1",
            "audience": "private-fixture", "horizon": "one-inert-undertaking"}


def envelope(limit=4):
    return {"id": "fixture", "purpose": "evidence-boundary-fixture",
            "approved_by": "fixture-owner", "approval_ref": "fixture:local-only",
            "standing": "authorized", "permissions": [scope("inspect"), scope("apply")],
            "max_attempts": limit, "resource_limits": {"steps": 4},
            "accepted_burdens": {}, "stop_on_failure": True}


def contract():
    return {"schema": 1, "envelope_id": "fixture",
            "claim": {"id": "eligibility", "subject": "subject-A", "period": "period-1",
                      "proposition": "The fixture subject meets the declared eligibility criterion.",
                      "criterion_ref": "fixture:rule", "criterion_version": "1",
                      "criterion_text": "An attributed assessment of the named record is required."},
            "assessors": ["fixture-owner"], "required_checks": ["existing-records"],
            "routes": [
                {"scope": scope("inspect"), "mode": "inquiry",
                 "reason": "An authorized inert inspection is not an eligibility decision."},
                {"scope": scope("apply"), "mode": "consequence",
                 "reason": "Applying the fixture decision requires its own assessment."}]}


def proposal(identity="apply-1", kind="apply"):
    return {"id": identity, "envelope_id": "fixture", "purpose": "evidence-boundary-fixture",
            **scope(kind), "costs": {"steps": 1}, "burdens": [], "material": False,
            "reasons": {}, "consequences": []}


def packet(identity="packet-1", check_state="checked", evidence_state="supporting"):
    return {"id": identity, "claim": copy(contract()["claim"]),
            "checks": [{"id": "existing-records", "state": check_state,
                        "record_ref": "fixture:checked-record" if check_state == "checked" else "",
                        "note": "Operator-supplied search account, not independently verified."}],
            "items": [{"id": "item-1", "state": evidence_state, "source_ref": "fixture:record",
                       "version": "1", "source_sha256": None,
                       "content_note": "Synthetic record says the subject meets the criterion.",
                       "supports": "Potential support for eligibility, not permission to act.",
                       "limits": "The code does not inspect or judge the synthetic account."}]}


def assessment(p=None, pack=None, identity="assessment-1", judgment="sufficient", responses=None):
    p = proposal() if p is None else p
    pack = packet() if pack is None else pack
    return {"id": identity, "action_id": p["id"], "proposal_sha256": digest(p),
            "contract_sha256": digest(contract()), "claim_id": "eligibility",
            "packet_id": pack["id"], "packet_sha256": digest(pack), "assessor": "fixture-owner",
            "judgment": judgment, "reason": "Fixture owner attributes this judgment to the named packet.",
            "limitations": "No semantic verification or real-world outcome is claimed.",
            "objection_responses": [] if responses is None else responses}


def objection(identity="objection-1", material=True):
    return {"id": identity, "claim_id": "eligibility",
            "statement": "The evidence may not support this period.",
            "account_ref": "", "material": material, "classified_by": "fixture-owner",
            "classification_reason": "Recorded allegation may change reliance; not established fact."}


def demo():
    with TemporaryDirectory() as folder:
        engine = EvidenceEngine.initialize(Path(folder) / "ledger.sqlite", envelope(), contract())
        engine.propose(proposal())
        blocked = engine.action("apply-1")
        try:
            engine.execute("apply-1")
        except GateError as error:
            denial = str(error)
        engine.propose(proposal("inspect-1", "inspect"))
        engine.execute("inspect-1")
        inquiry = engine.reconcile("inspect-1")
        after_inquiry = engine.action("apply-1")
        engine.record_packet(packet())
        engine.record_assessment(assessment())
        ready = engine.action("apply-1")
        engine.execute("apply-1")
        completed = engine.reconcile("apply-1")
        engine.record_objection(objection())
        challenged = engine.action("apply-1")
        return {"initial": blocked, "withheld_because": denial,
                "inquiry_completed": inquiry, "after_inquiry": after_inquiry,
                "assessment_recorded": ready, "inert_effect_completed": completed,
                "late_objection": challenged, "ledger": engine.export()}


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2))
