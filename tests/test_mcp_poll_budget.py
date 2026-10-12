import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp import Client

from wqb_agent import mcp_server, research_api


class MCPPollBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulation_tools_pass_bounded_poll_budget(self):
        single = Mock(return_value=[{
            "status": "DONE", "proposal_id": "single-1",
            "fingerprint": "a" * 64,
        }])
        multi = Mock(return_value=[
            {"status": "DONE", "proposal_id": f"multi-{i}",
             "fingerprint": f"{i:064x}"}
            for i in range(2)
        ])
        api = SimpleNamespace(
            simulate_batch=single,
            simulate_multi_batch=multi,
        )
        spec = {
            "expression": "rank(field)",
            "settings": {},
            "fields": ["field"],
            "proposal_id": "single-1",
        }
        multi_specs = [
            {**spec, "proposal_id": f"multi-{i}"}
            for i in range(2)
        ]
        with patch.dict(os.environ, {"ALPHA_FACTORY_ENABLE_SIMULATION_WRITES": "1"}):
            server = mcp_server.build_research_server(api=api, client=object())
        async with Client(server) as client:
            await client.call_tool("simulate_batch", {"specs": [spec]})
            await client.call_tool("simulate_multi_batch", {"specs": multi_specs})

        self.assertEqual(
            single.call_args.kwargs["poll_timeout_sec"],
            mcp_server.RECONCILE_POLL_TIMEOUT_SEC,
        )
        self.assertEqual(
            multi.call_args.kwargs["poll_timeout_sec"],
            mcp_server.RECONCILE_POLL_TIMEOUT_SEC,
        )


class ResearchAPIPollBudgetTests(unittest.TestCase):
    def test_write_facades_pass_poll_budget_to_gateway(self):
        def simulate_multi(specs, **_kwargs):
            return specs

        gateway = SimpleNamespace(
            simulate=lambda spec: [spec],
            simulate_batch=lambda specs: specs,
            simulate_multi_batch=simulate_multi,
        )
        with patch.object(research_api, "_simulation_gateway", return_value=gateway) as factory:
            self.assertEqual(
                research_api.simulate_batch(["single"], poll_timeout_sec=123),
                ["single"],
            )
            self.assertEqual(
                research_api.simulate_multi_batch(["multi"], poll_timeout_sec=234),
                ["multi"],
            )

        self.assertEqual(factory.call_args_list[0].kwargs["poll_timeout_sec"], 123)
        self.assertEqual(factory.call_args_list[1].kwargs["poll_timeout_sec"], 234)
