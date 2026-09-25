import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from wqb_agent.mcp_server import MAX_RESULT_BYTES, _envelope, build_server

ROOT = Path(__file__).resolve().parents[1]


class ReadOnlyMCPServerTests(unittest.IsolatedAsyncioTestCase):
    def test_oversized_result_drops_payload_instead_of_returning_an_unbounded_table(self):
        payload = {"source": "LIVE", "rows": [{"value": "x" * 8_000} for _ in range(12)]}

        result = _envelope(payload, owner="research_api.synthetic")

        self.assertTrue(result["truncated"])
        self.assertIsNone(result["data"])
        self.assertEqual(result["truncation_reason"], "MAX_RESULT_BYTES")
        self.assertLessEqual(len(str(result).encode("utf-8")), MAX_RESULT_BYTES)

    def test_oversized_envelope_metadata_is_bounded_too(self):
        payload = {
            "source": "s" * 100_000,
            "status": "u" * 100_000,
            "fetched_at": "t" * 100_000,
            "rows": [],
        }

        result = _envelope(payload, owner="research_api.synthetic")

        self.assertTrue(result["truncated"])
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            MAX_RESULT_BYTES,
        )

    async def test_live_tools_resolve_one_existing_client_lazily(self):
        fake_client = object()

        def get_capabilities(*, client=None, config=None):
            self.assertIs(client, fake_client)
            return {
                "source": "BRAIN_LIVE_ONLY", "status": "LIVE_VERIFIED",
                "evidence_status": "AVAILABLE", "operators": ["rank"],
            }

        api = SimpleNamespace(get_capabilities=get_capabilities)
        with patch("wqb_agent.client.WQBClient", return_value=fake_client) as factory:
            server = build_server(api=api)
            async with Client(server) as client:
                result = await client.call_tool("get_capabilities", {})

        factory.assert_called_once_with()
        self.assertEqual(result.structured_content["status"], "LIVE_VERIFIED")

    async def test_stdio_runtime_lists_the_same_read_only_surface(self):
        script_name = "alpha-factory-mcp.exe" if os.name == "nt" else "alpha-factory-mcp"
        script = Path(sys.executable).with_name(script_name)
        command = str(script) if script.is_file() else shutil.which("alpha-factory-mcp")
        parameters = StdioServerParameters(
            command=command or sys.executable,
            args=[] if command else ["-m", "wqb_agent.mcp_server"],
            cwd=ROOT,
        )

        async with Client(parameters) as client:
            listed = await client.list_tools()

        self.assertEqual(len(listed.tools), 5)
        self.assertTrue(all(tool.annotations.read_only_hint for tool in listed.tools))

    async def test_lists_only_read_only_facade_tools_and_calls_them_in_process(self):
        api = SimpleNamespace(
            get_capabilities=lambda **_: {
                "source": "BRAIN_LIVE_ONLY",
                "status": "UNKNOWN",
                "evidence_status": "UNAVAILABLE",
                "operators": [],
            },
            get_simulation_modes=lambda **_: {
                "single": {"source": "UNKNOWN", "status": "UNKNOWN"},
                "multi": {"source": "UNKNOWN", "status": "UNKNOWN"},
            },
            list_datafields=lambda *args, **kwargs: {
                "source": "LIVE", "fields": [{"id": "close"}],
            },
            get_alpha_evidence=lambda *args, **kwargs: {
                "alpha_id": "synthetic-alpha",
                "source": "LIVE", "fetched_at": 1.0,
                "status": {"alpha_detail": "AVAILABLE"},
                "alpha": {"id": "synthetic-alpha", "expression": "rank(close)"},
                "recordsets": {},
            },
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            listed = await client.list_tools()
            tools = listed.tools
            self.assertIsNone(listed.next_cursor)
            names = {tool.name for tool in tools}
            self.assertEqual(names, {
                "get_capabilities", "get_simulation_modes", "list_datafields",
                "get_alpha_evidence", "get_pending_executions",
            })
            self.assertTrue(all(tool.annotations.read_only_hint for tool in tools))
            result = await client.call_tool("get_capabilities", {})

            self.assertEqual(result.structured_content["access_mode"], "READ_ONLY")
            self.assertEqual(result.structured_content["source"], "BRAIN_LIVE_ONLY")
            self.assertEqual(result.structured_content["evidence_status"], "UNAVAILABLE")
            modes = await client.call_tool("get_simulation_modes", {})
            self.assertEqual(
                modes.structured_content["data"]["modes"]["single"]["status"],
                "UNKNOWN",
            )

    async def test_datafields_are_bounded_and_secrets_are_removed(self):
        seen = {}

        def list_datafields(*args, **kwargs):
            seen.update(kwargs)
            return {"source": "LIVE", "fields": [
                {
                    "id": str(index),
                    "api_token": "synthetic-secret",
                    "expression": "private-expression",
                }
                for index in range(30)
            ]}

        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=list_datafields,
            get_alpha_evidence=lambda *args, **kwargs: {},
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            result = await client.call_tool(
                "list_datafields", {"dataset_id": "synthetic-dataset", "limit": 50},
            )

        data = result.structured_content
        self.assertEqual(seen["limit"], 50)
        self.assertEqual(len(data["data"]["fields"]), 20)
        self.assertTrue(data["truncated"])
        self.assertIn("[REDACTED]", str(data))
        self.assertNotIn("synthetic-secret", str(data))
        self.assertNotIn("private-expression", str(data))

    async def test_alpha_expression_is_only_returned_through_authorized_evidence_tool(self):
        expression = "rank(synthetic_field)"
        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=lambda *args, **kwargs: {},
            get_alpha_evidence=lambda *args, **kwargs: {
                "source": "LIVE", "alpha": {"expression": expression},
                "recordsets": {},
            },
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            result = await client.call_tool(
                "get_alpha_evidence", {"alpha_id": "synthetic-alpha"},
            )

        self.assertEqual(result.structured_content["data"]["alpha"]["expression"], expression)
        self.assertEqual(result.structured_content["source"], "LIVE")

    async def test_pending_guard_is_only_read_using_the_configured_directory(self):
        calls = []

        def read_pending(**kwargs):
            calls.append(kwargs)
            return {"entries": [{"status": "SUBMIT_UNKNOWN", "simulation_count": 2}]}

        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=lambda *args, **kwargs: {},
            get_alpha_evidence=lambda *args, **kwargs: {},
            get_pending_executions=read_pending,
        )
        server = build_server(api=api, config={"runtime": {}}, state_dir="synthetic-state")

        async with Client(server) as client:
            result = await client.call_tool("get_pending_executions", {})

        self.assertEqual(calls, [{"state_dir": "synthetic-state", "config": {"runtime": {}}}])
        self.assertEqual(result.structured_content["source"], "LOCAL_EXECUTION_GUARD")
        self.assertEqual(result.structured_content["data"]["entries"][0]["status"], "SUBMIT_UNKNOWN")

    async def test_too_many_recordsets_are_rejected_before_facade_call(self):
        evidence_reader = Mock(return_value={"source": "LIVE"})
        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=lambda *args, **kwargs: {},
            get_alpha_evidence=evidence_reader,
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            result = await client.call_tool(
                "get_alpha_evidence",
                {"alpha_id": "synthetic-alpha", "recordsets": ["one", "two", "three"]},
            )

        self.assertEqual(result.structured_content["error"], "INVALID_ARGUMENT")
        evidence_reader.assert_not_called()

    async def test_evidence_depth_follows_the_selected_recordsets(self):
        calls = []

        def read_evidence(*args, **kwargs):
            calls.append((args, kwargs))
            return {"source": "LIVE", "depth": "FULL"}

        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=lambda *args, **kwargs: {},
            get_alpha_evidence=read_evidence,
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            await client.call_tool(
                "get_alpha_evidence", {"alpha_id": "synthetic-alpha"},
            )
            await client.call_tool(
                "get_alpha_evidence",
                {"alpha_id": "synthetic-alpha", "recordsets": ["coverage"]},
            )

        # The cheap default must not turn into a full read, and selecting
        # recordsets must not hit the "recordsets require full depth" guard.
        self.assertEqual(calls[0][1]["depth"], "summary")
        self.assertEqual(calls[0][1]["recordsets"], [])
        self.assertEqual(calls[1][1]["depth"], "full")
        self.assertEqual(calls[1][1]["recordsets"], ["coverage"])

    async def test_invalid_input_is_rejected_before_remote_facade_call(self):
        field_reader = Mock(return_value={"source": "LIVE", "fields": []})
        api = SimpleNamespace(
            get_capabilities=lambda **_: {},
            get_simulation_modes=lambda **_: {},
            list_datafields=field_reader,
            get_alpha_evidence=lambda *args, **kwargs: {},
            get_pending_executions=lambda **_: {"entries": []},
        )
        server = build_server(api=api, client=object(), state_dir="synthetic-state")

        async with Client(server) as client:
            result = await client.call_tool(
                "list_datafields", {"dataset_id": "x" * 257, "limit": 1},
            )

        self.assertEqual(result.structured_content["error"], "INVALID_ARGUMENT")
        field_reader.assert_not_called()

    async def test_auth_and_rate_limit_errors_are_classified_without_raw_messages(self):
        class FakeReadError(Exception):
            def __init__(self, status_code, message):
                super().__init__(message)
                self.status_code = status_code

        for status, expected in (
            (401, "AUTHENTICATION_REQUIRED"),
            (403, "PERMISSION_DENIED"),
            (404, "EVIDENCE_NOT_FOUND"),
            (429, "RATE_LIMITED"),
        ):
            with self.subTest(status=status):
                api = SimpleNamespace(
                    get_capabilities=lambda **_: (_ for _ in ()).throw(
                        FakeReadError(status, "secret-token private-expression")
                    ),
                    get_simulation_modes=lambda **_: {},
                    list_datafields=lambda *args, **kwargs: {},
                    get_alpha_evidence=lambda *args, **kwargs: {},
                    get_pending_executions=lambda **_: {"entries": []},
                )
                server = build_server(api=api, client=object(), state_dir="synthetic-state")
                async with Client(server) as client:
                    result = await client.call_tool("get_capabilities", {})
                data = result.structured_content
                self.assertEqual(data["evidence_status"], "UNAVAILABLE")
                self.assertEqual(data["error"], expected)
                self.assertNotIn("secret-token", str(data))
                self.assertNotIn("private-expression", str(data))


if __name__ == "__main__":
    unittest.main()
