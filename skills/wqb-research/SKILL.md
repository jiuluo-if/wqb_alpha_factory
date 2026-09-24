---
name: wqb-research
description: "Use for WorldQuant BRAIN Alpha research, experiment batches, Simulation evidence, result interpretation and next-experiment choices."
compatible_research_contract: "2026-09-24"
---

# WQB Research

## Owners

- BRAIN is the primary fact source for capabilities, fields, Simulations and Alpha evidence.
- The Agent owns hypotheses, mechanisms, field choice, experiment groups and interpretation. Python validates deterministic contracts and executes safely; it does not choose economic research.
- Alpha submission is always manual. All Simulation writes use `research_api → SimulationGateway → Simulator → WQBClient`.
- A Simulation result is not evidence that its proposed mechanism is supported. Missing evidence stays `UNKNOWN`/`UNAVAILABLE`.

## Contract handshake

`compatible_research_contract` above is the contract this file was written for. Call `research_status()` first and compare its `research_contract_version`. On a mismatch this Skill is `SKILL_STALE`: re-read the repository Skill and `AGENTS.md` before running any batch. Never reuse a stale Skill on trust.

## Every research batch

- Start from multiple competing hypotheses. Use BRAIN raw dataset/datafield lists and explain each Agent-chosen field.
- Give every proposal a `proposal_id`, a useful `note`, and a template/family label when applicable. Group the batch into controls, local siblings, falsifications and novel probes.
- Use `note` as the hypothesis link each result echoes back, for example `H2:EXPLORE`, `H3:CONTROL`, `H2:FALSIFY`, `H2:LOCAL`. Python returns this label unchanged and never interprets it.
- State which mechanism each group tests, what result would change the next decision, and how many Simulations it uses. Many purposeful Simulations are welcome; untraceable random expressions are not.
- Preserve exploration. A high result is a candidate for comparison, not permission to exploit that direction broadly.
- A local variant preserves field, operator and expression topology. A topology change is a `NEW_PROBE` with a distinct hypothesis.
- Attribute failures to hypothesis, field, operator, horizon, implementation, correlation or robustness. Do not repeat a clearly failed mode without new evidence.
- Do not treat a Skill, memory lesson or prior run as a BRAIN fact. Keep memory to a few transferable research lessons; never store full transcripts.

## Research working set

Keep one short working set for the current question and overwrite it every round:

```text
question
active hypotheses
strong evidence
rejected explanations
open uncertainty
promising families
next experiment
```

It is working memory, not a record: it never replaces BRAIN evidence, and only experience that stayed true across tasks belongs in a reference.

## Authority

- `RESEARCH_MODE` (this Skill): run live research and Simulations; never edit core code, `AGENTS.md` or this Skill.
- `MAINTENANCE_MODE`: edit code, tests and Skills; never execute a live Simulation POST.
- A Skill change never takes effect from a single Simulation: execution evidence → proposed Skill diff → independent tests/review → accept or reject → then research continues under the accepted version.

Read [Batch design](references/batch-design.md) when planning or labeling a large batch. Read [Result interpretation](references/result-interpretation.md) when comparing winners, checking robustness, or deciding whether to continue.
