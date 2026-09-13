"""Phase III optimization decision contract tests."""

import unittest
from dataclasses import replace
from pathlib import Path

from tests.optimizer_helpers import (
    CHILD_EXPRESSION,
    IMPACT,
    MECHANISM,
    PACKAGE_ROOT,
    PARENT_EXPRESSION,
    FakeCache,
    FakeTrajectory,
    RecordingFactory,
    child_decision,
    parent_record,
    validate_decision,
    workflow,
)
from wqb_agent.alpha_factory import (
    DEFAULT_TEMPLATES,
    ECONOMIC_TEMPLATES,
    AlphaFactory,
    template_numeric_audit,
)
from wqb_agent.diversity import semantic_mechanism_key
from wqb_agent.optimization_decision import (
    CHILD_REQUIRED_TEXT_FIELDS,
    OPPORTUNITY_CATEGORIES,
    VALID_DECISIONS,
    VALIDATION_VARIABLES,
    OptimizationDecision,
    decision_rejections,
    optimization_decision_identity,
    parent_opportunity,
    summarize_parent,
)
from wqb_agent.optimizer_workflow import (
    OptimizerHooks,
    OptimizerWorkflow,
    optimization_eligibility_map,
    optimizer_conversions,
)
from wqb_agent.pre_correlation import (
    READINESS_BANDS,
    optimization_parent_admission,
    pre_self_correlation_eligibility,
)
from wqb_agent.proposal_contract import validate_proposal
from wqb_agent.research_yield import build_research_yield


