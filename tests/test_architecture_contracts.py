import ast
import inspect
import pathlib
import unittest

from wqb_agent import research_api
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_templates.model import (
    AlphaTemplate,
    TemplateNumericSlot,
    TemplateOperatorSlot,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "wqb_agent"


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(f"{node.level}:{node.module}")
    return result


class RemoteFirstArchitectureTests(unittest.TestCase):
    def test_single_research_skill_declares_the_runtime_contract(self):
        skill_root = ROOT / "skills"
        skill_dirs = sorted(path.parent.name for path in skill_root.glob("*/SKILL.md"))
        self.assertEqual(skill_dirs, ["skill-authoring", "wqb-research"])

        self.assertEqual(research_api.RESEARCH_CONTRACT_VERSION, "2026-09-26")
        self.assertEqual(len(research_api.research_tool_manifest(profile="core")), 10)
        skill_path = skill_root / "wqb-research" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        self.assertIn(
            f'compatible_research_contract: "{research_api.RESEARCH_CONTRACT_VERSION}"',
            text,
        )
        references = sorted(
            path.name
            for path in (skill_root / "wqb-research" / "references").glob("*.md")
        )
        self.assertLessEqual(len(references), 2)

    def test_skill_authoring_contract_is_chinese_and_self_contained(self):
        skill_path = ROOT / "skills" / "skill-authoring" / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        self.assertLess(len(text.splitlines()), 500)
        for required in (
            "输入：", "输出：", "前置条件：", "成功标准：",
            "SPLIT_REQUIRED", "权限从小", "渐进式披露", "失败语义", "触发词前置",
            "有效触发", "非触发", "相近但不适用", "成功执行", "预期失败",
            "规则编号报告", "MUST 通过项退化",
        ):
            self.assertIn(required, text)
        self.assertIn("name: skill-authoring", text)
        self.assertNotIn("# Agent Skill Authoring Contract", text)
        self.assertIn("## 参考依据", text)

    def test_mcp_reference_documents_prod_correlation_as_a_finalist_tool(self):
        text = (ROOT / "docs" / "MCP_READ_ONLY.md").read_text(encoding="utf-8")
        self.assertIn("Research Mode 暴露 10 个工具", text)
        self.assertIn("get_alpha_prod_correlation", text)
        self.assertIn("FINALIST_ONLY", text)
        self.assertIn("不隐式", text)

    def test_result_guide_uses_agent_owned_trial_context(self):
        text = (
            ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("variant_family", text)
        self.assertNotIn("observed_execution_count", text)
        for name in ("family_label", "related_trial_count_lower_bound", "count_scope"):
            self.assertIn(name, text)
        self.assertIn("Agent-owned", text)

    def test_skill_workset_has_only_the_requested_trial_context_line(self):
        text = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("试验上下文", text)
        for name in ("family_label", "related_trial_count_lower_bound", "count_scope"):
            self.assertIn(name, text)

    def test_hashes_are_not_task_completion_evidence(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for phrase in (
            "用户目标 > 可观察程序行为",
            "不能证明任务完成",
            "安全关键 machine identity",
            "Git SHA 可确认 runtime evidence 对应版本",
            "same hash != 功能正确",
            "main SHA matched != runtime research 正常",
        ):
            self.assertIn(phrase, agents)

    def test_tool_optimization_reference_prioritizes_direct_evidence(self):
        prompt = (ROOT / "prompts" / "maintenance_agent.md").read_text(encoding="utf-8")
        for name in (
            "TASK_TARGET:", "DIRECT_VERIFICATION:", "CONSTRAINTS:",
            "CURRENT_BLOCKER:", "NEXT_USEFUL_ACTION:",
            "RUNTIME_EVIDENCE", "CHANGE", "DIRECT_VERIFICATION:",
            "RESEARCH_IMPACT", "REMAINING_BLOCKER", "mtime 只决定是否值得重读",
            "运行每条新命令前先问", "读取实际 artifact/runtime evidence",
            "SHA 只标示来源", "不能单独构成 blocker",
        ):
            self.assertIn(name, prompt)

    def test_research_agent_owns_evidence_triggered_tool_optimization(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        local_agents = (ROOT / "wqb_agent" / "AGENTS.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        prompt_index = (ROOT / "prompts" / "AGENTS.md").read_text(encoding="utf-8")
        tool_phase = (ROOT / "prompts" / "maintenance_agent.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        combined = "\n".join((agents, local_agents, prompt, prompt_index, tool_phase, skill))

        self.assertIn("TOOL_OPTIMIZATION", combined)
        self.assertIn("RESEARCH → TOOL_OPTIMIZATION → RESEARCH", combined)
        self.assertIn("同一个 Research Agent", combined)
        self.assertIn("真实运行证据", combined)
        self.assertIn("确定性工程摩擦", combined)
        self.assertIn("活跃 research wave", combined)
        self.assertIn("工具优化期间禁止 live Simulation POST", combined)
        self.assertIn("离线测试", combined)
        self.assertIn("fresh `research_status`", combined)
        self.assertNotIn("不修改仓库代码", prompt)
        self.assertNotIn("永不改核心代码", skill)
        self.assertNotIn("Maintenance Agent（architecture", prompt_index)

    def test_handoff_records_only_bounded_tool_friction_and_blocks_unknown_readiness(self):
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        tool_phase = (ROOT / "prompts" / "maintenance_agent.md").read_text(encoding="utf-8")
        combined = prompt + "\n" + tool_phase

        for field in (
            "invalid_spec_failures", "deterministic_failure_codes",
            "batch_execution_failures", "evidence_retrieval_failures",
            "evidence_retrieval_pending_count", "repeated_manual_workaround_count",
        ):
            self.assertIn(field, combined)
        self.assertIn("pending_execution_count > 0", combined)
        self.assertIn("readiness 必须是 `BLOCKED_BY_REMOTE_STATE`", combined)
        self.assertIn("只记录 reason code", combined)
        self.assertIn("不记录自由文本", combined)
        self.assertIn("不得写入 expression、field ID、Alpha ID", combined)
        self.assertIn("不新增 database、telemetry、watcher 或 scheduler", combined)

    def test_research_guides_preserve_validation_and_mechanism_boundaries(self):
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        result = (ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        for phrase in (
            "Validation Ladder", "evidence_status=INCONCLUSIVE",
            "METRIC SYMPTOM != MECHANISM DIAGNOSIS",
            "EXTERNAL_HYPOTHESIS_SOURCE", "FULL_LOCAL_EXPRESSION_COMPILER = DEFER",
            "dataset metadata → semantic themes → bounded field pages",
            "IDEA", "IMPLEMENTATION", "OPTIMIZATION / ROBUSTNESS",
            "IDEA → IMPLEMENTATION → OPTIMIZATION / ROBUSTNESS",
            "SUBMISSION_CUTOFFS_ARE_PLATFORM_FACTS", "TASK_SCOPED_TARGET",
            "TURNOVER_REDUCTION != TURNOVER_MINIMIZATION",
            "NEUTRALIZATION IS A RISK-HYPOTHESIS",
            "PLATFORM_RESEARCH_PRIOR", "EXPRESSION_NEUTRALIZATION",
            "CORRELATION FITTING != RESEARCH DIVERSIFICATION",
            "DECORRELATION_SIBLING", "CORE_MECHANISM", "AUX_PROCESSING",
            "TEMPLATE_EFFECT != FIELD_EFFECT", "FIELD_DOMINATED_EVIDENCE",
            "numerator/denominator role", "cadence compatibility", "RAW RATIO",
        ):
            self.assertIn(phrase, batch)
        for label in (
            "FIELD_SIGNAL_WEAK", "EXTRACTION_WEAK", "INCONCLUSIVE",
            "RETURN_WEAK", "INSTABILITY_HIGH", "MIXED",
        ):
            self.assertIn(label, result)
        for phrase in (
            "Robustness Ladder", "DO_NOT_FIT_THE_TEST", "rank transform",
            "sign/binary transform", "DEVELOPMENT_EVIDENCE",
            "median", "failure distribution", "portfolio optimizer",
        ):
            self.assertIn(phrase, result)
        self.assertIn("不得因 Sharpe 为负而事后反转方向", skill)
        self.assertNotIn("signal_light.py", batch + result)
        self.assertNotRegex(batch + result, r"(?:Sharpe|Fitness)\s*>\s*\d")

    def test_research_aligns_long_term_alpha_with_current_platform_opportunities(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        result = (ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        combined = "\n".join((agents, skill, result, prompt))

        for phrase in (
            "长期可持续 Alpha",
            "当前 BRAIN submission / checks / correlation / cost",
            "Genius / Theme / competition / consultant",
            "每个 research wave",
            "当前 account",
            "UNKNOWN",
            "活动机会不能替代机制证据",
        ):
            self.assertIn(phrase, combined)

    def test_every_paper_maps_to_brain_template_even_when_observables_are_missing(self):
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        combined = "\n".join((skill, batch, prompt))

        for phrase in (
            "UNIVERSAL PAPER MAPPING",
            "PAPER → claim / empirical finding → economic mechanism",
            "BRAIN 可检验的模板草案",
            "predicted variable",
            "observable variables",
            "information timing",
            "implementation frictions",
            "transaction-cost implications",
            "falsification conditions",
            "近 24 个月",
            "peer-reviewed papers",
            "working papers",
            "market microstructure",
            "alternative data",
            "multiple-testing/overfitting",
            "CORE_MECHANISM",
            "FIELD_ROLES",
            "TEMPORAL_EXTRACTION",
            "SETTINGS_HYPOTHESIS",
            "ROBUSTNESS_AXES",
            "UNMAPPED_OBSERVABLE",
            "PARTIAL_MAPPING",
            "方法论文",
            "不强造 Alpha 表达式",
        ):
            self.assertIn(phrase, combined)
        self.assertIn("研究/audit 输入", combined)
        self.assertIn("不要求本地 Research Agent 联网检索论文", combined)

    def test_reusable_template_promotion_requires_family_level_brain_evidence(self):
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        result = (ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md").read_text(encoding="utf-8")
        combined = "\n".join((skill, batch, result))

        for phrase in (
            "TEMPLATE EVIDENCE",
            "BRAIN family-level validation",
            "supporting / falsification siblings",
            "同一经济机制",
            "字段",
            "horizon",
            "structural",
            "settings",
            "cost",
            "correlation",
            "return 与 stability",
            "time decay",
            "selection pressure",
            "单一 Alpha",
            "不设固定字段数、批次数或通过率",
            "机制不变量",
            "已知失败条件",
            "验证证据范围",
        ):
            self.assertIn(phrase, combined)

    def test_paper_template_candidate_preserves_source_assumptions_and_controls(self):
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        combined = "\n".join((skill, batch, prompt))

        for phrase in (
            "PAPER_TEMPLATE_CANDIDATE",
            "SOURCE",
            "CLAIM",
            "ECONOMIC / BEHAVIORAL / MICROSTRUCTURE MECHANISM",
            "PREDICTED VARIABLE",
            "REQUIRED OBSERVABLES",
            "FIELD ROLES / RELATIONSHIP",
            "DIRECTION + REASON",
            "INFORMATION AVAILABILITY",
            "EXPECTED HORIZON / DECAY",
            "CONDITION / REGIME",
            "IMPLEMENTATION / COST FRICTION",
            "NEGATIVE CONTROL",
            "BRAIN MAPPING",
            "publication date",
            "PAPER → TEMPLATE ASSUMPTION PROVENANCE",
            "CORE_MECHANISM",
            "FIELD RELATIONSHIP",
            "TEMPORAL_EXTRACTION",
            "AUX_PROCESSING",
            "RISK / SETTINGS",
        ):
            self.assertIn(phrase, combined)
        for route in (
            "map to an existing template",
            "extend an existing semantic slot",
            "NEW_PROBE sibling",
            "new template skeleton",
        ):
            self.assertIn(route, combined)
        self.assertIn("existing templates cannot faithfully express", combined)
        self.assertIn("CAPABILITY_MISSING", combined)
        for axis_field in (
            "default", "allowed neighborhood", "economic role",
            "what changing it tests",
        ):
            self.assertIn(axis_field, combined)
        self.assertIn("Alpha/PA", combined)
        self.assertIn("合法 validation/write contract", combined)

    def test_paper_templates_use_existing_alpha_template_owner_contract(self):
        owner = (ROOT / "wqb_agent" / "alpha_templates" / "AGENTS.md").read_text(encoding="utf-8")
        model = (ROOT / "wqb_agent" / "alpha_templates" / "model.py").read_text(encoding="utf-8")
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        combined = "\n".join((owner, batch))

        template_fields = set(AlphaTemplate.__dataclass_fields__)
        for name in (
            "economic_mechanism", "semantic_contract", "relationship_contract",
            "field_roles", "direction", "direction_reason", "expected_horizon",
            "falsification", "novelty_family", "numeric_slots",
            "allowed_horizon_profiles", "allowed_settings_arms",
        ):
            self.assertIn(name, template_fields)
        self.assertTrue(hasattr(AlphaTemplate, "structural_fingerprint"))
        self.assertTrue(hasattr(AlphaTemplate, "mechanism_fingerprint"))
        self.assertTrue({"default", "allowed_values", "economic_role"}.issubset(
            TemplateNumericSlot.__dataclass_fields__
        ))
        self.assertTrue({"role", "baseline_operator", "allowed_operators"}.issubset(
            TemplateOperatorSlot.__dataclass_fields__
        ))
        self.assertIn("structural_fingerprint", model)
        self.assertIn("mechanism_fingerprint", model)
        self.assertIn("用于 template 结构身份/机制分组和去重", combined)
        self.assertIn("Research mapping", combined)
        self.assertIn("不是 `AlphaTemplate` schema", combined)
        self.assertIn("无等价项时保留为 `CAPABILITY_MISSING`", combined)
        self.assertIn("不得硬塞相邻枚举", combined)
        self.assertIn("私有 catalog", owner)
        self.assertIn("fail-closed", owner)

    def test_paper_template_research_preserves_evidence_and_validation_budget(self):
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        batch = (ROOT / "skills" / "wqb-research" / "references" / "batch-design.md").read_text(encoding="utf-8")
        result = (ROOT / "skills" / "wqb-research" / "references" / "result-interpretation.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        combined = "\n".join((skill, batch, result, prompt))

        for phrase in (
            "PAPER SUPPORT != BRAIN SUPPORT",
            "SIMPLE IMPLEMENTATION",
            "CONTROL + FALSIFICATION",
            "NEGATIVE CONTROL",
            "LARGE-SCALE FAMILY ADMISSION",
            "ATTRIBUTION",
            "LOCAL / STRUCTURAL STABILITY",
            "COST + CORRELATION + TIME ROBUSTNESS",
            "REUSABLE TEMPLATE",
            "DEVELOPMENT_EVIDENCE",
            "VALIDATION_EXHAUSTED",
            "不汇总成单一综合值",
        ):
            self.assertIn(phrase, combined)

    def test_natural_wave_efficiency_review_separates_research_unknown_from_tool_friction(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        prompt = (ROOT / "prompts" / "research_agent.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "wqb-research" / "SKILL.md").read_text(encoding="utf-8")
        combined = "\n".join((agents, prompt, skill))

        for phrase in (
            "研究信息增益",
            "family yield",
            "无效 Simulation 来源",
            "deterministic failures",
            "evidence retrieval friction",
            "重复人工 workaround",
            "RESEARCH_UNKNOWN",
            "TOOL_FRICTION",
            "INFORMATION GAIN / SIMULATION",
            "paper → template → evidence",
        ):
            self.assertIn(phrase, combined)

    def test_research_status_reports_the_contract_version(self):
        status = research_api.research_status()
        self.assertEqual(
            status["research_contract_version"],
            research_api.RESEARCH_CONTRACT_VERSION,
        )

    def test_public_api_has_no_retired_agent_parameter(self):
        public = [
            value for name, value in vars(research_api).items()
            if inspect.isfunction(value) and not name.startswith("_")
        ]
        for function in public:
            self.assertNotIn("agent", inspect.signature(function).parameters, function.__name__)

    def test_factory_exposes_only_spec_generation(self):
        factory = AlphaFactory()
        for name in ("assemble_proposals", "generate_factory_batch", "assess_feasibility"):
            self.assertFalse(hasattr(factory, name), name)

    def test_public_package_exports_remote_first_tools_only(self):
        tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        exports = next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(target, "id", None) == "__all__" for target in node.targets)
        )
        names = {item.value for item in exports.elts}
        self.assertNotIn("Agent", names)
        self.assertNotIn("run_experiment", names)
        self.assertIn("simulate", names)
        self.assertIn("get_alpha_evidence", names)
        self.assertIn("sync_alpha_colors", names)
        import wqb_agent
        for name in (
            "get_alpha_metrics", "get_alpha_aggregates", "get_alpha_pnl",
            "get_alpha_self_correlation", "get_alpha_recordsets",
        ):
            self.assertIn(name, names)
            self.assertTrue(hasattr(wqb_agent, name), name)

    def test_research_surface_and_manifest_are_exactly_aligned(self):
        expected = set(research_api.__all__) - {"SimulationSpec", "research_tool_manifest"}
        rows = research_api.research_tool_manifest(profile="full")
        names = [row["name"] for row in rows]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names) - {"alpha_submission"}, expected)
        for name in names:
            if name == "alpha_submission":
                continue
            self.assertTrue(callable(getattr(research_api, name)), name)

    def test_package_root_matches_research_surface_and_lazy_exports(self):
        import wqb_agent

        self.assertEqual(
            set(wqb_agent.__all__) - {"WQBClient"},
            set(research_api.__all__),
        )
        for name in wqb_agent.__all__:
            self.assertTrue(hasattr(wqb_agent, name), name)

    def test_manifest_modes_reflect_possible_io(self):
        rows = {
            row["name"]: row
            for row in research_api.research_tool_manifest(profile="full")
        }
        for name in (
            "generate_probes", "validate_simulation_settings",
            "build_simulation_spec",
        ):
            self.assertEqual(rows[name]["mode"], "READ_ONLY", name)
        for name in (
            "build_simulation_variant", "group_alphas", "preview_alpha_colors",
        ):
            self.assertEqual(rows[name]["mode"], "PURE", name)
        for name in ("refresh_remote_alphas", "purge_remote_cache"):
            self.assertEqual(rows[name]["mode"], "LOCAL_CACHE_WRITE", name)
            self.assertTrue(rows[name].get("local_write"), name)
        self.assertEqual(rows["remote_cache_status"]["mode"], "READ_ONLY")
        self.assertFalse(rows["remote_cache_status"].get("local_write", False))

    def test_manifest_preserves_write_boundaries_and_has_one_public_name_per_operation(self):
        rows = {
            row["name"]: row
            for row in research_api.research_tool_manifest(profile="full")
        }
        import wqb_agent

        self.assertIn("find_duplicate_alphas", rows)
        self.assertNotIn("find_alpha_duplicates", rows)
        for name in (
            "simulate", "simulate_single", "simulate_batch",
            "simulate_multi_batch",
        ):
            self.assertEqual(rows[name]["mode"], "SIMULATION_WRITE", name)
            self.assertTrue(rows[name].get("remote_write"), name)
        for name in (
            "simulate_single_batch", "find_alpha_duplicates",
        ):
            self.assertNotIn(name, rows)
            self.assertFalse(hasattr(research_api, name), name)
            self.assertFalse(hasattr(wqb_agent, name), name)

    def test_canonical_remote_read_surface_stays_available_at_public_facades(self):
        canonical = {
            "get_live_preflight", "get_simulation_modes", "get_alpha_evidence",
            "get_alpha_recordsets", "get_activity_diversity",
        }
        import wqb_agent

        for name in canonical:
            self.assertTrue(hasattr(research_api, name), name)
            self.assertTrue(hasattr(wqb_agent, name), name)
        manifest = {
            item["name"] for item in research_api.research_tool_manifest(profile="full")
        }
        self.assertTrue(canonical <= manifest)

    def test_legacy_runtime_and_optimizer_modules_are_absent(self):
        retired = (
            "agent.py", "runtime_components.py", "runtime_composition.py",
            "runtime_policy.py", "optimizer_workflow.py", "optimizer_selection.py",
            "heartbeat.py",
        )
        for name in retired:
            self.assertFalse((PACKAGE / name).exists(), name)

    def test_agent_field_discovery_is_raw_and_ranked_selector_is_retired(self):
        for name in ("discovery.py", "discovery_selection.py", "field_catalog.py"):
            self.assertFalse((PACKAGE / name).exists(), name)
        self.assertFalse(hasattr(research_api, "discover_fields"))
        for name in ("list_datasets", "list_datafields", "list_all_datafields"):
            self.assertTrue(callable(getattr(research_api, name)), name)

    def test_public_simulation_path_is_gateway_to_simulator_to_client(self):
        gateway_imports = _imports(PACKAGE / "simulation_gateway.py")
        simulator_imports = _imports(PACKAGE / "simulator.py")
        self.assertIn("1:simulator", gateway_imports)
        self.assertIn("1:client", simulator_imports)

        callers = []
        for path in PACKAGE.glob("*.py"):
            if path.name in {"simulation_gateway.py", "simulator.py", "client.py"}:
                continue
            if "submit_simulation(" in path.read_text(encoding="utf-8"):
                callers.append(path.name)
        self.assertEqual(callers, [])

    def test_guard_and_repository_do_not_import_legacy_state(self):
        for name in ("simulation_gateway.py", "remote_alpha_repository.py", "remote_quota.py"):
            imports = _imports(PACKAGE / name)
            self.assertNotIn("1:state", imports, name)
            self.assertNotIn("1:trial_ledger", imports, name)
            self.assertNotIn("1:memory", imports, name)
            self.assertNotIn("1:optimizer_workflow", imports, name)

    def test_retired_trajectory_recovery_is_not_a_public_entry(self):
        for path in (ROOT / "main.py", PACKAGE / "cli.py", PACKAGE / "research_api.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("settle-stale-trajectory", source, str(path))
            self.assertNotIn("settle_stale_trajectory", source, str(path))


if __name__ == "__main__":
    unittest.main()
