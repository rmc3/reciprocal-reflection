# Decision and Repair: Inactive Reference Example

This Python/SQLite example and fixed Node fake dispatcher demonstrate selected bookkeeping distinctions from the practice notes. They do not authenticate authority, judge whether evidence is adequate, represent affected parties or make a process fair.

## What the example does

- **Cumulative accounting:** Reservations and attempted actions remain within one declared undertaking. Renaming an action does not reset that undertaking's capacity.
- **Uncertain execution:** An attempted action with an unknown outcome is not automatically replayed or refunded.
- **Separate observations:** Acceptance of a dispatch, observed completion and verified fixture output are different facts.
- **Closure:** A late failure is retained without overwriting an explicitly closed undertaking.
- **Withdrawal:** A never-attempted reservation can be withdrawn and released without erasing evidence or objections. This does not cancel an attempted action or settle its underlying claim.

The code contains an engine, an evidence profile and a fake-dispatch profile. The evidence profile and bridge are separate demonstrations, not an integrated live consequence path.

## Run in a disposable development environment

Python 3 and Node.js are required. File-guard behavior uses POSIX facilities, and Windows portability is not claimed.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  test_closure_regression test_disposition \
  test_decision_repair test_evidence_action test_bridge
PYTHONDONTWRITEBYTECODE=1 python3 disposition_demo.py
node bridge.mjs demo
```

The demos use temporary directories and simulated effects. A printed output path may refer to a directory already removed at exit; it is not a retained deliverable.

The historical combined development suite had 205 test methods: 32 added checks and 173 inherited checks. The public review package's fresh verification is recorded separately in [validation](../../VALIDATION.md); repeated runs are not new independent evidence of improvement.

## Trust boundary

Use trusted local fixtures only. Caller labels, approval references, evidence accounts and reasons are supplied data, not authenticated consent or substantive validation.

An operator who controls the source or database can bypass or rewrite the mechanism. There is no protected credential, capability sandbox, tamperproof audit log or global guard over other tools.

A permit is checked at commitment to execute; later closure does not atomically revoke an already issued permit in a separate process. Fixture hashes verify known bytes, not whether an arbitrary real-world task was done correctly.

The negative-control tests intentionally show that structurally valid but substantively weak accounts can pass. Empty objection tables do not establish adequate hearing, and `review_assignment: not_represented` is not an accepted reviewer or monitoring service.

## Provenance

All included implementation and test files are byte-identical copies of the final historical disposition snapshot, including its modified engine under `vendor/`. Here, `vendor/` contains an earlier assistant-authored component from the same work, not a claim of third-party origin.

No implementation behavior was changed for this export. The README is newly written, old operational records were omitted, and the [export manifest](../../EXPORT-MANIFEST.json) supplies source and copy hashes.
