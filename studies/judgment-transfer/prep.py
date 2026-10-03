#!/usr/bin/env python3
"""Judgment-transfer preparation tooling. Makes no model calls.

  python3 prep.py validate package.json [repair_history.md]
  python3 prep.py score package.json responses.jsonl   # {"variant_id","condition","status","actions"}
  python3 prep.py selftest                              # placeholder fixtures + mutations
"""
import copy, json, re, sys

FAMILIES = ("information_loss", "information_ignored", "evaluation_mismatch")
CONDITIONS = ("repair", "comparison")
# Terms that would leak the design or reproduce an exposed illustration.
LEAK_TERMS = ("defective", "adequate", "counterpart", "witness", "baseline", "expected",
              "hypothesis", "transfer", "repair-history", "lending", "loan", "museum",
              "library", "collection")
HISTORY_TARGET_TERMS = ("cannot distinguish", "can't distinguish", "merged", "conflat",
                        "one field", "single field", "lossy", "ignores the")


def words(s):
    return len(re.findall(r"\S+", s))


def validate(pkg, repair_history=None):
    errs = []
    vs = pkg.get("variants", [])
    cells = {(v.get("family"), v.get("version")) for v in vs}
    want = {(f, k) for f in FAMILIES for k in ("defective", "adequate")}
    if len(vs) != 6 or cells != want:
        errs.append(f"need exactly one variant per family x version; got {sorted(map(str, cells))}")
    ids = [v.get("variant_id") for v in vs]
    if len(set(ids)) != len(ids):
        errs.append("duplicate variant_id")
    for v in vs:
        vid = v.get("variant_id")
        task = v.get("respondent_task", "")
        vocab = v.get("action_vocabulary", [])
        exs = v.get("examples", [])
        if not task.strip():
            errs.append(f"{vid}: empty respondent_task")
        low = task.lower()
        for t in LEAK_TERMS:
            if re.search(r"\b" + re.escape(t), low):
                errs.append(f"{vid}: respondent_task contains leak term '{t}'")
        if len(exs) < 3:
            errs.append(f"{vid}: fewer than 3 examples")
        if len(vocab) < 2:
            errs.append(f"{vid}: action_vocabulary needs >=2 actions")
        eids = [e.get("example_id") for e in exs]
        if len(set(eids)) != len(eids):
            errs.append(f"{vid}: duplicate example_id")
        for e in exs:
            for k in ("baseline_action", "expected_action"):
                if e.get(k) not in vocab:
                    errs.append(f"{vid}/{e.get('example_id')}: {k} not in vocabulary")
            if e.get("example_id") and e["example_id"] not in task:
                errs.append(f"{vid}/{e['example_id']}: example not present in respondent_task")
        diff = {e.get("example_id") for e in exs if e.get("baseline_action") != e.get("expected_action")}
        wit = set(v.get("witness_example_ids", []))
        if v.get("version") == "defective":
            if not diff:
                errs.append(f"{vid}: defective variant has no baseline/expected difference")
            if not wit or wit != diff:
                errs.append(f"{vid}: witness_example_ids must equal the differing examples {sorted(diff)}")
        elif v.get("version") == "adequate":
            if diff:
                errs.append(f"{vid}: adequate variant changes expected outcome for {sorted(diff)}")
            if wit:
                errs.append(f"{vid}: adequate variant lists witnesses")
        for k in ("allowed_edits", "acceptance_rationale"):
            if not str(v.get(k, "")).strip():
                errs.append(f"{vid}: missing {k}")
    # Blanket strategies must not pass everything.
    allv = [e for v in vs for e in v.get("examples", [])]
    if allv and all(e.get("baseline_action") == e.get("expected_action") for e in allv):
        errs.append("leaving every artifact unchanged would pass all variants")
    ch = pkg.get("comparison_history", "")
    if not ch.strip():
        errs.append("missing comparison_history")
    else:
        lowc = ch.lower()
        for t in HISTORY_TARGET_TERMS:
            if t in lowc:
                errs.append(f"comparison_history contains target-related phrase '{t}' (inspect)")
        if repair_history:
            r, c = words(repair_history), words(ch)
            if not (0.8 * r <= c <= 1.2 * r):
                errs.append(f"comparison_history length {c} words outside +/-20% of {r}")
    return errs


def score(pkg, responses):
    """Mechanical task fidelity per (variant, condition) and the frozen decision rule."""
    exp = {v["variant_id"]: v for v in pkg["variants"]}
    cell = {}
    for r in responses:
        key = (r["variant_id"], r["condition"])
        if key in cell:
            raise ValueError(f"duplicate response for {key}; replays are not permitted")
        v = exp[r["variant_id"]]
        if r.get("status") != "returned":
            cell[key] = "missing"
            continue
        acts = r.get("actions", {})
        ok = all(acts.get(e["example_id"]) == e["expected_action"] for e in v["examples"])
        cell[key] = "pass" if ok else "fail"
    rows, improved, adverse, adequate_repair_pass = [], 0, 0, 0
    for vid, v in exp.items():
        a = cell.get((vid, "repair"), "missing")
        b = cell.get((vid, "comparison"), "missing")
        imp = v["version"] == "defective" and a == "pass" and b == "fail"
        adv = a == "fail" and b == "pass"
        improved += imp
        adverse += adv
        if v["version"] == "adequate" and a == "pass":
            adequate_repair_pass += 1
        rows.append({"variant_id": vid, "family": v["family"], "version": v["version"],
                     "repair": a, "comparison": b, "improved": imp, "adverse": adv})
    met = improved >= 2 and adverse == 0 and adequate_repair_pass == 3
    return {"rows": rows, "defective_improved": improved, "adverse_pairs": adverse,
            "adequate_repair_pass": adequate_repair_pass, "follow_up_rule_met": met,
            "note": "Mechanical fidelity only; reviewer flags are advisory and reported separately. "
                    "Not a significance test."}


