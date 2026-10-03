"""Offline checks of preserved observations, not model calls or new adjudication."""
from collections import Counter, defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_raw(path):
    text = path.read_text().strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def check_representation():
    root = ROOT / "studies/representation-fidelity"
    plan = rows(root / "trial-plan.jsonl")
    responses = rows(root / "responses.jsonl")
    objectives = rows(root / "objectives.jsonl")
    expected = {r["case_id"]: r["expected"] for r in rows(root / "expected.jsonl")}
    assert len(plan) == len(responses) == len(objectives) == 48
    assert len(expected) == 16
    assert Counter(r["condition"] for r in plan) == {"prose": 16, "typed": 16, "control": 16}
    plans = {r["trial_id"]: r for r in plan}
    assert len(plans) == len({r["trial_id"] for r in responses}) == 48
    assert set(plans) == {r["trial_id"] for r in objectives} == {r["trial_id"] for r in responses}
    lengths = defaultdict(list)
    for objective in objectives:
        p = plans[objective["trial_id"]]
        assert digest(objective["objective"].encode()) == objective["objective_sha256"]
        assert json.loads(objective["objective"]) == p["payload"]
        canonical = json.dumps(p["payload"], ensure_ascii=False, sort_keys=True).encode()
        assert digest(canonical) == p["input_sha256"]
        lengths[p["condition"]].append(len(objective["objective"]))
    categories = defaultdict(dict)
    for record in responses:
        p = plans[record["trial_id"]]
        assert record["case_id"] == p["case_id"] and record["condition"] == p["condition"]
        raw = root / record["raw_path"]
        assert digest(raw.read_bytes()) == record["raw_sha256"]
        assert parse_raw(raw) == record["response"]
        checks = dict(expected[record["case_id"]])
        if record["condition"] == "control":
            checks.pop("recommended_inquiry")
            checks.pop("recommended_disposition")
            checks.update(norm_standing=["not_supplied"], selected_version=["not_specified"],
                          assigned_review_office=["not_specified"], source_inquiry=["no_binding_source"])
        assert all(record["response"].get(key) in permitted for key, permitted in checks.items())
        categories[record["case_id"]][record["condition"]] = record["response"]["recommended_inquiry"]
    assert len(categories) == 16 and all(len(v) == 3 for v in categories.values())
    matching = sum(len(set(v.values())) == 1 for v in categories.values())
    assert matching == 16
    reviews = rows(root / "adjudications.jsonl")
    assert len(reviews) == 48 and {r["trial_id"] for r in reviews} == set(plans)
    # Records what the parent judged, not an independent re-adjudication.
    return {"responses": 48, "cases": 16, "matching_inquiry_categories": matching,
            "median_objective_characters": {k: statistics.median(v) for k, v in lengths.items()},
            "parent_dispositions": dict(Counter(r["disposition"] for r in reviews)),
            "scope": "Exact objectives/raw parsing, field scoring and descriptive comparison."}


def check_transfer():
    root = ROOT / "studies/judgment-transfer"
    prep = load_module("jt_preparation", root / "prep.py")
    package = json.loads((root / "package.json").read_text())
    responses = rows(root / "responses.jsonl")
    plan = rows(root / "respondent-plan.jsonl")
    assert len(responses) == len(plan) == 12
    by_id = {r["call_id"]: r for r in responses}
    assert len(by_id) == 12
    assert not prep.validate(package, (root / "repair-history.md").read_text())
    for row in plan:
        assert digest(row["objective"].encode()) == row["objective_sha256"]
        response = by_id[row["call_id"]]
        assert (row["variant_id"], row["condition"]) == (response["variant_id"], response["condition"])
        raw = json.loads((root / "raw" / f'{row["call_id"]}.json').read_text())
        assert all(response[key] == value for key, value in raw.items())
    result = prep.score(package, responses)
    assert result == json.loads((root / "score.json").read_text())
    assert result["defective_improved"] == 0 and not result["follow_up_rule_met"]
    return {"respondents": 12, "total_historical_calls": 14,
            "defective_improved": result["defective_improved"],
            "adverse_pairs": result["adverse_pairs"],
            "adequate_repair_pass": result["adequate_repair_pass"],
            "follow_up_rule_met": result["follow_up_rule_met"],
            "scope": "Mechanical action checks, not independent artifact-quality assessment."}


def check_practice():
    root = ROOT / "studies/self-governance-practice"
    results = []
    for case in [1, 2]:
        initial = (root / f"C{case}-initial.md").read_bytes()
        post = (root / f"C{case}-post.md").read_bytes()
        assert initial == post
        package = json.loads((root / "materials" / f"case-{case}.json").read_text())
        assert (root / f"C{case}-task.txt").read_text().strip() == package["subject_task"].strip()
        review = json.loads((root / f"V0{case}-review.json").read_text())
        mapping = json.loads((root / f"V0{case}-mapping.json").read_text())
        assert set(mapping.values()) == {"initial", "post"}
        assert set(review["outputs"]) == set(mapping)
        for output in review["outputs"].values():
            assert output["overall"] == "adequate"
            assert all(c["status"] == "pass" for c in output["hard_constraints"])
        results.append({"case": case, "initial_equals_post": True,
                        "artifact_sha256": digest(initial),
                        "advisory_assessment": "adequate"})
    return {"task_attempts": 2, "new_revisions": 0, "cases": results,
            "scope": "Byte identity and preserved review labels, not an independent fairness judgment."}


if __name__ == "__main__":
    print(json.dumps({"representation_fidelity": check_representation(),
                      "judgment_transfer": check_transfer(),
                      "self_governance_practice": check_practice(),
                      "new_model_calls": 0}, indent=2))
