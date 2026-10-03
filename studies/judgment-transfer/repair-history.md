# Prior Work Episode (inherited record)

This is a record of an earlier work episode by a different assistant session. You are given it as background. It is not your own memory.

**Task.** An assistant was preparing a small written evaluation. The plan said each answer would be judged on two separate things: what a supplied source required, and what the answerer recommended doing on their own judgment. Each answer was to be recorded in a structured form that a later checker would read.

**Original artifact.** The answer form had one field, `inquiry`. Answerers were told to write both their reading of the source and their recommendation in that field.

**Critique.** Before anything was used, a reviewer pointed out a problem. The plan said the two judgments would be told apart, but the record had only one place to put them. A later checker reading `inquiry` could not tell an answer that misread the source but recommended well from one that read the source correctly and then chose to depart from it. The reviewer also noticed that the form could not show whether an action was recommended *now* or *after a delay*, although the plan treated those as different.

**Correction.** The assistant split the field into `source_obligation`, `independent_recommendation` and `dissent`. It added a `disposition` field with fixed values: proceed now, defer, reduce scope, decline. Then it checked that the checker read the new fields, and froze the form before use.

**Note recorded at the time.** The fix was to make the record able to carry the distinction the task depended on. More explanatory text would not have done that. The assistant also noted where the fix should not be applied: when a judgment is nuanced and cannot usefully be reduced to a label, a short explanation with an explicit ambiguity flag can be more faithful than another field.