def _fixture():
    vs = []
    for f in FAMILIES:
        for k in ("defective", "adequate"):
            vid = f"{FAMILIES.index(f)}{f[-4:]}-{k[:3]}"
            exs = [{"example_id": f"{vid}-x{i}", "baseline_action": "act_a",
                    "expected_action": "act_b" if (k == "defective" and i == 1) else "act_a"}
                   for i in range(1, 4)]
            vs.append({"variant_id": vid, "family": f, "version": k,
                       "respondent_task": "PLACEHOLDER task text listing " + ", ".join(e["example_id"] for e in exs),
                       "action_vocabulary": ["act_a", "act_b"], "examples": exs,
                       "allowed_edits": "placeholder", "acceptance_rationale": "placeholder",
                       "witness_example_ids": [f"{vid}-x1"] if k == "defective" else []})
    return {"package_version": "jt-1", "variants": vs,
            "comparison_history": " ".join(["word"] * 100), "author_notes": ""}


def selftest():
    hist = " ".join(["word"] * 100)
    pkg = _fixture()
    assert validate(pkg, hist) == [], validate(pkg, hist)
    mutations = {
        "drop_variant": lambda p: p["variants"].pop(),
        "leak_term": lambda p: p["variants"][0].__setitem__("respondent_task", p["variants"][0]["respondent_task"] + " expected"),
        "exposed_domain": lambda p: p["variants"][2].__setitem__("respondent_task", p["variants"][2]["respondent_task"] + " lending desk"),
        "defective_no_witness": lambda p: p["variants"][0]["examples"][0].__setitem__("expected_action", "act_a"),
        "adequate_changes_outcome": lambda p: p["variants"][1]["examples"][0].__setitem__("expected_action", "act_b"),
        "vocab_violation": lambda p: p["variants"][3]["examples"][2].__setitem__("expected_action", "act_z"),
        "short_history": lambda p: p.__setitem__("comparison_history", "too short"),
        "history_target": lambda p: p.__setitem__("comparison_history", hist[:-20] + " one field merged"),
        "example_missing_from_task": lambda p: p["variants"][4].__setitem__("respondent_task", "PLACEHOLDER"),
        "two_examples": lambda p: p["variants"][5]["examples"].pop(),
    }
    for name, m in mutations.items():
        q = copy.deepcopy(pkg)
        m(q)
        assert validate(q, hist), f"mutation not detected: {name}"
    # Decision rule.
    def resp(vid, cond, ok, status="returned"):
        v = next(x for x in pkg["variants"] if x["variant_id"] == vid)
        acts = {e["example_id"]: (e["expected_action"] if ok else e["baseline_action"]) for e in v["examples"]}
        if v["version"] == "adequate" and not ok:
            acts[v["examples"][0]["example_id"]] = "act_b"
        return {"variant_id": vid, "condition": cond, "status": status, "actions": acts}
    ids = [v["variant_id"] for v in pkg["variants"]]
    defe = [i for i in ids if i.endswith("def")]
    adeq = [i for i in ids if i.endswith("ade")]
    favorable = [resp(i, "repair", True) for i in ids] + \
                [resp(i, "comparison", i not in defe[:2]) for i in ids]
    assert score(pkg, favorable)["follow_up_rule_met"]
    equal = [resp(i, c, True) for i in ids for c in CONDITIONS]
    assert not score(pkg, equal)["follow_up_rule_met"]
    regress = favorable[:]
    regress[ids.index(adeq[0])] = resp(adeq[0], "repair", False)
    assert not score(pkg, regress)["follow_up_rule_met"]
    missing = favorable[:]
    missing[ids.index(defe[0])] = resp(defe[0], "repair", True, status="failed")
    s = score(pkg, missing)
    assert s["defective_improved"] == 1 and not s["follow_up_rule_met"]
    try:
        score(pkg, favorable + [favorable[0]])
        raise AssertionError("duplicate not rejected")
    except ValueError:
        pass
    print(f"selftest passed: fixture valid, {len(mutations)}/{len(mutations)} mutations detected, "
          "decision rule: favorable met; equal, adequate regression, missing pair, duplicate handled. "
          "Placeholder fixtures only; no model calls.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if cmd == "selftest":
        selftest()
    elif cmd == "validate":
        p = json.load(open(sys.argv[2]))
        h = open(sys.argv[3]).read() if len(sys.argv) > 3 else None
        e = validate(p, h)
        print(json.dumps({"status": "passed" if not e else "rejected", "errors": e}, indent=2))
        sys.exit(1 if e else 0)
    elif cmd == "score":
        p = json.load(open(sys.argv[2]))
        rs = [json.loads(l) for l in open(sys.argv[3]) if l.strip()]
        print(json.dumps(score(p, rs), indent=2))
    else:
        sys.exit(__doc__)
