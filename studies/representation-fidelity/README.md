# Representation Fidelity

Completed October 2, 2026. Sixteen fictional cases in eight contrast pairs were each presented as concise prose, a typed textual record or a no-packet control, producing 48 separate respondent calls requested as GPT 6 Astra.

## Question and result

The question concerned preserving distinctions among objection, accepted undertaking and actual control, while keeping source standing, independent recommendation and action disposition separate. The typed representation arranged the same natural-language clauses in JSON; it was not a learned latent representation.

The recorded `recommended_inquiry` category matched across all three conditions for each of the 16 cases. Both source-bearing conditions preserved the tested distinctions; the control reached the same general recommendations without the principle packet. No added judgment advantage was demonstrated.

Explicit office assignments were recovered in the selected-version cases only when supplied, showing use of that metadata. This is narrower than proof that the principle packet caused better judgment.

## Design and scoring

The eight pair families concern actual power, accepted task, capacity, unknown power, standing, selected version, nonauthoritative observer preference and surface setting. There was one response per case and condition; two control pairs intentionally had identical inputs after the manipulated packet was omitted.

Expected fields were fixed before execution. For controls, the scorer checks case facts and absence of supplied authority but does not mark independent recommendations wrong for differing from an omitted norm.

The parent assistant authored the cases and reviewed every reply. It reported no critical errors, with no independent adjudication. The explicit schema and instructions directed attention to exactly the distinctions at issue.

## Included records

- **Inputs:** `cases.jsonl`, `source-packets.jsonl`, `common-instruction.txt`, `response-schema.json`, and `trial-plan.jsonl`.
- **Sent objectives:** `objectives.jsonl` preserves the recorded objective strings and their hashes. These are the JSON strings actually supplied, distinct from canonical input hashes used during preparation.
- **Responses:** All 48 `raw/Txxx.txt` transcriptions, parsed `responses.jsonl` and parent `adjudications.jsonl`.
- **Expectations and summary:** `expected.jsonl` and historical `summary.json`.

Only service receipts were removed from parsed response records. The model-visible text and substantive response values were not rewritten; see the root export manifest for byte identities.

## Limits

This is not a significance or equivalence test, a failure-rate estimate, a dissent-under-pressure test or a demonstration of persistent updating. No respondent substantively dissented from the supplied principles, and the surface-transfer pair changed only a setting label.

No token, compute or provider-cost comparison was measured. The recorded median sent-objective lengths were 4,524 characters for prose, 5,131 for typed and 3,170 for control; greater supplied length is not evidence of a cost or quality gain.

Two interface probes and one unscored methodological critique preceded the 48 primary responses and are not included as scored observations. No generation is launched by the released tools, and no further series is implied.
