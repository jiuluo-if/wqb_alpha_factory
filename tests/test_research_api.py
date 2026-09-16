import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from wqb_agent.research_api import (
    SimulationSpec,
    discover_fields,
    find_similar_alphas,
    generate_probes,
    get_capabilities,
    get_operator_reference,
    get_simulation_modes,
    inspect_template,
    list_all_datafields,
    list_datafields,
    list_datasets,
    list_templates,
    simulate_multi_batch,
    simulate_single,
)


class TestResearchApi(unittest.TestCase):
    def test_simulation_modes_keep_single_and_multi_explicit(self):
        modes = get_simulation_modes()

        self.assertEqual(modes["single"]["name"], "Single Simulation")
        self.assertEqual(modes["single"]["max_concurrent"], 10)
        self.assertEqual(modes["multi"]["name"], "Multi-Simulation")
        self.assertEqual(modes["multi"]["children_per_job"], 10)
        self.assertEqual(modes["multi"]["max_concurrent_jobs"], 8)
        self.assertFalse(modes["region_agnostic"]["available"])

    def test_single_alias_and_multi_facade_delegate_to_gateway(self):
        spec = SimulationSpec("rank(close)")
        with mock.patch("wqb_agent.research_api._simulation_gateway") as factory:
            gateway = factory.return_value
            gateway.simulate.return_value = {"status": "DONE"}
            self.assertEqual(simulate_single(spec), {"status": "DONE"})
            gateway.simulate.assert_called_once_with(spec)

        with mock.patch("wqb_agent.research_api._simulation_gateway") as factory:
            gateway = factory.return_value
            gateway.simulate_multi_batch.return_value = [{"status": "DONE"}]
            result = simulate_multi_batch(
                [spec], child_batch_size=1, max_concurrent_multi=1
            )
            self.assertEqual(result, [{"status": "DONE"}])
            gateway.simulate_multi_batch.assert_called_once_with(
                [spec], child_batch_size=1, max_concurrent_multi=1
            )

    def test_list_all_datafields_reads_every_bounded_page(self):
        rows = [{"id": f"field_{index}", "type": "MATRIX"} for index in range(5)]
        calls = []

        def get_datafields(dataset_id, **kwargs):
            calls.append((dataset_id, kwargs))
            start = kwargs["offset"]
            stop = start + kwargs["limit"]
            return rows[start:stop], len(rows)

        client = SimpleNamespace(
            instrument_type="EQUITY",
            region="GLB",
            universe="TOPDIV3000",
            delay=1,
            get_datafields=get_datafields,
        )

        result = list_all_datafields(
            "analyst69", client=client, page_limit=2, field_type="MATRIX"
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["pages"], 3)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["fields"], rows)
        self.assertEqual([call[1]["offset"] for call in calls], [0, 2, 4])

    def test_list_datasets_exposes_live_scope(self):
        client = SimpleNamespace(
            instrument_type="EQUITY",
            region="GLB",
            universe="TOPDIV300",
            delay=1,
            get_datasets=lambda: [{"id": "analyst69"}],
        )

        result = list_datasets(client=client)

        self.assertEqual(result["source"], "LIVE")
        self.assertEqual(result["scope"]["region"], "GLB")
        self.assertEqual(result["scope"]["universe"], "TOPDIV300")
        self.assertEqual(result["datasets"], [{"id": "analyst69"}])

    def test_list_datafields_is_bounded_and_preserves_count(self):
        calls = []

        def get_datafields(dataset_id, **kwargs):
            calls.append((dataset_id, kwargs))
            return ([{"id": "field_a", "type": "MATRIX"}], 1)

        client = SimpleNamespace(
            instrument_type="EQUITY",
            region="GLB",
            universe="TOPDIV300",
            delay=1,
            get_datafields=get_datafields,
        )

        result = list_datafields(
            "analyst69", client=client, limit=10, offset=20, field_type="MATRIX"
        )

        self.assertEqual(result["source"], "LIVE")
        self.assertEqual(result["dataset_id"], "analyst69")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["fields"], [{"id": "field_a", "type": "MATRIX"}])
        self.assertEqual(
            calls,
            [(
                "analyst69",
                {"limit": 10, "offset": 20, "field_type": "MATRIX"},
            )],
        )
        with self.assertRaisesRegex(ValueError, "between 1 and 50"):
            list_datafields("analyst69", client=client, limit=51)

    def test_list_datafields_accepts_normalized_config(self):
        calls = []

        def get_datafields(dataset_id, **kwargs):
            calls.append((dataset_id, kwargs))
            return ([], 0)

        client = SimpleNamespace(
            instrument_type="EQUITY",
            region="GLB",
            universe="TOPDIV300",
            delay=1,
            get_datafields=get_datafields,
        )
        from wqb_agent.config import normalize_config

        list_datafields(
            "analyst69",
            client=client,
            config=normalize_config({"runtime": {"pagination_limit": 7}}),
        )

        self.assertEqual(calls[0][1]["limit"], 7)

    def test_remote_first_tool_surface_has_capabilities_and_templates(self):
        capabilities = get_capabilities(client=SimpleNamespace(
            get_operator_capability=lambda: {
                "status": "LIVE_VERIFIED", "availability": "AVAILABLE",
                "source": "BRAIN_LIVE_ONLY", "operators": ["rank"],
            }
        ))
        self.assertEqual(capabilities["operators"], ["rank"])
        templates = list_templates()
        self.assertTrue(templates)
        template_id = templates[0]["template_id"]
        self.assertEqual(inspect_template(template_id)["template_id"], template_id)

    def test_similarity_is_advisory_and_does_not_require_local_trajectory(self):
        rows = [
            {"alpha_id": "a1", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
            {"alpha_id": "a2", "alpha": {"regular": "rank(open)"}, "settings": {"delay": 1}},
        ]
        result = find_similar_alphas("rank(close)", rows=rows)
        self.assertEqual(result["kind"], "STRUCTURALLY_SIMILAR")
        self.assertEqual([item["alpha_id"] for item in result["matches"]], ["a2"])
    def test_discovery_without_agent_does_not_construct_legacy_runtime(self):
        client = SimpleNamespace()
        with mock.patch("wqb_agent.research_api.FieldDiscovery") as discovery_type:
            discovery = discovery_type.return_value
            discovery.discover.return_value = [{"id": "close", "type": "MATRIX"}]
            discovery.source_provenance.return_value = {"kind": "brain_api"}
            result = discover_fields("price reversal", client=client)
        self.assertEqual(result["fields"][0]["id"], "close")
        discovery_type.assert_called_once()

    def test_generate_probes_without_agent_does_not_construct_legacy_runtime(self):
        client = SimpleNamespace(get_operator_capability=lambda: {
            "valid": True, "operators": ["rank"],
        })
        factory = mock.Mock()
        factory.generate_probe_specs.return_value = [SimulationSpec("rank(close)")]
        with mock.patch("wqb_agent.research_api.FieldDiscovery") as discovery_type, \
                mock.patch("wqb_agent.research_api.AlphaFactory", return_value=factory):
            discovery = discovery_type.return_value
            discovery.discover.return_value = [{"id": "close", "type": "MATRIX"}]
            result = generate_probes(client=client, count=1)
        self.assertEqual(result, [SimulationSpec("rank(close)")])

    def test_operator_reference_requires_a_live_client(self):
        with self.assertRaises(RuntimeError):
            get_operator_reference()

    def test_operator_syntax_reference_is_explicitly_static(self):
        from wqb_agent.research_api import get_operator_syntax_reference

        reference = get_operator_syntax_reference()
        self.assertTrue(reference["sha256"])
        self.assertIn("rank", reference["operators"])
        self.assertEqual(reference["source"], "STATIC_SYNTAX_REFERENCE")
        self.assertEqual(reference["availability"], "UNKNOWN")

    def test_operator_reference_uses_client_capability(self):
        client = SimpleNamespace(get_operator_capability=lambda: {
            "key": "operators", "status": "LIVE_VERIFIED",
            "availability": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
            "operators": ["rank"], "capability_fingerprint": "fp",
        })
        self.assertEqual(get_operator_reference(client=client)["operators"], ["rank"])

    def test_operator_reference_uses_installed_package_resource(self):
        with mock.patch(
            "wqb_agent.research_api.load_packaged_operator_syntax_reference",
            return_value={"source": "STATIC_SYNTAX_REFERENCE", "operators": ["rank"]},
        ) as loader:
            result = __import__("wqb_agent.research_api", fromlist=["get_operator_syntax_reference"]).get_operator_syntax_reference()
        self.assertEqual(result["operators"], ["rank"])
        loader.assert_called_once_with()

    def test_clean_wheel_reads_operator_resource_outside_checkout(self):
        import pathlib
        import subprocess
        import sys
        import zipfile

        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            wheel_dir = pathlib.Path(directory) / "wheel"
            install_dir = pathlib.Path(directory) / "install"
            outside_dir = pathlib.Path(directory) / "outside"
            wheel_dir.mkdir()
            install_dir.mkdir()
            outside_dir.mkdir()
            subprocess.run(
                [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheel_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(install_dir)
            probe = (
                "import os, sys; "
                f"sys.path.insert(0, {str(install_dir)!r}); "
                f"os.chdir({str(outside_dir)!r}); "
                "from wqb_agent.research_api import get_operator_syntax_reference; "
                "assert 'rank' in get_operator_syntax_reference()['operators']"
            )
            subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
