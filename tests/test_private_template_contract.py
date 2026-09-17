import tempfile
import unittest
from pathlib import Path
from subprocess import check_output

from wqb_agent.alpha_templates.loader import (
    PrivateTemplateCatalogError,
    load_private_templates,
    resolve_private_catalog_path,
)
from wqb_agent.alpha_templates.model import HORIZON_LATTICE
from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_templates.registry import AlphaTemplateRegistry

PRIVATE_TOML = """
[[templates]]
id = "private-probe"
version = "2"
role = "PROBE_ALPHA"
kind = "economic"
family = "private-test"
expression = "rank(add(ts_zscore({p}, 5), ts_zscore({s}, 22)))"
required_slots = ["p", "s"]
economic_mechanism = "private test confirmation"
field_roles = ["signal", "confirmation"]
fixed_field_bindings = []
allowed_field_families = ["TEST"]
field_relationship = "complementary test signals"
relationship_contract = "CO_MOVEMENT"
semantic_contract = "RELATIONAL_PRIMARY"
direction = "long"
direction_reason = "test direction"
direction_transform = "identity"
expected_horizon = "lattice profile"
falsification = "no out-of-sample confirmation"
self_correlation_impact = "UNKNOWN"
allowed_horizon_profiles = [[5, 22]]
allowed_settings_arms = ["BASE"]
mechanism_tags = ["TEST"]
novelty_family = "TEST"
selection_groups = ["candidate_scratch"]
[[templates.numeric_slots]]
name = "fast"
kind = "RESEARCH_HORIZON"
default = 5
allowed_values = [5, 22, 66, 120, 255]
economic_role = "fast"
token = "5"
occurrence = 0
[[templates.numeric_slots]]
name = "slow"
kind = "RESEARCH_HORIZON"
default = 22
allowed_values = [5, 22, 66, 120, 255]
economic_role = "slow"
token = "22"
occurrence = 0
"""


class TestPrivateTemplateContract(unittest.TestCase):
    def test_missing_private_catalog_fails_closed(self):
        with self.assertRaises(PrivateTemplateCatalogError) as ctx:
            load_private_templates(environ={}, home=tempfile.gettempdir())
        self.assertEqual(ctx.exception.code, "PRIVATE_TEMPLATE_CATALOG_MISSING")

    def test_relative_private_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            resolve_private_catalog_path("private.toml", environ={})

    def test_private_catalog_requires_v2_schema_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.toml"
            path.write_text(PRIVATE_TOML, encoding="utf-8")
            loaded = load_private_templates(path)
            self.assertEqual([item.template_id for item in loaded], ["private-probe"])
            self.assertEqual(AlphaTemplateRegistry(private_catalog=path).get("private-probe").operator_count, 4)

    def test_multi_field_private_contract_is_explicit_and_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.toml"
            path.write_text(PRIVATE_TOML, encoding="utf-8")
            template = AlphaTemplateRegistry(private_catalog=path).get("private-probe")
            self.assertEqual(template.relationship_contract, "CO_MOVEMENT")
            profiles = [
                {"id": "price", "description": "daily close price", "dataset": "d1",
                 "frequency": "daily", "category": "market", "semantic_status": "KNOWN"},
                {"id": "volume", "description": "daily trading volume", "dataset": "d2",
                 "frequency": "daily", "category": "market", "semantic_status": "KNOWN"},
            ]
            decision = AlphaFactory()._relationship_gate(profiles, template)
            self.assertEqual(
                AlphaFactory()._relationship_gate(profiles, template)["admission"],
                "ALLOW",
            )

    def test_public_catalog_is_synthetic_and_horizons_are_lattice(self):
        registry = AlphaTemplateRegistry()
        self.assertTrue(all("toy" in item["template_id"] for item in registry.catalog()))
        self.assertEqual(set(HORIZON_LATTICE), {5, 22, 66, 120, 255})
        self.assertTrue(all(
            item.role == "CONTROL_ALPHA" or 4 <= item.operator_count <= 6
            for item in (registry.get(row["template_id"]) for row in registry.catalog())
        ))

    def test_operator_coverage_is_reported_without_forcing_random_operators(self):
        coverage = AlphaTemplateRegistry().operator_coverage(["rank", "ts_zscore", "sqrt"])
        self.assertIn("rank", coverage["used"])
        self.assertIn("sqrt", coverage["uncovered"])
        self.assertIn("economic mechanism", coverage["policy"])

    def test_real_catalog_and_private_assets_are_not_tracked(self):
        tracked = check_output(["git", "ls-files"], text=True).splitlines()
        self.assertNotIn("wqb_agent/alpha_templates/catalog/private.toml", tracked)
        self.assertNotIn("alpha_templates.private.toml", tracked)
        self.assertTrue(all("round_" not in path for path in tracked if "alpha_templates" in path))


if __name__ == "__main__":
    unittest.main()
