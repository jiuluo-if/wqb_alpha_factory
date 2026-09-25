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

from wqb_agent import mcp_server
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


class ResearchMCPServerTests(unittest.IsolatedAsyncioTestCase):
    ENV = "ALPHA_FACTORY_ENABLE_SIMULATION_WRITES"

    @staticmethod
    def api(**overrides):
        defaults = {
            "research_status": lambda **_: {"source": "LIVE", "status": "AVAILABLE"},
            "list_datasets": lambda **_: {"source": "LIVE", "datasets": []},
            "list_datafields": lambda *_, **__: {"source": "LIVE", "fields": []},
            "get_operator_reference": lambda **_: {"source": "BRAIN_LIVE", "operators": []},
            "validate_simulation_spec": lambda spec, **_: {"status": "VALID", "expression": spec.expression},
            "simulate_batch": lambda specs, **_: [
                {"status": "DONE", "proposal_id": spec.proposal_id, "fingerprint": f"fp-{i}"}
                for i, spec in enumerate(specs)
            ],
            "simulate_multi_batch": lambda specs, **_: [
                {"status": "DONE", "proposal_id": spec.proposal_id, "fingerprint": f"fp-{i}"}
                for i, spec in enumerate(specs)
            ],
            "get_alpha_summary": lambda *_, **__: {"source": "LIVE", "alpha": {}},
            "get_alpha_evidence": lambda *_, **__: {"source": "LIVE", "alpha": {}},
            "reconcile_execution": lambda fingerprint, **_: {"status": "SUBMIT_UNKNOWN", "fingerprint": fingerprint},
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    async def test_research_server_fails_closed_without_explicit_opt_in(self):
        for value in (None, "0", "true"):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=False):
                os.environ.pop(self.ENV, None)
                if value is not None:
                    os.environ[self.ENV] = value
                with self.assertRaisesRegex(RuntimeError, "RESEARCH_WRITE_MODE_NOT_ENABLED"):
                    mcp_server.build_research_server(api=self.api(), client=object())

    async def test_research_server_exposes_bounded_surface_and_write_annotations(self):
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=self.api(), client=object())
        async with Client(server) as client:
            listed = await client.list_tools()

        by_name = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(by_name), {
            "research_status", "list_datasets", "list_datafields",
            "get_operator_reference", "validate_simulation_spec",
            "simulate_batch", "simulate_multi_batch", "get_alpha_summary",
            "get_alpha_evidence", "reconcile_execution",
        })
        self.assertFalse(by_name["simulate_batch"].annotations.read_only_hint)
        self.assertFalse(by_name["simulate_multi_batch"].annotations.read_only_hint)
        self.assertTrue(by_name["research_status"].annotations.read_only_hint)
        for tool in listed.tools:
            self.assertNotIn("state_dir", tool.input_schema.get("properties", {}))
        self.assertNotIn("alpha_submission", by_name)

    async def test_research_status_is_first_tool_and_performs_live_handshake(self):
        status = Mock(return_value={"source": "LIVE", "status": "AVAILABLE", "capability": {"status": "LIVE_VERIFIED"}})
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(
                api=self.api(research_status=status), client=object(), state_dir="synthetic-state",
            )
        async with Client(server) as client:
            listed = await client.list_tools()
            result = await client.call_tool(listed.tools[0].name, {})
        self.assertEqual(listed.tools[0].name, "research_status")
        self.assertEqual(result.structured_content["data"]["capability"]["status"], "LIVE_VERIFIED")
        self.assertEqual(status.call_args.kwargs["state_dir"], "synthetic-state")

    async def test_opted_in_research_entrypoint_serves_stdio_inventory(self):
        env = os.environ.copy()
        env[self.ENV] = "1"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from wqb_agent.mcp_server import research_main; research_main()"],
            cwd=ROOT,
            env=env,
        )
        async with Client(parameters) as client:
            listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        self.assertEqual(len(names), 10)
        self.assertTrue({"research_status", "simulate_batch", "simulate_multi_batch"} <= names)

    async def test_alpha_expression_is_available_only_through_requested_evidence(self):
        expression = "rank(synthetic_field)"
        evidence = self.api(get_alpha_evidence=lambda *_, **__: {
            "source": "LIVE", "alpha": {"expression": expression},
        })
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=evidence, client=object())
        async with Client(server) as client:
            result = await client.call_tool("get_alpha_evidence", {"alpha_id": "alpha-1"})
        self.assertEqual(result.structured_content["data"]["alpha"]["expression"], expression)

    async def test_simulation_batch_routes_facade_and_returns_only_small_projection(self):
        submit = Mock(return_value=[{
            "status": "SUBMIT_UNKNOWN", "reason_code": "SUBMIT_UNKNOWN",
            "proposal_id": "p-1", "note": "token=secret-token", "template_id": "t-1",
            "fingerprint": "fp-1", "alpha_id": None,
            "progress_url": "https://brain.invalid/?token=secret-token",
            "evidence": {"expression": "private-expression", "metrics": {"x": 1}},
            "field_validation": "LIVE_VERIFIED",
            "remote_duplicate_scan": {"status": "BOUNDED_RECENT_WINDOW"},
        }])
        multi = Mock()
        fake_api = self.api(simulate_batch=submit, simulate_multi_batch=multi)
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=fake_api, client=object(), state_dir="synthetic-state")
        async with Client(server) as client:
            result = await client.call_tool("simulate_batch", {"specs": [{
                "expression": "rank(close)", "settings": {}, "fields": [],
                "field_datasets": {}, "proposal_id": "p-1", "note": "H1",
                "template_id": "t-1", "simulation_type": "REGULAR",
            }]})

        submit.assert_called_once()
        args, kwargs = submit.call_args
        self.assertEqual(len(args[0]), 1)
        self.assertEqual(args[0][0].expression, "rank(close)")
        self.assertEqual(kwargs["state_dir"], "synthetic-state")
        data = result.structured_content
        self.assertTrue(submit.called)
        self.assertEqual(data.get("error"), None, msg=repr(data))
        self.assertEqual(len(data["results"]), 1, msg=repr(data))
        self.assertEqual(data["results"][0]["status"], "SUBMIT_UNKNOWN")
        self.assertEqual(data["remote_duplicate_scan"]["status"], "BOUNDED_RECENT_WINDOW")
        self.assertNotIn("progress_url", data["results"][0])
        self.assertNotIn("private-expression", str(data))
        self.assertNotIn("secret-token", str(data))
        self.assertEqual(data["results"][0]["note"], "[REDACTED]")
        self.assertNotIn("evidence", data["results"][0])
        multi.assert_not_called()

    async def test_multi_batch_routes_gateway_and_preserves_child_guard_statuses(self):
        multi = Mock(return_value=[
            {"status": "EXACT_DUPLICATE", "fingerprint": "child-1", "batch_fingerprint": "parent-1"},
            {"status": "SUBMIT_UNKNOWN", "fingerprint": "child-2", "batch_fingerprint": "parent-1"},
        ])
        fake_api = self.api(simulate_multi_batch=multi)
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=fake_api, client=object())
        specs = [{"expression": "rank(close)", "proposal_id": f"p-{i}"} for i in range(2)]
        async with Client(server) as client:
            result = await client.call_tool("simulate_multi_batch", {"specs": specs})

        multi.assert_called_once()
        self.assertTrue(multi.called)
        self.assertEqual(
            [row["status"] for row in result.structured_content["results"]],
            ["EXACT_DUPLICATE", "SUBMIT_UNKNOWN"],
        )

    async def test_transport_bounds_reject_oversized_batch_without_calling_facade(self):
        submit = Mock(return_value=[])
        fake_api = self.api(simulate_batch=submit)
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=fake_api, client=object())
        async with Client(server) as client:
            result = await client.call_tool(
                "simulate_batch", {"specs": [{"expression": "rank(close)"}] * 51},
            )
        self.assertEqual(result.structured_content["error"], "INVALID_ARGUMENT")
        submit.assert_not_called()

    async def test_batch_result_keeps_all_candidates_within_output_contract(self):
        submit = Mock(return_value=[
            {"status": "DONE", "proposal_id": f"p-{i}", "fingerprint": f"fp-{i}"}
            for i in range(50)
        ])
        fake_api = self.api(simulate_batch=submit)
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=fake_api, client=object())
        specs = [{"expression": "rank(close)", "proposal_id": f"p-{i}"} for i in range(50)]
        async with Client(server) as client:
            result = await client.call_tool("simulate_batch", {"specs": specs})
        payload = json.dumps(result.structured_content, separators=(",", ":"))
        self.assertLessEqual(len(payload.encode("utf-8")), MAX_RESULT_BYTES)
        self.assertEqual(len(result.structured_content["results"]), 50, msg=repr(result.structured_content))
        self.assertFalse(result.structured_content["truncated"])

    async def test_multi_batch_keeps_all_100_candidates_and_marks_bounded_labels(self):
        multi = Mock(return_value=[
            {"status": "DONE", "proposal_id": f"p-{i}", "note": "n" * 200,
             "fingerprint": f"fp-{i}"}
            for i in range(100)
        ])
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=self.api(simulate_multi_batch=multi), client=object())
        specs = [{"expression": "rank(close)", "proposal_id": f"p-{i}"} for i in range(100)]
        async with Client(server) as client:
            result = await client.call_tool("simulate_multi_batch", {"specs": specs})
        payload = json.dumps(result.structured_content, separators=(",", ":"))
        self.assertLessEqual(len(payload.encode("utf-8")), MAX_RESULT_BYTES)
        self.assertEqual(len(result.structured_content["results"]), 100)
        self.assertTrue(result.structured_content["truncated"])

    async def test_write_failures_do_not_return_exception_messages_or_drop_candidates(self):
        def fail(*_, **__):
            raise RuntimeError("secret-token cookie=private")
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=self.api(simulate_batch=fail), client=object())
        async with Client(server) as client:
            result = await client.call_tool("simulate_batch", {"specs": [
                {"expression": "rank(close)", "proposal_id": "p-1"},
                {"expression": "rank(volume)", "proposal_id": "p-2"},
            ]})
        payload = str(result.structured_content)
        self.assertEqual(len(result.structured_content["results"]), 2)
        self.assertEqual(result.structured_content["error"], "REMOTE_WRITE_FAILED")
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("private", payload)

    async def test_simulation_specs_reject_unknown_keys_including_state_dir(self):
        submit = Mock(return_value=[])
        fake_api = self.api(simulate_batch=submit)
        with patch.dict(os.environ, {self.ENV: "1"}):
            server = mcp_server.build_research_server(api=fake_api, client=object())
        async with Client(server) as client:
            result = await client.call_tool("simulate_batch", {"specs": [{
                "expression": "rank(close)", "state_dir": "elsewhere",
            }]})
        self.assertEqual(result.structured_content["error"], "INVALID_ARGUMENT")
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
