import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from wqb_agent.config import normalize_config
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
    list_remote_alphas,
    list_templates,
    simulate_multi_batch,
    simulate_single,
)


class TestResearchApi(unittest.TestCase):
    def test_field_classification_exposes_type_dataset_and_semantics(self):
        from wqb_agent.research_api import classify_fields

        result = classify_fields([{
            "id": "close", "type": "MATRIX", "dataset": "prices",
            "description": "daily close price",
        }])

        self.assertEqual(result["source"], "DERIVED_METADATA")
        self.assertEqual(result["fields"][0]["type"], "MATRIX")
        self.assertEqual(result["fields"][0]["dataset"], "prices")
        self.assertEqual(result["fields"][0]["economic_meaning"], "market_level")
        self.assertEqual(result["fields"][0]["availability"], "AVAILABLE")

        vector = classify_fields([{
            "id": "embedding", "type": "VECTOR", "dataset": {"id": "alt"},
            "description": "daily sentiment vector",
        }])["fields"][0]
        self.assertEqual(vector["type"], "VECTOR")
        self.assertEqual(vector["dataset"], "alt")

    def test_simulation_helpers_validate_and_build_without_remote_write(self):
        from wqb_agent.research_api import (
            build_simulation_spec,
            get_simulation_config,
            validate_simulation_settings,
        )

        config = get_simulation_config()
        self.assertEqual(config["source"], "CONFIG")
        self.assertIn("neutralization", config["settings"])
        valid = validate_simulation_settings({
            "region": "USA", "universe": "TOP3000", "delay": 1,
            "decay": 6, "neutralization": "SUBINDUSTRY",
            "fields": ["close"],
        })
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["status"], "VALID")
        with self.assertRaises(ValueError):
            build_simulation_spec("rank(close)", settings={"delay": 2})
        spec = build_simulation_spec(
            "rank(close)", settings=valid["settings"], fields=["close"]
        )
        self.assertEqual(spec.expression, "rank(close)")

    def test_numeric_variant_changes_only_declared_occurrence_and_copies_base(self):
        from wqb_agent.alpha_templates import AlphaTemplate, TemplateNumericSlot
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "numeric-test", expression="rank(add(ts_mean({p}, 5), ts_mean({p}, 5)))",
            required_slots=("p",), role="CONTROL_ALPHA",
            semantic_contract="SYNTHETIC_FIXTURE",
            economic_mechanism="synthetic control", field_relationship="single field",
            direction_reason="synthetic", expected_horizon="short-term",
            falsification="synthetic falsification",
            numeric_slots=(TemplateNumericSlot(
                name="slow_window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="slow state", token="5",
                occurrence=1,
            ),),
        )
        base = SimulationSpec(
            "rank(add(ts_mean(field_a, 5), ts_mean(field_a, 5)))",
            settings={"delay": 1}, fields=("field_a",), template_id="numeric-test",
        )
        variant = build_simulation_variant(base, template, "slow_window", 22)
        self.assertEqual(variant.expression,
                         "rank(add(ts_mean(field_a, 5), ts_mean(field_a, 22)))")
        self.assertEqual(base.expression,
                         "rank(add(ts_mean(field_a, 5), ts_mean(field_a, 5)))")
        self.assertEqual(variant.settings, base.settings)
        self.assertEqual(variant.fields, base.fields)
        with self.assertRaisesRegex(ValueError, "no-op"):
            build_simulation_variant(base, template, "slow_window", 5)
        with self.assertRaises(ValueError):
            build_simulation_variant(base, template, "slow_window", 66)

    def test_template_crud_requires_explicit_private_catalog(self):
        from wqb_agent.research_api import (
            create_template,
            delete_template,
            inspect_template,
            update_template,
        )

        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "private.toml"
            with self.assertRaises(FileNotFoundError):
                create_template({}, catalog_path=missing)
            with self.assertRaises(FileNotFoundError):
                delete_template("x", catalog_path=missing)

    def test_private_template_crud_round_trip_is_persistent(self):
        from wqb_agent.alpha_templates.loader import load_builtin_templates
        from wqb_agent.research_api import (
            create_template,
            delete_template,
            update_template,
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path("wqb_agent/alpha_templates/catalog/builtin.toml")
            catalog = pathlib.Path(tmp) / "private.toml"
            catalog.write_text(
                source.read_text(encoding="utf-8").replace(
                    'semantic_contract = "SYNTHETIC_FIXTURE"',
                    'semantic_contract = "DATA_QUALITY"',
                ),
                encoding="utf-8",
            )
            template = replace(
                load_builtin_templates()[0],
                template_id="private_round_trip",
                semantic_contract="DATA_QUALITY",
            )
            created = create_template(template, catalog_path=catalog)
            self.assertEqual(created["template_id"], "private_round_trip")
            self.assertEqual(created["source"], "private_catalog")
            updated = replace(template, version="2")
            self.assertEqual(
                update_template("private_round_trip", updated, catalog_path=catalog)["version"],
                "2",
            )
            self.assertEqual(
                delete_template("private_round_trip", catalog_path=catalog)["status"],
                "DELETED",
            )
    def test_discovery_simulation_and_remote_boundaries_accept_equivalent_configs(self):
        raw = {
            "simulation": {},
            "runtime": {
                "pagination_limit": 7,
                "max_concurrent_sims": 2,
                "state_dir": "configured-state",
            },
            "remote_cache": {"retention_days": 3},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            forms = (raw, normalize_config(raw), str(path))
            for config in forms:
                self.assertEqual(
                    normalize_config(config).runtime.pagination_limit, 7
                )

            client = SimpleNamespace(
                instrument_type="EQUITY", region="GLB", universe="TOP3000", delay=1,
                get_datafields=lambda _dataset_id, **kwargs: ([], 0),
            )
            with mock.patch("wqb_agent.research_api.FieldDiscovery") as discovery_type:
                discovery_type.return_value.discover.return_value = [{"id": "close"}]
                discovery_type.return_value.source_provenance.return_value = {"kind": "synthetic"}
                for config in forms:
                    discovery_type.reset_mock()
                    result = discover_fields("reversal", client=client, config=config,
                                            state_dir=tmp)
                    self.assertEqual(result["fields"], [{"id": "close"}])
                    self.assertEqual(
                        discovery_type.call_args.kwargs["pagination_limit"], 7
                    )
            for config in forms:
                result = list_datafields("analyst69", client=client, config=config)
                self.assertEqual(result["limit"], 7)

            spec = SimulationSpec("rank(close)")
            with mock.patch("wqb_agent.research_api.SimulationGateway") as gateway_type:
                for config in forms:
                    gateway_type.reset_mock()
                    gateway_type.return_value.simulate.return_value = {"status": "DONE"}
                    simulate_single(spec, client=client, config=config, state_dir=tmp)
                    gateway_type.assert_called_once()
                    self.assertEqual(
                        gateway_type.call_args.kwargs["max_concurrent"], 2
                    )

            with mock.patch("wqb_agent.research_api.RemoteAlphaRepository") as repo_type:
                for config in forms:
                    repo_type.reset_mock()
                    repo_type.return_value.cache_status.return_value = {"retention_days": 3}
                    repo_type.return_value.list_remote_alphas.return_value = [{"alpha_id": "a"}]
                    from wqb_agent.research_api import remote_cache_status
                    result = remote_cache_status(config=config, state_dir=tmp)
                    self.assertEqual(result["retention_days"], 3)
                    self.assertEqual(
                        repo_type.call_args.kwargs["retention_days"], 3
                    )
                    listed = list_remote_alphas(config=config, state_dir=tmp)
                    self.assertEqual(listed, [{"alpha_id": "a"}])
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
        template = next(item for item in templates if item.get("numeric_slots"))
        template_id = template["template_id"]
        inspected = inspect_template(template_id)
        self.assertEqual(inspected["template_id"], template_id)
        self.assertEqual(inspected["numeric_slots"], template["numeric_slots"])

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

    def test_generate_probes_decouples_discovery_target_and_probe_target(self):
        config = {
            "simulation": {"region": "USA", "universe": "TOP3000", "delay": 1,
                            "decay": 4, "neutralization": "SUBINDUSTRY"},
            "runtime": {"fields_per_discovery": 5},
            "factory": {"default_probe_count": 9},
        }
        client = SimpleNamespace(get_operator_capability=lambda: {
            "valid": True, "status": "LIVE_VERIFIED", "availability": "AVAILABLE",
            "source": "BRAIN_LIVE_ONLY", "operators": ["rank"],
        })
        factory = mock.Mock()
        factory.generate_probe_specs.return_value = []
        with mock.patch("wqb_agent.research_api.FieldDiscovery") as discovery_type, \
                mock.patch("wqb_agent.research_api.AlphaFactory", return_value=factory):
            discovery_type.return_value.discover.return_value = [{"id": "field_a"}]
            generate_probes("query", count=2, client=client, config=config)
        discovery_type.return_value.discover.assert_called_once_with(
            mock.ANY, target_count=5
        )
        factory.generate_probe_specs.assert_called_once()
        call = factory.generate_probe_specs.call_args
        self.assertEqual(call.kwargs["target"], 2)
        self.assertEqual(call.kwargs["simulation_settings"],
                         config["simulation"])

    def test_generate_probes_uses_factory_default_when_count_is_omitted(self):
        config = {
            "simulation": {},
            "runtime": {"fields_per_discovery": 4},
            "factory": {"default_probe_count": 3},
        }
        client = SimpleNamespace(get_operator_capability=lambda: {
            "valid": True, "status": "LIVE_VERIFIED", "availability": "AVAILABLE",
            "source": "BRAIN_LIVE_ONLY", "operators": ["rank"],
        })
        factory = mock.Mock()
        factory.generate_probe_specs.return_value = []
        with mock.patch("wqb_agent.research_api.FieldDiscovery") as discovery_type, \
                mock.patch("wqb_agent.research_api.AlphaFactory", return_value=factory):
            discovery_type.return_value.discover.return_value = []
            generate_probes(client=client, config=config)
        self.assertEqual(factory.generate_probe_specs.call_args.kwargs["target"], 3)
        discovery_type.return_value.discover.assert_called_once_with(
            mock.ANY, target_count=4
        )

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