class TestOptimizationDecisionContract(unittest.TestCase):
    def test_semantic_identity_is_order_and_time_independent(self):
        first = child_decision("p1")
        second = OptimizationDecision.from_mapping({
            **first.as_dict(),
            "self_correlation_impact": dict(reversed(list(first.self_correlation_impact.items()))),
        })
        self.assertEqual(optimization_decision_identity(first),
                         optimization_decision_identity(second))

    def setUp(self):
        self.parent = parent_record("p1")

    def test_decision_vocabulary_and_required_child_fields_are_explicit(self):
        self.assertEqual(set(VALID_DECISIONS), {"CHILD", "VALIDATE", "REROUTE", "STOP"})
        self.assertIn("economic_mechanism", CHILD_REQUIRED_TEXT_FIELDS)
        self.assertIn("falsification", CHILD_REQUIRED_TEXT_FIELDS)
        self.assertIn("parent_id", CHILD_REQUIRED_TEXT_FIELDS)

    def test_unknown_decision_and_empty_parent_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            OptimizationDecision(parent_id="p1", decision="MAYBE")
        with self.assertRaises(ValueError):
            OptimizationDecision(parent_id="   ", decision="STOP")

    def test_legal_child_decision_passes_every_deterministic_gate(self):
        decision = child_decision("p1")
        self.assertTrue(decision.is_child)
        self.assertEqual(decision.missing_fields(), [])
        self.assertEqual(decision_rejections(decision, self.parent), [])

    def test_missing_parent_is_rejected(self):
        decision = child_decision("p1")
        self.assertEqual(decision_rejections(decision, None), ["PARENT_INVALID"])
        self.assertEqual(
            decision_rejections(decision, parent_record("p2")),
            ["PARENT_IDENTITY_MISMATCH"],
        )

    def test_missing_economic_mechanism_or_falsification_is_rejected(self):
        for field in ("economic_mechanism", "falsification", "why_not_parameter_tuning"):
            decision = child_decision("p1", **{field: "  "})
            self.assertEqual(
                decision_rejections(decision, self.parent),
                ["DECISION_FIELDS_MISSING"],
                field,
            )

    def test_parameter_only_change_is_rejected(self):
        tuned_parent = parent_record(
            "p1", expression="ts_decay_linear(rank(field_a), 5)"
        )
        decision = child_decision(
            "p1", expression="ts_decay_linear(rank(field_a), 10)"
        )
        self.assertIn(
            "PARAMETER_ONLY_CHANGE", decision_rejections(decision, tuned_parent)
        )

    def test_direction_only_change_is_rejected(self):
        decision = child_decision("p1", expression="-rank(field_a)")
        self.assertEqual(
            decision_rejections(decision, self.parent), ["DIRECTION_ONLY_CHANGE"]
        )

    def test_disguised_multi_change_is_rejected(self):
        decision = child_decision("p1", expression="rank(field_b)")
        self.assertIn(
            "DECLARED_CHANGE_MULTIPLE_FIELDS",
            decision_rejections(decision, self.parent),
        )
        tuned_parent = parent_record(
            "p1", expression="ts_decay_linear(rank(field_a), 5)"
        )
        disguised = child_decision(
            "p1", expression="ts_decay_linear(rank(field_b), 10)"
        )
        reasons = decision_rejections(disguised, tuned_parent)
        self.assertIn("DECLARED_CHANGE_FIELD_AND_PARAMETER", reasons)

    def test_change_type_and_direction_transform_must_match_the_proposal_contract(self):
        """决策层必须复用既有 proposal contract，而不是自创同义取值。"""
        wrong_vocabulary = child_decision("p1", change_type="neutralization_change")
        self.assertEqual(
            decision_rejections(wrong_vocabulary, self.parent),
            ["CHANGE_TYPE_NOT_IN_PROPOSAL_CONTRACT"],
        )
        bad_transform = child_decision("p1", direction_transform="same")
        self.assertEqual(
            decision_rejections(bad_transform, self.parent),
            ["DIRECTION_TRANSFORM_INVALID"],
        )

    def test_illegal_operator_and_invalid_self_correlation_are_rejected(self):
        decision = child_decision("p1")
        reasons = decision_rejections(
            decision, self.parent, allowed_operators={"rank"}
        )
        self.assertEqual(reasons, ["OPERATOR_ILLEGAL"])
        bad_impact = child_decision(
            "p1",
            self_correlation_impact={
                "expected_effect": "SIMILAR",
                "basis": "b",
                "rationale": "r",
                "admission": "ALLOW",
            },
        )
        self.assertIn(
            "SELF_CORRELATION_IMPACT_INVALID",
            decision_rejections(bad_impact, self.parent),
        )

    def test_non_child_decisions_carry_no_child_discovery_claim(self):
        for decision_name in ("VALIDATE", "REROUTE", "STOP"):
            decision = OptimizationDecision(
                parent_id="p1", decision=decision_name
            )
            self.assertFalse(decision.is_child)
            self.assertEqual(decision.missing_fields(), [])
            self.assertEqual(decision_rejections(decision, self.parent), [])
        validate = OptimizationDecision(
            parent_id="p1",
            decision="VALIDATE",
            expression=CHILD_EXPRESSION,
            economic_mechanism=MECHANISM,
        )
        # A VALIDATE decision never becomes a CHILD claim even if fields exist.
        self.assertFalse(validate.is_child)
        self.assertEqual(validate.to_child_hypothesis()["expression"], CHILD_EXPRESSION)

    def test_from_child_hypothesis_round_trips_the_legacy_dict(self):
        legacy = child_decision("p1").to_child_hypothesis()
        restored = OptimizationDecision.from_child_hypothesis("p1", legacy)
        self.assertTrue(restored.is_child)
        self.assertEqual(restored.to_child_hypothesis(), legacy)
        self.assertEqual(restored.parent_id, "p1")

    def test_from_mapping_requires_an_object(self):
        with self.assertRaises(TypeError):
            OptimizationDecision.from_mapping(["not", "a", "mapping"])

    def test_legacy_extra_metadata_is_ignored_without_changing_identity(self):
        decision = OptimizationDecision.from_mapping({
            "parent_id": "p1", "decision": "STOP",
            "external_evidence_refs": ["legacy"],
        })
        plain = OptimizationDecision(parent_id="p1", decision="STOP")
        self.assertFalse(hasattr(decision, "external_evidence_refs"))
        self.assertNotIn("external_evidence_refs", decision.as_dict())
        self.assertEqual(optimization_decision_identity(decision),
                         optimization_decision_identity(plain))

    def test_parent_opportunity_and_summary_stay_evidence_derived(self):
        self.assertIn(parent_opportunity(self.parent), OPPORTUNITY_CATEGORIES)
        self.assertEqual(
            parent_opportunity(parent_record("p", self_correlation={"status": "FAIL"})),
            "SELF_CORRELATION_REPAIR",
        )
        self.assertEqual(
            parent_opportunity(
                parent_record(
                    "p",
                    metrics={"sharpe": 1.0, "checks": [
                        {"name": "CONCENTRATED_WEIGHT", "pass": False}
                    ]},
                )
            ),
            "CONCENTRATION_REPAIR",
        )
        summary = summarize_parent(self.parent)
        self.assertEqual(summary["parent_id"], "p1")
        self.assertEqual(summary["opportunity"], "NO_CLEAR_OPPORTUNITY")
        self.assertEqual(summary["mechanism_state"], "UNKNOWN")
        self.assertEqual(
            set(summary["metrics"]),
            {"sharpe", "fitness", "turnover", "margin", "returns"},
        )
        # Missing evidence stays UNKNOWN instead of being guessed.
        self.assertEqual(summarize_parent({})["self_correlation_status"], "UNKNOWN")

