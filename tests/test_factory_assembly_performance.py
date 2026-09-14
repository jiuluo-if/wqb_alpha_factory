"""Deterministic contracts for ephemeral Factory assembly memoization."""

import unittest
from unittest.mock import patch

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.alpha_templates.model import AlphaTemplate


def _profile(field_id, dataset):
    return {
        "id": field_id,
        "dataset": dataset,
        "description": "daily close price",
        "type": "MATRIX",
        "frequency": "daily",
        "category": "market",
        "coverage": 0.9,
        "semantic_status": "KNOWN",
    }


def _relation_template():
    return AlphaTemplate(
        "arbitrary-relation-template", family="arbitrary-family",
        expression="rank(add({p}, {s}))", required_slots=("p", "s"),
        relationship_contract="DIRECTIONAL_RATIO", role="PROBE_ALPHA",
        semantic_contract="SYNTHETIC_FIXTURE",
        economic=True, economic_mechanism="synthetic relation",
        field_relationship="synthetic relation", direction_reason="synthetic",
        expected_horizon="short-term", falsification="synthetic failure",
    )


class TestFactoryAssemblyPerformance(unittest.TestCase):
    def test_batch_facts_prepare_each_profile_once(self):
        profiles = [_profile("field-a", "dataset-a"), _profile("field-b", "dataset-b")]
        factory = AlphaFactory()
        module = __import__("wqb_agent.alpha_factory", fromlist=["_derive_field_semantic_traits"])
        with patch("wqb_agent.alpha_factory._derive_field_semantic_traits",
                   wraps=module._derive_field_semantic_traits) as derive:
            facts = factory._prepare_batch_facts(profiles)
        self.assertEqual(set(facts), {("dataset-a", "field-a"), ("dataset-b", "field-b")})
        self.assertEqual(derive.call_count, len(profiles))
        self.assertEqual(facts[("dataset-a", "field-a")]["traits"]["concept"], "market_price")

    def test_relationship_memo_keeps_directional_slot_order(self):
        left = _profile("left", "dataset-a")
        right = _profile("right", "dataset-b")
        factory = AlphaFactory()
        template = _relation_template()
        memo = {}
        with patch.object(factory, "_relationship_gate",
                          wraps=factory._relationship_gate) as gate:
            first = factory._relationship_gate_cached([left, right], template, memo)
            same = factory._relationship_gate_cached([left, right], template, memo)
            reverse = factory._relationship_gate_cached([right, left], template, memo)
        self.assertEqual(first, same)
        self.assertEqual(gate.call_count, 2)
        self.assertIsNot(first, reverse)


if __name__ == "__main__":
    unittest.main()
