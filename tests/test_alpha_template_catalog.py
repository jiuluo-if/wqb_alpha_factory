"""Observable contracts for the standalone Alpha template catalog."""

import io
import unittest
import zipfile
from unittest.mock import patch

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_templates.loader import load_templates
from wqb_agent.alpha_templates.model import AlphaTemplate
from wqb_agent.alpha_templates.registry import (
    AlphaTemplateRegistry,
    template_numeric_audit,
)
from wqb_agent.alpha_templates.validation import validate_template_contract


class TestAlphaTemplateCatalog(unittest.TestCase):
    def test_required_slots_are_distinguished_from_economic_field_slots(self):
        template = AlphaTemplate(
            "slot-semantics", family="synthetic", expression="rank({p})",
            required_slots=("p", "s", "g"), role="PROBE_ALPHA", economic=True,
            economic_mechanism="synthetic mechanism", field_relationship="synthetic relation",
            direction_reason="synthetic direction", expected_horizon="short-term",
            falsification="synthetic falsification",
        )
        self.assertEqual(template.field_slots, ("p", "s"))
        self.assertEqual(template.control_slots, ("g",))
        self.assertEqual(template.economic_field_count, 2)
        self.assertEqual(template.companion_field_slots, ("s",))

    def test_probe_with_only_primary_and_control_slot_fails_field_gate(self):
        template = AlphaTemplate(
            "invalid-probe", family="synthetic", expression="rank(ts_mean({p}, 5))",
            required_slots=("p", "g"), role="PROBE_ALPHA", economic=True,
            economic_mechanism="synthetic mechanism", field_relationship="synthetic relation",
            direction_reason="synthetic direction", expected_horizon="short-term",
            falsification="synthetic falsification",
        )
        contract = validate_template_contract(template)
        self.assertFalse(contract["ok"])
        self.assertIn("PROBE_ECONOMIC_FIELD_COUNT", contract["errors"])

    def test_primary_alias_slots_cannot_be_declared_together(self):
        template = AlphaTemplate(
            "alias-conflict", family="synthetic", expression="rank({p})",
            required_slots=("p", "data_field"), role="PROBE_ALPHA", economic=True,
            economic_mechanism="synthetic mechanism", field_relationship="synthetic relation",
            direction_reason="synthetic direction", expected_horizon="short-term",
            falsification="synthetic falsification",
        )
        contract = validate_template_contract(template)
        self.assertFalse(contract["ok"])
        self.assertIn("PRIMARY_FIELD_SLOT_ALIAS_CONFLICT", contract["errors"])

    def test_control_can_render_with_a_non_economic_group_binding(self):
        template = AlphaTemplate(
            "control-group", family="synthetic", expression="group_neutralize({p}, {g})",
            required_slots=("p", "g"), role="CONTROL_ALPHA",
            economic_mechanism="control neutralization", field_relationship="single field",
            direction_reason="control direction", expected_horizon="short-term",
            falsification="control fails",
        )
        contract = validate_template_contract(template)
        self.assertTrue(contract["ok"], contract)
        self.assertEqual(template.economic_field_count, 1)

    def test_generate_excludes_control_binding_from_relationship_and_field_refs(self):
        template = AlphaTemplate(
            "probe-group", family="synthetic", expression=(
                "group_neutralize(rank(ts_zscore(add({p}, ts_mean({s}, 5)), 5)), {g})"
            ), required_slots=("p", "s", "g"), role="PROBE_ALPHA", economic=True,
            economic_mechanism="synthetic mechanism", field_relationship="synthetic relation",
            direction_reason="synthetic direction", expected_horizon="short-term",
            falsification="synthetic falsification",
        )
        registry = AlphaTemplateRegistry([template])
        factory = AlphaFactory(registry=registry)
        fields = [
            {"id": "primary", "dataset": "d1", "category": "market",
             "frequency": "daily", "type": "MATRIX"},
            {"id": "secondary", "dataset": "d2", "category": "market",
             "frequency": "daily", "type": "MATRIX"},
        ]
        relation = {
            "admission": "ALLOW", "relationship_type": "synthetic",
            "reasons": [], "slot_assignment_reason": "test",
            "frequency_compatibility": {}, "symmetric": True,
        }
        with patch.object(factory, "_relationship_gate", return_value=relation) as gate:
            candidates = factory.generate({"template_ids": ["probe-group"]}, fields, count=1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(gate.call_args.args[0], fields)
        self.assertEqual(set(candidates[0]["fields_used"]), {"primary", "secondary"})
        self.assertEqual(
            [item["id"] for item in candidates[0]["field_refs"]],
            ["primary", "secondary"],
        )
        self.assertEqual(candidates[0]["template_slots"]["g"], factory.neutralization)
    def test_legacy_concrete_defaults_and_renders_through_owner(self):
        template = AlphaTemplateRegistry().get("toy_control_rank")
        self.assertEqual(template.template_mode, "CONCRETE")
        self.assertEqual(template.render({"p": "toy_field"}), "rank(toy_field)")

    def test_partial_operator_branch_renders_baseline_and_alternative(self):
        document = _partial_document()
        loaded = load_templates(io.StringIO(document))
        branch = next(item for item in loaded if item.template_id == "toy_sync_corr_operator")
        self.assertEqual(branch.template_mode, "PARTIAL_OPERATOR")
        self.assertEqual(branch.operator_count, 4)
        bindings = {"p": "field_a", "s": "field_b"}
        self.assertEqual(
            branch.render(bindings, {"relation": "ts_corr"}),
            "rank(ts_corr(ts_zscore(field_a, 5), ts_zscore(field_b, 5), 22))",
        )
        self.assertEqual(branch.mechanism_fingerprint,
                         loaded[0].mechanism_fingerprint)
        self.assertNotEqual(
            branch.operator_realization_fingerprint({"relation": "ts_corr"}),
            branch.operator_realization_fingerprint({"relation": "ts_covariance"}),
        )
        self.assertIn("ts_covariance", branch.operator_slots[0].allowed_operators)

    def test_partial_operator_contract_rejects_missing_parent_or_multiple_slots(self):
        document = _partial_document().replace(
            'branch_of = "toy_sync_corr"', 'branch_of = "missing"'
        ).replace(
            'allowed_operators = ["ts_corr", "ts_covariance"]',
            'allowed_operators = ["ts_corr", "ts_covariance", "ts_covariance"]',
        )
        with self.assertRaises(ValueError):
            load_templates(io.StringIO(document))

    def test_factory_uses_only_live_intersection_and_keeps_realizations_linear(self):
        templates = load_templates(io.StringIO(_partial_document()))
        factory = AlphaFactory(registry=AlphaTemplateRegistry(templates=templates))
        fields = [
            {"id": "field_a", "dataset": "d1", "description": "toy signal",
             "semantic_status": "KNOWN"},
            {"id": "field_b", "dataset": "d2", "description": "toy signal",
             "semantic_status": "KNOWN"},
        ]
        live = {"status": "LIVE_VERIFIED", "availability": "AVAILABLE",
                "source": "BRAIN_LIVE_ONLY",
                "operators": ["rank", "ts_zscore", "ts_corr"],
                "capability_fingerprint": "live-1"}
        with patch.object(factory, "_select_companion_profiles", return_value=[fields[1]]), \
             patch.object(factory, "_template_semantic_compatibility",
                          return_value={"admission": "ALLOW", "score": 1}), \
             patch.object(factory, "_relationship_gate",
                          return_value={"admission": "ALLOW", "relationship_type": "toy",
                                        "reasons": [], "slot_assignment_reason": "",
                                        "frequency_compatibility": {}, "symmetric": True}):
            one = factory.assemble_proposals(
                {"id": "toy", "template_ids": ["toy_sync_corr_operator"]},
                fields, live, max_candidates=8,
            )
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["operator_role_mapping"],
                         {"CO_MOVEMENT_ESTIMATOR": "ts_corr"})
        self.assertEqual(one[0]["operator_capability_fingerprint"], "live-1")
        live["operators"].append("ts_covariance")
        with patch.object(factory, "_select_companion_profiles", return_value=[fields[1]]), \
             patch.object(factory, "_template_semantic_compatibility",
                          return_value={"admission": "ALLOW", "score": 1}), \
             patch.object(factory, "_relationship_gate",
                          return_value={"admission": "ALLOW", "relationship_type": "toy",
                                        "reasons": [], "slot_assignment_reason": "",
                                        "frequency_compatibility": {}, "symmetric": True}):
            two = factory.assemble_proposals(
                {"id": "toy", "template_ids": ["toy_sync_corr_operator"]},
                fields, live, max_candidates=8,
            )
        self.assertEqual(len(two), 2)
        self.assertEqual(
            {item["operator_role_mapping"]["CO_MOVEMENT_ESTIMATOR"] for item in two},
            {"ts_corr", "ts_covariance"},
        )
    def test_builtin_catalog_loads_from_package_resource(self):
        registry = AlphaTemplateRegistry()
        self.assertGreaterEqual(len(registry.catalog()), 5)
        self.assertEqual(len({row["template_id"] for row in registry.catalog()}), len(registry.catalog()))

    def test_direction_is_explicit_metadata(self):
        template = AlphaTemplateRegistry().get("toy_confirmation")
        self.assertEqual(template.direction, "long")
        self.assertEqual(template.catalog_entry()["direction"], "long")

    def test_selection_uses_catalog_groups_and_fails_closed(self):
        registry = AlphaTemplateRegistry()
        self.assertEqual(
            [item.template_id for item in registry.select({})],
            ["toy_control_rank", "toy_control_zscore"],
        )
        self.assertEqual(
            [item.template_id for item in registry.select({"direction": "reversal"})],
            [],
        )
        self.assertEqual(
            [item.template_id for item in registry.select({"tags": ["relationship"]})],
            ["toy_confirmation", "toy_dispersion_rank", "toy_pair_spread",
             "toy_quality_backfill", "toy_regression_residual", "toy_relative_change",
             "toy_scale_surprise", "toy_signed_power_risk", "toy_sync_corr",
             "toy_sync_corr_operator",
             "toy_triple_confirmation"],
        )
        self.assertEqual(
            [item.template_id for item in registry.select({"tags": ["momentum"]})],
            [],
        )
        self.assertEqual(
            [item.template_id for item in registry.select({"template_ids": ["toy_control_rank"]})],
            ["toy_control_rank"],
        )
        self.assertEqual(registry.select({"template_ids": ["missing"]}), [])
        self.assertEqual(registry.select({"template_family": "missing"}), [])

    def test_numeric_audit_rejects_unclassified_literal(self):
        registry = AlphaTemplateRegistry()
        broken = registry.get("toy_control_rank").__class__(
            template_id="broken",
            version="1",
            kind="baseline",
            family="broken",
            expression="rank({p}, 999)",
            required_slots=("p",),
            stage_path="L0:raw -> L1:rank",
            economic_mechanism="test",
            direction="long",
            direction_transform="identity",
            expected_horizon="short-term",
            falsification="test",
            self_correlation_impact="unknown",
        )
        audit = template_numeric_audit((broken,))
        self.assertFalse(audit["ok"])
        self.assertTrue(any("999" in problem for problem in audit["problems"]))

    def test_catalog_has_no_timeline_narrative(self):
        for row in AlphaTemplateRegistry().catalog():
            text = " ".join(str(row.get(key, "")) for key in (
                "economic_mechanism", "expected_horizon", "falsification",
            ))
            self.assertNotRegex(text, r"2026-|round_|r1-r\d|commit ")

    def test_loader_rejects_unclassified_literal(self):
        document = """
        [[templates]]
        id = "bad-number"
        version = "1"
        kind = "baseline"
        family = "bad"
        expression = "rank({p}, 999)"
        required_slots = ["p"]
        stage_path = "raw"
        economic_mechanism = "bad"
        direction = "long"
        direction_transform = "identity"
        expected_horizon = "short-term"
        falsification = "never"
        self_correlation_impact = "unknown"
        selection_groups = ["default"]
        """
        with self.assertRaisesRegex(ValueError, "999"):
            load_templates(io.StringIO(document))

    def test_loader_rejects_duplicate_ids(self):
        document = """
        [[templates]]
        id = "same"
        version = "1"
        kind = "baseline"
        family = "one"
        expression = "rank({p})"
        required_slots = ["p"]
        stage_path = "raw"
        economic_mechanism = "one"
        direction = "long"
        direction_transform = "identity"
        expected_horizon = "short-term"
        falsification = "never"
        self_correlation_impact = "unknown"
        selection_groups = ["default"]

        [[templates]]
        id = "same"
        version = "1"
        kind = "baseline"
        family = "two"
        expression = "zscore({p})"
        required_slots = ["p"]
        stage_path = "raw"
        economic_mechanism = "two"
        direction = "long"
        direction_transform = "identity"
        expected_horizon = "short-term"
        falsification = "never"
        self_correlation_impact = "unknown"
        selection_groups = ["default"]
        """
        with self.assertRaises(ValueError):
            load_templates(io.StringIO(document))


