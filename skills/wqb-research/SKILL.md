---
name: wqb-research
description: "Use for WorldQuant BRAIN Alpha research, experiment batches, Simulation evidence, result interpretation and next-experiment choices."
---

# WQB Research

## Owners

- BRAIN is the primary fact source for capabilities, fields, Simulations and Alpha evidence.
- The Agent owns hypotheses, mechanisms, field choice, experiment groups and interpretation. Python validates deterministic contracts and executes safely; it does not choose economic research.
- Alpha submission is always manual. All Simulation writes use `research_api → SimulationGateway → Simulator → WQBClient`.
- A Simulation result is not evidence that its proposed mechanism is supported. Missing evidence stays `UNKNOWN`/`UNAVAILABLE`.

## Every research batch

- Start from multiple competing hypotheses. Use BRAIN raw dataset/datafield lists and explain each Agent-chosen field.
- Give every proposal a `proposal_id`, a useful `note`, and a template/family label when applicable. Group the batch into controls, local siblings, falsifications and novel probes.
- State which mechanism each group tests, what result would change the next decision, and how many Simulations it uses. Many purposeful Simulations are welcome; untraceable random expressions are not.
- Preserve exploration. A high result is a candidate for comparison, not permission to exploit that direction broadly.
- A local variant preserves field, operator and expression topology. A topology change is a `NEW_PROBE` with a distinct hypothesis.
- Attribute failures to hypothesis, field, operator, horizon, implementation, correlation or robustness. Do not repeat a clearly failed mode without new evidence.
- Do not treat a Skill, memory lesson or prior run as a BRAIN fact. Keep memory to a few transferable research lessons; never store full transcripts.

Read [Batch design](references/batch-design.md) when planning or labeling a large batch. Read [Result interpretation](references/result-interpretation.md) when comparing winners, checking robustness, or deciding whether to continue.
