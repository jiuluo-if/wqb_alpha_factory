import tempfile
import unittest
from pathlib import Path

from tests.test_targeted_ci import critical_contract
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_relationships import (
    frequency_compatibility,
    relationship_labels,
    relationship_type,
)
from wqb_agent.alpha_semantics import derive_field_semantic_traits
from wqb_agent.alpha_templates.loader import load_private_templates, load_templates
from wqb_agent.alpha_templates.model import AlphaTemplate
from wqb_agent.alpha_templates.registry import AlphaTemplateRegistry
from wqb_agent.alpha_templates.validation import evaluate_semantic_contract


class AlphaSemanticsTests(unittest.TestCase):
    def test_direct_description_allows_conservative_semantic_admission(self):
        result = derive_field_semantic_traits({
            "id": "eps_revision",
            "description": "Analyst earnings estimate revision change",
            "frequency": "daily",
        })
        self.assertEqual(result["concept"], "analyst_revision")
        self.assertEqual(result["semantic_admission"], "ALLOW")
        self.assertEqual(result["sign_semantics"], "signed_change")

    def test_missing_profile_is_unknown(self):
        result = derive_field_semantic_traits(None)
        self.assertEqual(result["semantic_admission"], "UNKNOWN")
        self.assertEqual(result["concept"], "unknown")

    def test_declared_text_supplies_frequency_when_catalog_omits_it(self):
        daily = derive_field_semantic_traits(
            {"id": "returns", "description": "Daily returns"}
        )
        annual = derive_field_semantic_traits({
            "id": "annual_total_assets_value",
            "description": "Total assets for the most recent fiscal year",
        })
        silent = derive_field_semantic_traits(
            {"id": "momentum", "description": "Composite momentum score"}
        )
        peer = {"frequency": "daily"}
        self.assertEqual(
            frequency_compatibility([daily, peer], "CO_MOVEMENT")["status"],
            "COMPATIBLE",
        )
        self.assertEqual(
            frequency_compatibility(
                [annual, {"frequency": "annual"}], "CO_MOVEMENT"
            )["status"],
            "COMPATIBLE",
        )
        # A field whose declared text states no cadence stays unknown and keeps
        # the relationship at REVIEW instead of being silently assumed.
        self.assertEqual(
            frequency_compatibility([silent, peer], "CO_MOVEMENT")["status"],
            "REVIEW",
        )


class AlphaRelationshipsTests(unittest.TestCase):
    @critical_contract("semantics.relationship_labels_price_liquidity")
    def test_relationship_labels_capture_explicit_price_liquidity_relation(self):
        labels = relationship_labels(
            {"concept": "market_price", "tags": ["price"]},
            {"concept": "liquidity", "tags": ["volume"]},
        )
        self.assertEqual(labels, {"price_volume"})
        self.assertEqual(relationship_type(labels), "price_volume")

    @critical_contract("semantics.frequency_gap_incompatible")
    def test_frequency_compatibility_rejects_wide_co_movement_gap(self):
        result = frequency_compatibility(
            [{"frequency": "daily"}, {"frequency": "annual"}],
            "CO_MOVEMENT",
        )
        self.assertEqual(result["status"], "INCOMPATIBLE")


def _template(*, template_id="fixture", family="arbitrary-family",
              semantic_contract="SYNTHETIC_FIXTURE", expression="rank({p})"):
    return AlphaTemplate(
        template_id=template_id,
        family=family,
        expression=expression,
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
    @critical_contract("semantic.declared_vs_legacy_status")
    def test_catalog_entry_reports_declared_and_legacy_status(self):
        self.assertEqual(_template().catalog_entry()["semantic_contract"], "SYNTHETIC_FIXTURE")
        self.assertEqual(_template().catalog_entry()["semantic_contract_status"], "DECLARED")
        legacy = _template(semantic_contract="UNDECLARED").catalog_entry()
        self.assertEqual(legacy["semantic_contract_status"], "LEGACY_UNDECLARED")

    @critical_contract("semantic.evaluator_contracts_distinct")
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

    @critical_contract(
        "factory.no_template_identity_dispatch",
        "semantic.family_id_renames_same_admission_score",
    )
    def test_family_and_id_renames_do_not_change_admission_or_score(self):
        profile = _known_profile()
        factory = AlphaFactory()
        original = factory._template_semantic_compatibility(_template(), profile)
        renamed = factory._template_semantic_compatibility(
            _template(template_id="another-arbitrary-id", family="another-family"),
            profile,
        )
        self.assertEqual(original, renamed)

    @critical_contract("semantic.undeclared_contract_no_name_inference")
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

    @critical_contract("semantic.vector_decision_structural_family_independent")
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

    @critical_contract("semantic.factory_vector_operator_coverage")
    def test_factory_recognizes_every_vector_aggregation_operator(self):
        profile = {
            "id": "arbitrary-vector-field",
            "description": "analyst estimate dispersion",
            "dataset": "arbitrary-dataset",
            "type": "VECTOR",
            "frequency": "daily",
        }
        traits = {
            "concept": "analyst_dispersion", "measurement": "dispersion",
            "behavior": "signed", "frequency": "daily",
            "sign_semantics": "nonnegative_dispersion",
            "semantic_admission": "ALLOW",
        }
        factory = AlphaFactory()
        for expression in (
            "rank(ts_rank(subtract(vec_max({p}), vec_min({p})), 22))",
            "rank(vec_stddev({p}))",
            "rank(vec_range({p}))",
            "rank(vec_count({p}))",
            "rank(vec_avg({p}))",
            "rank(vec_sum({p}))",
        ):
            result = factory._template_semantic_compatibility(
                _template(semantic_contract="VECTOR_AGGREGATION", expression=expression),
                profile, traits,
            )
            self.assertEqual(result["admission"], "ALLOW", expression)

    @critical_contract("semantic.matrix_field_rejected_by_vector_contract")
    def test_matrix_field_is_still_rejected_by_vector_contract(self):
        profile = {
            "id": "arbitrary-matrix-field",
            "description": "analyst estimate dispersion",
            "dataset": "arbitrary-dataset",
            "type": "MATRIX",
            "frequency": "daily",
        }
        traits = {
            "concept": "analyst_dispersion", "measurement": "dispersion",
            "behavior": "signed", "frequency": "daily",
            "sign_semantics": "nonnegative_dispersion",
            "semantic_admission": "ALLOW",
        }
        result = AlphaFactory()._template_semantic_compatibility(
            _template(
                semantic_contract="VECTOR_AGGREGATION",
                expression="rank(ts_rank(subtract(vec_max({p}), vec_min({p})), 22))",
            ),
            profile, traits,
        )
        self.assertEqual(result["admission"], "REJECT")

    @critical_contract("semantic.private_catalog_synthetic_bypass_rejected")
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