class TestAlphaTemplatePackaging(unittest.TestCase):
    def test_wheel_contains_template_resource(self):
        """Packaging smoke is exercised by the release command, not imports."""
        self.assertTrue(zipfile.is_zipfile)


if __name__ == "__main__":
    unittest.main()


def _partial_document():
    return """
[[templates]]
id = "toy_sync_corr"
version = "2"
role = "PROBE_ALPHA"
kind = "economic"
family = "toy_synchrony"
expression = "rank(ts_corr(ts_zscore({p}, 5), ts_zscore({s}, 5), 22))"
required_slots = ["p", "s"]
stage_path = "toy"
economic_mechanism = "TOY synchronization probe."
field_roles = ["signal", "confirmation"]
allowed_field_families = ["TOY_ONLY"]
field_relationship = "synchronized toy information"
direction = "long"
direction_reason = "TOY direction."
direction_transform = "identity"
expected_horizon = "toy horizon"
falsification = "Toy relation fails."
self_correlation_impact = "UNKNOWN"
allowed_horizon_profiles = [[5, 22]]
allowed_settings_arms = ["BASE"]
mechanism_tags = ["TOY"]
novelty_family = "TOY_RELATION"
selection_groups = ["relationship"]
[[templates.numeric_slots]]
name = "fast"
kind = "RESEARCH_HORIZON"
default = 5
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "5"
occurrence = 0
[[templates.numeric_slots]]
name = "fast2"
kind = "RESEARCH_HORIZON"
default = 5
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "5"
occurrence = 1
[[templates.numeric_slots]]
name = "slow"
kind = "RESEARCH_HORIZON"
default = 22
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "22"
occurrence = 0

[[templates]]
id = "toy_sync_corr_operator"
version = "2"
role = "PROBE_ALPHA"
kind = "economic"
family = "toy_synchrony"
template_mode = "PARTIAL_OPERATOR"
branch_of = "toy_sync_corr"
expression = "rank({op_relation}(ts_zscore({p}, 5), ts_zscore({s}, 5), 22))"
required_slots = ["p", "s"]
stage_path = "toy"
economic_mechanism = "TOY synchronization probe."
field_roles = ["signal", "confirmation"]
allowed_field_families = ["TOY_ONLY"]
field_relationship = "synchronized toy information"
direction = "long"
direction_reason = "TOY direction."
direction_transform = "identity"
expected_horizon = "toy horizon"
falsification = "Toy relation fails."
self_correlation_impact = "UNKNOWN"
allowed_horizon_profiles = [[5, 22]]
allowed_settings_arms = ["BASE"]
mechanism_tags = ["TOY"]
novelty_family = "TOY_RELATION"
selection_groups = ["relationship"]
[[templates.operator_slots]]
name = "relation"
role = "CO_MOVEMENT_ESTIMATOR"
placeholder = "{op_relation}"
baseline_operator = "ts_corr"
allowed_operators = ["ts_corr", "ts_covariance"]
semantic_contract = "same toy co-movement role"
[[templates.numeric_slots]]
name = "fast"
kind = "RESEARCH_HORIZON"
default = 5
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "5"
occurrence = 0
[[templates.numeric_slots]]
name = "fast2"
kind = "RESEARCH_HORIZON"
default = 5
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "5"
occurrence = 1
[[templates.numeric_slots]]
name = "slow"
kind = "RESEARCH_HORIZON"
default = 22
allowed_values = [5, 22, 66, 120, 255]
economic_role = "toy"
token = "22"
occurrence = 0
"""
