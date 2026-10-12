import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client

from wqb_agent import mcp_server


class ResearchInventoryProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_inventory_preserves_operator_and_horizon_slots(self):
        row = {
            "template_id": "synthetic_template",
            "version": "1",
            "kind": "economic",
            "family": "synthetic_family",
            "role": "PROBE_ALPHA",
            "economic": True,
            "economic_mechanism": "synthetic mechanism",
            "direction_reason": "synthetic direction",
            "required_slots": ["p", "s"],
            "economic_field_slots": ["p", "s"],
            "control_slots": [],
            "economic_field_count": 2,
            "field_roles": ["synthetic primary", "synthetic scale"],
            "allowed_field_families": ["SYNTHETIC"],
            "field_relationship": "synthetic pair",
            "relationship_contract": "DIRECTIONAL_RATIO",
            "semantic_contract": "RELATIONAL_PRIMARY",
            "direction": "long",
            "direction_transform": "identity",
            "expected_horizon": "one synthetic profile",
            "falsification": "synthetic negative control",
            "self_correlation_impact": "UNKNOWN",
            "numeric_slots": [{
                "name": "horizon",
                "kind": "RESEARCH_HORIZON",
                "default": 5,
                "allowed_values": [5, 22],
                "economic_role": "synthetic horizon",
                "token": "5",
                "occurrence": 0,
            }],
            "allowed_horizon_profiles": [[5, 22]],
            "allowed_settings_arms": ["BASE"],
            "mechanism_tags": ["synthetic"],
            "novelty_family": "synthetic_family",
            "template_mode": "PARTIAL_OPERATOR",
            "operator_slots": [{
                "name": "smoother",
                "role": "synthetic smoother",
                "placeholder": "operator_slot",
                "baseline_operator": "ts_mean",
                "allowed_operators": ["ts_mean", "ts_product"],
                "semantic_contract": "TIME_SERIES_STATE",
            }],
            "simulation_type": "REGULAR",
        }
        api = SimpleNamespace(list_templates=lambda **_: [row])

        with patch.dict(os.environ, {mcp_server.RESEARCH_WRITE_OPT_IN: "1"}):
            server = mcp_server.build_research_server(api=api, client=object())
        async with Client(server) as client:
            result = await client.call_tool("list_templates", {"offset": 0, "limit": 1})

        template = result.structured_content["data"]["templates"][0]
        self.assertEqual(template["template_mode"], "PARTIAL_OPERATOR")
        self.assertEqual(template["operator_slots"][0]["allowed_operators"], [
            "ts_mean", "ts_product",
        ])
        self.assertEqual(template["numeric_slots"][0]["allowed_values"], [5, 22])
        self.assertFalse(result.structured_content["truncated"])

    async def test_operator_reference_keeps_the_full_bounded_live_list(self):
        operators = [f"synthetic_operator_{index}" for index in range(85)]
        reference = {
            "key": "operators",
            "status": "LIVE_VERIFIED",
            "availability": "AVAILABLE",
            "source": "BRAIN_LIVE_ONLY",
            "valid": True,
            "errors": [],
            "operators": operators,
            "capability_fingerprint": "synthetic-fingerprint",
            "endpoint": "/operators",
        }
        api = SimpleNamespace(get_operator_reference=lambda **_: reference)

        with patch.dict(os.environ, {mcp_server.RESEARCH_WRITE_OPT_IN: "1"}):
            server = mcp_server.build_research_server(api=api, client=object())
        async with Client(server) as client:
            result = await client.call_tool("get_operator_reference", {})

        data = result.structured_content["data"]
        self.assertEqual(data["operators"], operators)
        self.assertFalse(result.structured_content["truncated"])


if __name__ == "__main__":
    unittest.main()