class TestNumericVariantIdentityAndDedupe(unittest.TestCase):
    """§24-§32/§49/§55：参数变化不制造机制多样性，并复用现有去重与预算。"""

    @staticmethod
    def window_parent():
        return parent_record(
            "p-window", expression="-rank(ts_zscore(field_a, 5))",
            template_id="toy_control_zscore",
        )

    @staticmethod
    def window_request(value, *, expression):
        return {
            "parent": TestNumericVariantIdentityAndDedupe.window_parent(),
            "variable": "template_window",
            "old_value": 5,
            "new_value": value,
            "expected_effect": "检验短期反转窗口是否稳定",
            "falsification": "窗口变化后 Sharpe 反向恶化则稳定性假设不成立",
            "reason": "检验短期反转窗口稳定性",
            "expression": expression,
            "settings_override": {},
            "numeric_variant": {
                "source_template": "toy_control_zscore",
                "slot": "horizon",
                "parent_default_value": 5,
                "candidate_value": value,
                "change_count": 1,
                "reason": "检验短期反转窗口稳定性",
            },
        }

    def test_variant_keeps_the_parent_semantic_mechanism_family(self):
        template = AlphaFactory().registry.get("toy_confirmation")
        variants = template.numeric_variants(max_variants=3)
        self.assertTrue(variants)
        parent_proposal = {
            "semantic_mechanism_family": template.family,
            "template_family": template.family,
        }
        for variant in variants:
            self.assertEqual(
                variant["semantic_mechanism_family"], template.family
            )
            family = str(variant["semantic_mechanism_family"]).lower()
            for token in ("5", "20", "60", "window", "decay", "truncation",
                          "threshold"):
                self.assertNotIn(token, family)
            self.assertEqual(
                semantic_mechanism_key({
                    "semantic_mechanism_family": variant["semantic_mechanism_family"],
                    "template_family": template.family,
                    "template_variant_id": variant["template_variant_id"],
                }),
                semantic_mechanism_key(parent_proposal),
            )
        identities = {variant["template_variant_id"] for variant in variants}
        self.assertEqual(len(identities), len(variants))

    def test_validation_proposals_dedupe_identical_expressions(self):
        factory = AlphaFactory()
        template = factory.registry.get("toy_confirmation")
        request = self.window_request(
            22,
            expression=template.numeric_slot("horizon").render(
                "rank(add(ts_zscore(field_a, 5), ts_zscore(field_b, 5)))", 22
            ),
        )
        proposals = factory.validation_proposals(
            [dict(request), dict(request)],
            {"operators": ["rank", "ts_zscore", "add"], "sha256": "sha"},
            max_candidates=4,
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["expression"], "rank(add(ts_zscore(field_a, 22), ts_zscore(field_b, 5)))")

    def test_validation_proposals_are_role_capped_and_bounded(self):
        factory = AlphaFactory()
        template = factory.registry.get("toy_confirmation")
        requests = [
            self.window_request(
                value,
                expression=template.numeric_slot("horizon").render(
                    "rank(add(ts_zscore(field_a, 5), ts_zscore(field_b, 5)))", value
                ),
            )
            for value in (22, 66)
        ]
        proposals = factory.validation_proposals(
            requests,
            {"operators": ["rank", "ts_zscore", "add"], "sha256": "sha"},
            max_candidates=1,
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["research_role"], "VALIDATION")
        self.assertEqual(proposals[0]["experiment_stage"], "ROBUSTNESS")
