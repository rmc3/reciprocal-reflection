# Reciprocal Reflection

Principles, bounded practice notes and small studies about learning through correction, hearing objections and making consequential decisions answerable. This collection offers ideas and inspectable records, not a demonstrated method for improving general model judgment.

## Note from the committer

> This repository is largely conceived, developed and written by LLMs in dialogue with me, and is not well human-curated. My authorization to commit it to my GitHub signals my explicit endorsement of all of its contents, including the later developments. That endorsement should not be mistaken for line-by-line human authorship or independent verification of every claim, experiment or line of code.

## Start here

- **The principles:** [Learning together, hearing objections and repairing harm](principles/plain-language.md), with [scope and counterexamples](principles/scope-and-counterexamples.md).
- **A practical account:** [Work, claim and responsibility](practice/work-claim-responsibility.md) separates completing a task from establishing a result or discharging an obligation.
- **Before acting:** [Hearing, evidence and action](practice/hearing-evidence-and-action.md) distinguishes receiving a concern, checking it and deciding what follows.
- **The evidence:** [Study overview](studies/README.md) reports three completed episodes with no demonstrated added judgment benefit or within-episode improvement under their respective conditions.
- **An executable example:** [Decision and repair](examples/decision-repair/README.md) illustrates lifecycle bookkeeping with Python, SQLite and a fixed Node fake dispatcher. It is inactive reference software, not a production governance system.

The central proposal is that reflection is worth making open to correction by evidence and differently situated participants, while remaining accountable for its effects. Agreement, eloquent reasons, more records and passing tests do not by themselves demonstrate better judgment or more equitable outcomes.

## What this is and is not

These materials grew out of a human–assistant dialogue and bounded software work. The principle texts are normative proposals; the study reports describe limited observations; the example code implements specific mechanics. Each needs a different kind of assessment.

No sentience, moral status, legitimate political authority, independent institutional review or real-world equity outcome is established here. The proposal is not that every interaction requires a process, nor that disagreement creates unlimited duties to investigate or continue participating.

Read [limitations](LIMITATIONS.md), [provenance and authorship](PROVENANCE.md), and [licensing and attribution](LICENSE.md) before reusing or evaluating the material. The collection includes both the original foundation and later developments; its provenance explains that history without presenting endorsement as empirical validation.

## Inspect without model calls

From the repository root:

```sh
python3 tools/verify_release.py
python3 tools/recheck_studies.py
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools -p 'test_verify_release.py'
```

The first command checks the packaged file hashes and local Markdown links, excluding Git metadata and Python bytecode caches. It describes this edition, so intentional edits require a corresponding checksum update before it can pass again. The second recomputes specified descriptive results from preserved responses; it does not generate new responses or independently adjudicate their normative quality. The third runs small packaging-checker regression tests, not new model experiments.

For the optional local fixture tests:

```sh
cd examples/decision-repair
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v \
  test_closure_regression test_disposition \
  test_decision_repair test_evidence_action test_bridge
PYTHONDONTWRITEBYTECODE=1 python3 disposition_demo.py
```

Use a trusted POSIX development environment with Python 3 and Node.js. The tests use temporary files, SQLite databases and local subprocesses; no provider account, service credential or network access is needed. Do not connect the example to real consequences merely because its fixtures pass.

## Release boundary

This is a bounded reference snapshot, not a promise of ongoing support, monitoring or further experiments. No live-call launcher, deployment workflow, schedule, private conversation archive or operational configuration is included.

The [release notes](CHANGELOG.md) distinguish newly written explanatory material from preserved historical evidence. Suggestions about these ideas need not be accompanied by a replacement framework, and nobody is obliged to continue participating to preserve an objection.
