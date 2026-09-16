"""Declarative unary semantic admission contract regressions."""

import inspect
import tempfile
import unittest
from pathlib import Path

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_templates.loader import load_private_templates, load_templates
from wqb_agent.alpha_templates.model import AlphaTemplate
from wqb_agent.alpha_templates.registry import AlphaTemplateRegistry
from wqb_agent.alpha_templates.validation import evaluate_semantic_contract


def _template(*, template_id="fixture", family="arbitrary-family",
              semantic_contract="SYNTHETIC_FIXTURE"):
    return AlphaTemplate(
        template_id=template_id,
        family=family,
        expression="rank({p})",
        required_slots=("p",),
        role="CONTROL_ALPHA",
        economic_mechanism="synthetic fixture",
        field_relationship="single field",
        direction_reason="fixture direction",
        expected_horizon="short-term",
        falsification="fixture fails",
        semantic_contract=semantic_contract,
    )


def _known_profile(description="daily close price"):
    return {
        "id": "arbitrary-field",
        "description": description,
        "dataset": "arbitrary-dataset",
        "type": "MATRIX",
        "frequency": "daily",
        "category": "market",
        "semantic_status": "KNOWN",
    }


class TestSemanticContractAdmission(unittest.TestCase):
    def test_factory_dispatch_source_has_no_template_identity_selector(self):
        source = inspect.getsource(AlphaFactory._template_semantic_compatibility)
        self.assertNotIn("template.family", source)
        self.assertNotIn("template.template_id", source)
        self.assertNotIn("startswith(\"toy_\")", source)

    def test_catalog_entry_reports_declared_and_legacy_status(self):
        self.assertEqual(_template().catalog_entry()["semantic_contract"], "SYNTHETIC_FIXTURE")
        self.assertEqual(_template().catalog_entry()["semantic_contract_status"], "DECLARED")
        legacy = _template(semantic_contract="UNDECLARED").catalog_entry()
        self.assertEqual(legacy["semantic_contract_status"], "LEGACY_UNDECLARED")

    def test_generic_evaluator_distinguishes_declared_contracts(self):
        traits = {
            "concept": "data_quality",
            "measurement": "level",
            "behavior": "stable",
            "frequency": "daily",
            "sign_semantics": "unknown",
            "semantic_admission": "ALLOW",
        }
        quality = evaluate_semantic_contract(
            "DATA_QUALITY", traits, field_type="MATRIX",
            uses_vector_operator=False, field_slots=("p",),
        )
        event = evaluate_semantic_contract(
            "EVENT_DRIVEN", traits, field_type="MATRIX",
            uses_vector_operator=False, field_slots=("p",),
        )
        self.assertEqual(quality["admission"], "ALLOW")
        self.assertNotEqual(event["admission"], "ALLOW")
        self.assertNotEqual(quality["score"], event["score"])

    def test_family_and_id_renames_do_not_change_admission_or_score(self):
        profile = _known_profile()
        factory = AlphaFactory()
        original = factory._template_semantic_compatibility(_template(), profile)
        renamed = factory._template_semantic_compatibility(
            _template(template_id="another-arbitrary-id", family="another-family"),
            profile,
        )
        self.assertEqual(original, renamed)

    def test_undeclared_contract_is_review_only_without_name_inference(self):
        result = AlphaFactory()._template_semantic_compatibility(
            _template(
                template_id="toy_like_but_arbitrary",
                family="suggestive-but-arbitrary",
                semantic_contract="UNDECLARED",
            ),
            _known_profile("event announcement"),
        )
        self.assertNotEqual(result["admission"], "ALLOW")
        self.assertIn("SEMANTIC_CONTRACT_UNDECLARED", result["reasons"])

    def test_vector_decision_is_structural_and_family_independent(self):
        traits = {
            "concept": "unknown", "measurement": "unknown",
            "behavior": "unknown", "frequency": "daily",
            "sign_semantics": "unknown", "semantic_admission": "ALLOW",
        }
        vector = evaluate_semantic_contract(
            "VECTOR_AGGREGATION", traits, field_type="VECTOR",
            uses_vector_operator=True, field_slots=("p",),
        )
        matrix = evaluate_semantic_contract(
            "VECTOR_AGGREGATION", traits, field_type="MATRIX",
            uses_vector_operator=True, field_slots=("p",),
        )
        self.assertEqual(vector["admission"], "ALLOW")
        self.assertEqual(matrix["admission"], "REJECT")

    def test_private_catalog_cannot_use_synthetic_contract_bypass(self):
        document = """
        [[templates]]
        id = "arbitrary-private-fixture"
        version = "1"
        role = "CONTROL_ALPHA"
        kind = "baseline"
        family = "arbitrary-family"
        semantic_contract = "SYNTHETIC_FIXTURE"
        expression = "rank({p})"
        required_slots = ["p"]
        economic_mechanism = "private fixture"
        field_relationship = "single field"
        direction = "long"
        direction_reason = "private fixture"
        direction_transform = "identity"
        expected_horizon = "short-term"
        falsification = "private fixture"
        self_correlation_impact = "UNKNOWN"
        allowed_horizon_profiles = []
        allowed_settings_arms = ["BASE"]
        selection_groups = ["default"]
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.toml"
            path.write_text(document, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SYNTHETIC_SEMANTIC_CONTRACT_PRIVATE"):
                load_private_templates(path)

if __name__ == "__main__":
    unittest.main()
