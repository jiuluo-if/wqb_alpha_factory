# Batch Design

Default reasoning flow:

```text
Observation
→ several competing hypotheses
→ grouped batch experiments
→ BRAIN results
→ compare hypothesis families
→ expand promising directions
→ falsification / robustness
→ stop or continue
```

For each batch, record:

1. What mechanism is being tested?
2. Which proposals are controls, local siblings, falsifications or novel probes?
3. What result would change the next decision?

Use `proposal_id` for a unique transient result link, `note` for the hypothesis/group explanation, and `template_id` for template provenance. The Gateway returns these labels with each child result; they are not stored in `ExecutionGuard` or a local research database.

Read fields from `list_datasets()` and `list_datafields()`/`list_all_datafields()`. Select fields yourself and pass each field ID with its dataset provenance into `SimulationSpec`. The Gateway's live check is a safety check, not an economic suitability score.

Build only the candidates needed to distinguish explanations. A large batch is useful when its groups have different interpretations; do not generate a Cartesian product or a Python-planned search cycle.
