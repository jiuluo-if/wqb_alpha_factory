# Alpha template generation constraints

## Mandatory pre-read

这是模板目录的最近约束。任何 Agent 在本目录新增、修改、迁移、审查或扩展模板及其 schema/catalog/operator/horizon/settings 规则前，必须先阅读根目录 `AGENTS.md`、[`wqb_agent/AGENTS.md`](../AGENTS.md) 和本文件，并在工作记录中确认。未阅读不得变更；无法阅读时报告 `TEMPLATE_CONSTRAINTS_NOT_READ`。

tracked catalog 只能包含 TOY/SYNTHETIC/NON-RESEARCH 示例；真实模板、字段、表达式、经验和 evidence 只能存在本地私有目录。私有 catalog 仅按显式绝对路径、`WQB_ALPHA_TEMPLATE_CATALOG` 或用户 home 默认路径加载，缺失必须 fail-closed。

This directory owns the template schema, fail-closed loaders, registry, and numeric/operator audits. The tracked catalog is public synthetic material only; it must never contain production expressions, private field IDs, fixed private pairings, research evidence, or learned priors.

`family` and `template_id` are provenance/grouping metadata only and never
dispatch semantic or relationship admission. `semantic_contract` is the bounded
machine selector for unary/primary-field suitability; `relationship_contract` is
the separate bounded selector for multi-field relations. `field_relationship`
and mechanism text are human-readable only. Legacy templates may load with
`UNDECLARED`, but remain review-only until the user explicitly declares a
supported contract.

Private templates are loaded only from an explicit absolute constructor path, `WQB_ALPHA_TEMPLATE_CATALOG`, or `~/.wqb_alpha_factory/private/alpha_templates.toml`. Missing private input is `PRIVATE_TEMPLATE_CATALOG_MISSING`; never search cwd/parents or fall back to the public package catalog.

Every private template declares `role`, field roles/relationships, mechanism, direction and reason, expected horizon, falsification, self-correlation impact, novelty family, settings arms, and horizon profiles. `required_slots` contains every render binding: `p`/`data_field`, `s`, and `t` are economic field slots, while `g` is a control binding. `p` and `data_field` are aliases and cannot coexist. Probe templates require 4–6 operator occurrences and 2–4 conceptual economic fields; controls are the only 1–3 operator / single-field exception. Horizon values are restricted to `[5, 22, 66, 120, 255]`; multi-window profiles are ordered adjacent lattice periods and are not Cartesian products.

Operator coverage is a diversity objective, never a reason to add an operator without an explicit economic mechanism. Prefer broad coverage of verified operators across independent mechanisms, while preserving arity, semantic relation, novelty, and complexity gates. Numeric literals are classified fail-closed; only declared `RESEARCH_HORIZON` slots may rotate.

Templates default to `CONCRETE`. Only explicit `PARTIAL_OPERATOR` probe siblings may
declare exactly one bounded `TemplateOperatorSlot`; the sibling must retain its concrete
parent's mechanism, field relationship, direction, numeric profiles, settings arms and
family. Materialization uses only declared operators intersected with the current
`LIVE_VERIFIED` BRAIN capability. Static syntax and fixture data are never availability
truth, and recovery never re-renders an already materialized proposal.

`AlphaFactory` and `CandidateBuilder` consume this owner. Do not add skeletons, fixed field combinations, parameter grids, or historical success rationale elsewhere.
