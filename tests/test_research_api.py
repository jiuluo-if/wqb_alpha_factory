import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from wqb_agent.alpha_grouping import variant_family_fingerprint
from wqb_agent.config import normalize_config
from wqb_agent.research_api import (
    SimulationSpec,
    discover_fields,
    find_similar_alphas,
    generate_probes,
    get_alpha_aggregates,
    get_alpha_evidence,
    get_alpha_metrics,
    get_alpha_pnl,
    get_alpha_recordsets,
    get_alpha_self_correlation,
    get_capabilities,
    get_live_preflight,
    get_operator_reference,
    get_pending_executions,
    get_simulation_modes,
    inspect_template,
    list_all_datafields,
    list_datafields,
    list_datasets,
    list_remote_alphas,
    list_templates,
    simulate,
    simulate_multi_batch,
    validate_simulation_settings,
)
from wqb_agent.simulation_gateway import SimulationGateway


class TestResearchApi(unittest.TestCase):
    def test_spec_facades_share_regular_only_writer_contract(self):
        from wqb_agent.research_api import (
            build_simulation_spec,
            execution_fingerprint,
            validate_simulation_spec,
        )

        client = SimpleNamespace()
        for simulation_type in ("REGION_AGNOSTIC", "SUPER", "BOGUS"):
            with self.subTest(simulation_type=simulation_type):
                with self.assertRaisesRegex(
                    ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                ):
                    build_simulation_spec(
                        "rank(close)", simulation_type=simulation_type
                    )
                spec = SimulationSpec(
                    "rank(close)", simulation_type=simulation_type
                )
                with self.assertRaisesRegex(
                    ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                ):
                    validate_simulation_spec(spec, client=client)
                with self.assertRaisesRegex(
                    ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                ):
                    execution_fingerprint(spec, client=client)

    def test_numeric_variant_rejects_non_regular_base_spec(self):
        from wqb_agent.alpha_templates import AlphaTemplate, TemplateNumericSlot
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "non-regular-base", expression="rank(ts_mean({p}, 5))",
            required_slots=("p",), role="CONTROL_ALPHA",
            semantic_contract="SYNTHETIC_FIXTURE", economic_mechanism="synthetic",
            field_relationship="single field", direction_reason="synthetic",
            expected_horizon="short-term", falsification="synthetic",
            numeric_slots=(TemplateNumericSlot(
                name="window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="synthetic", token="5",
                occurrence=0,
            ),),
        )
        base = SimulationSpec(
            "rank(ts_mean(field_a, 5))", fields=("field_a",),
            template_id=template.template_id, simulation_type="SUPER",
        )

        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_SIMULATION_TYPE"):
            build_simulation_variant(base, template, "window", 22)

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
        self.assertEqual(
            build_simulation_spec("rank(close)", settings={"delay": 2}).settings["delay"],
            2,
        )
        spec = build_simulation_spec(
            "rank(close)", settings=valid["settings"], fields=["close"]
        )
        self.assertEqual(spec.expression, "rank(close)")

    def test_numeric_variant_changes_only_declared_occurrence_and_copies_base(self):
        from wqb_agent.alpha_templates import AlphaTemplate, TemplateNumericSlot
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "numeric-test", expression=(
                "scale(normalize(rank(subtract(ts_mean({p}, 5), "
                "ts_mean({s}, 5))))"
            ), required_slots=("p", "s"), role="PROBE_ALPHA",
            semantic_contract="SYNTHETIC_FIXTURE",
            economic_mechanism="synthetic probe", field_relationship="paired fields",
            direction_reason="synthetic", expected_horizon="short-term",
            falsification="synthetic falsification",
            numeric_slots=(TemplateNumericSlot(
                name="slow_window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="slow state", token="5",
                occurrence=1,
            ),),
        )
        base = SimulationSpec(
            "scale(normalize(rank(subtract(ts_mean(field_a, 5), "
            "ts_mean(field_b, 5)))))",
            settings={"delay": 1}, fields=("field_a", "field_b"), template_id="numeric-test",
        )
        variant = build_simulation_variant(base, template, "slow_window", 22)
        self.assertEqual(variant.expression,
                         "scale(normalize(rank(subtract(ts_mean(field_a, 5), "
                         "ts_mean(field_b, 22)))))")
        self.assertEqual(
            variant_family_fingerprint(base.expression),
            variant_family_fingerprint(variant.expression),
        )
        self.assertEqual(base.expression,
                         "scale(normalize(rank(subtract(ts_mean(field_a, 5), "
                         "ts_mean(field_b, 5)))))")
        self.assertEqual(variant.settings, base.settings)
        self.assertEqual(variant.fields, base.fields)
        with self.assertRaisesRegex(ValueError, "no-op"):
            build_simulation_variant(base, template, "slow_window", 5)
        with self.assertRaises(ValueError):
            build_simulation_variant(base, template, "slow_window", 66)

    def test_numeric_variant_rejects_anchor_over_role_operator_ceiling(self):
        from wqb_agent.alpha_templates import AlphaTemplate, TemplateNumericSlot
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "too-complex-control", expression="rank(add(ts_mean({p}, 5), ts_mean({p}, 5)))",
            required_slots=("p",), role="CONTROL_ALPHA",
            semantic_contract="SYNTHETIC_FIXTURE", economic_mechanism="synthetic",
            field_relationship="single field", direction_reason="synthetic",
            expected_horizon="short-term", falsification="synthetic",
            numeric_slots=(TemplateNumericSlot(
                name="window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="synthetic", token="5", occurrence=1,
            ),),
        )
        base = SimulationSpec(
            "rank(add(ts_mean(field_a, 5), ts_mean(field_a, 5)))",
            settings={}, fields=("field_a",), template_id=template.template_id,
        )
        with self.assertRaisesRegex(ValueError, "INVALID_OPTIMIZATION_ANCHOR"):
            build_simulation_variant(base, template, "window", 22)

    def test_numeric_variant_rejects_operator_topology_change(self):
        from wqb_agent.alpha_templates import (
            AlphaTemplate,
            TemplateNumericSlot,
            TemplateOperatorSlot,
        )
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "partial-optimization", expression="rank({relation}(ts_mean({p}, 5), ts_mean({s}, 5)))",
            required_slots=("p", "s"), role="PROBE_ALPHA", template_mode="PARTIAL_OPERATOR",
            operator_slots=(TemplateOperatorSlot(
                name="relation", role="relation", placeholder="{relation}",
                baseline_operator="ts_corr", allowed_operators=("ts_corr", "ts_covariance"),
                semantic_contract="synthetic",
            ),), semantic_contract="SYNTHETIC_FIXTURE", economic_mechanism="synthetic",
            field_relationship="paired fields", direction_reason="synthetic",
            expected_horizon="short-term", falsification="synthetic",
            numeric_slots=(TemplateNumericSlot(
                name="window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="synthetic", token="5", occurrence=0,
            ),),
        )
        base = SimulationSpec(
            "rank(ts_delta(ts_mean(field_a, 5), ts_mean(field_b, 5)))",
            settings={}, fields=("field_a", "field_b"), template_id=template.template_id,
        )
        with self.assertRaisesRegex(ValueError, "NEW_PROBE_REQUIRED"):
            build_simulation_variant(base, template, "window", 22)

    def test_partial_operator_anchor_realization_is_frozen_but_allowed(self):
        from wqb_agent.alpha_templates import (
            AlphaTemplate,
            TemplateNumericSlot,
            TemplateOperatorSlot,
        )
        from wqb_agent.research_api import build_simulation_variant

        template = AlphaTemplate(
            "partial-allowed", expression="rank({relation}(ts_mean({p}, 5), ts_mean({s}, 5)))",
            required_slots=("p", "s"), role="PROBE_ALPHA", template_mode="PARTIAL_OPERATOR",
            operator_slots=(TemplateOperatorSlot(
                name="relation", role="relation", placeholder="{relation}",
                baseline_operator="ts_corr", allowed_operators=("ts_corr", "ts_covariance"),
                semantic_contract="synthetic",
            ),), semantic_contract="SYNTHETIC_FIXTURE", economic_mechanism="synthetic",
            field_relationship="paired fields", direction_reason="synthetic",
            expected_horizon="short-term", falsification="synthetic",
            numeric_slots=(TemplateNumericSlot(
                name="window", kind="RESEARCH_HORIZON", default=5,
                allowed_values=(5, 22), economic_role="synthetic", token="5", occurrence=0,
            ),),
        )
        base = SimulationSpec(
            "rank(ts_covariance(ts_mean(field_a, 5), ts_mean(field_b, 5)))",
            settings={}, fields=("field_a", "field_b"), template_id=template.template_id,
        )
        variant = build_simulation_variant(base, template, "window", 22)
        self.assertEqual(
            variant.expression,
            "rank(ts_covariance(ts_mean(field_a, 22), ts_mean(field_b, 5)))",
        )

    def test_settings_variant_requires_anchor_expression(self):
        from wqb_agent.research_api import build_simulation_spec

        anchor = SimulationSpec("rank(field_a)", settings={"decay": 4}, template_id="synthetic")
        variant = build_simulation_spec(
            " RANK( field_a ) ", settings={"decay": 6}, template_id="synthetic",
            anchor_spec=anchor,
        )
        self.assertEqual(variant.expression.replace(" ", "").lower(), "rank(field_a)")
        with self.assertRaisesRegex(ValueError, "NEW_PROBE_REQUIRED"):
            build_simulation_spec(
                "normalize(rank(field_a))", settings={"decay": 6},
                template_id="synthetic", anchor_spec=anchor,
            )

    def test_settings_variant_inherits_anchor_identity(self):
        from wqb_agent.research_api import build_simulation_spec

        anchor = SimulationSpec(
            "rank(field_a)", settings={"decay": 4}, fields=("field_a",),
            template_id="synthetic", simulation_type="REGULAR",
        )
        variant = build_simulation_spec(
            "rank(field_a)", settings={"decay": 6}, anchor_spec=anchor,
        )
        self.assertEqual(variant.fields, ("field_a",))
        self.assertEqual(variant.template_id, "synthetic")
        self.assertEqual(variant.simulation_type, anchor.simulation_type)
        self.assertEqual(variant.expression, anchor.expression)

    def test_settings_variant_rejects_anchor_identity_changes(self):
        from wqb_agent.research_api import build_simulation_spec

        anchor = SimulationSpec(
            "rank(field_a)", fields=("field_a",), template_id="synthetic",
        )
        cases = (
            {"fields": ["field_b"]},
            {"template_id": "synthetic-other"},
            {"simulation_type": "SUPER"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, "NEW_PROBE_REQUIRED"):
                    build_simulation_spec(
                        "rank(field_a)", settings={"decay": 6},
                        anchor_spec=anchor, **overrides,
                    )

    def test_settings_variant_rejects_new_template_identity(self):
        from wqb_agent.research_api import build_simulation_spec

        anchor = SimulationSpec("rank(field_a)", fields=("field_a",))
        with self.assertRaisesRegex(ValueError, "NEW_PROBE_REQUIRED"):
            build_simulation_spec(
                "rank(field_a)", settings={"decay": 6},
                template_id="synthetic", anchor_spec=anchor,
            )

    def test_settings_variant_rejects_non_regular_anchor_before_construction(self):
        from wqb_agent.research_api import build_simulation_spec

        for simulation_type in ("SUPER", "REGION_AGNOSTIC"):
            with self.subTest(simulation_type=simulation_type):
                anchor = SimulationSpec(
                    "rank(field_a)", fields=("field_a",),
                    simulation_type=simulation_type,
                )
                with self.assertRaisesRegex(
                    ValueError, "UNSUPPORTED_SIMULATION_TYPE"
                ):
                    build_simulation_spec(
                        "rank(field_a)", settings={"decay": 6},
                        anchor_spec=anchor,
                    )

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
                    simulate(spec, client=client, config=config, state_dir=tmp)
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
        client = SimpleNamespace(
            get_authentication_status=lambda: {
                "authenticated": True, "user_id": "user-1",
                "permissions": ["MULTI_SIMULATION"],
            },
            get_simulation_capability=lambda: {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "source": "BRAIN_LIVE", "simulation_type_choices": ["REGULAR", "SUPER"],
                "settings": {}, "required_fields": [], "required_settings": [],
            },
        )
        modes = get_simulation_modes(client=client)

        self.assertEqual(modes["single"]["name"], "Single Simulation")
        self.assertEqual(modes["single"]["max_concurrent"], 10)
        self.assertEqual(modes["multi"]["name"], "Multi-Simulation")
        self.assertEqual(modes["multi"]["children_per_job"], 10)
        self.assertEqual(modes["multi"]["min_children_per_job"], 2)
        self.assertEqual(modes["multi"]["max_children_per_job"], 10)
        self.assertEqual(modes["multi"]["default_concurrent_jobs"], 2)
        self.assertEqual(modes["multi"]["max_concurrent_jobs"], 8)
        self.assertFalse(modes["region_agnostic"]["available"])

    def test_simulation_mode_reason_projection_truth_table(self):
        cases = (
            (
                "unauthenticated_available",
                {"authenticated": False, "permissions": []},
                {"status": "AVAILABLE", "simulation_type_choices": ["REGULAR"]},
                "AUTHENTICATION_REQUIRED", "AUTHENTICATION_REQUIRED",
            ),
            (
                "unauthenticated_unknown_options",
                {"authenticated": False, "permissions": []},
                {"status": "UNKNOWN", "simulation_type_choices": []},
                "AUTHENTICATION_REQUIRED", "AUTHENTICATION_REQUIRED",
            ),
            (
                "authenticated_unknown_options",
                {"authenticated": True, "permissions": []},
                {"status": "UNKNOWN", "simulation_type_choices": []},
                "CAPABILITY_UNKNOWN", "CAPABILITY_UNKNOWN",
            ),
            (
                "authenticated_without_regular",
                {"authenticated": True, "permissions": ["MULTI_SIMULATION"]},
                {"status": "AVAILABLE", "simulation_type_choices": ["SUPER"]},
                "CAPABILITY_UNKNOWN", "CAPABILITY_UNKNOWN",
            ),
            (
                "authenticated_single_only",
                {"authenticated": True, "permissions": []},
                {"status": "AVAILABLE", "simulation_type_choices": ["REGULAR"]},
                None, "PERMISSION_UNAVAILABLE",
            ),
            (
                "authenticated_multi_available",
                {"authenticated": True, "permissions": ["MULTI_SIMULATION"]},
                {"status": "AVAILABLE", "simulation_type_choices": ["REGULAR"]},
                None, None,
            ),
            (
                "capability_status_only",
                {"authenticated": True, "permissions": ["MULTI_SIMULATION"]},
                {"capability_status": "AVAILABLE", "simulation_type_choices": ["REGULAR"]},
                None, None,
            ),
            (
                "biometric_auth_reason_passthrough",
                {
                    "authenticated": False, "permissions": [],
                    "reason": "BIOMETRIC_AUTH_REQUIRED",
                },
                {"status": "UNKNOWN", "simulation_type_choices": []},
                "BIOMETRIC_AUTH_REQUIRED", "BIOMETRIC_AUTH_REQUIRED",
            ),
        )
        for name, authentication, capability, single_reason, multi_reason in cases:
            with self.subTest(case=name):
                client = SimpleNamespace(
                    get_authentication_status=lambda value=authentication: value,
                    get_simulation_capability=lambda value=capability: value,
                )
                modes = get_simulation_modes(client=client)
                for mode_name, expected_reason in (
                    ("single", single_reason), ("multi", multi_reason)
                ):
                    mode = modes[mode_name]
                    self.assertEqual(mode["available"], expected_reason is None)
                    if expected_reason is None:
                        self.assertNotIn("reason", mode)
                    else:
                        self.assertEqual(mode["reason"], expected_reason)

    def test_settings_validation_facade_uses_gateway_owner(self):
        client = SimpleNamespace(region="USA", universe="TOP3000", instrument_type="EQUITY")
        capability = {
            "status": "AVAILABLE",
            "required_settings": [],
            "settings": {},
        }
        with mock.patch.object(
            SimulationGateway, "validate_simulation_settings",
            return_value={
                "valid": True, "status": "VALID", "source": "GATEWAY",
                "validation_source": "GATEWAY", "capability_status": "AVAILABLE",
                "evidence_status": "INCONCLUSIVE",
                "settings": {"delay": 1}, "errors": [],
            },
        ) as validator:
            result = validate_simulation_settings(
                {"delay": 1}, client=client, capability=capability
            )
        self.assertEqual(result["source"], "GATEWAY")
        validator.assert_called_once_with(
            {"delay": 1}, client=client, capability=capability
        )

    def test_live_preflight_reuses_mode_reason_projection(self):
        client = SimpleNamespace(
            get_authentication_status=lambda: {
                "authenticated": False,
                "permissions": [],
                "reason": "BIOMETRIC_AUTH_REQUIRED",
            },
            get_simulation_capability=lambda: {
                "capability_status": "UNKNOWN",
                "simulation_type_choices": [],
            },
        )
        with tempfile.TemporaryDirectory() as state, mock.patch(
            "wqb_agent.research_api.simulation_quota",
            return_value={"status": "UNKNOWN"},
        ):
            result = get_live_preflight(client=client, state_dir=state)
        self.assertEqual(
            result["simulation_modes"]["single"]["reason"],
            "BIOMETRIC_AUTH_REQUIRED",
        )
        self.assertEqual(
            result["simulation_modes"]["multi"]["reason"],
            "BIOMETRIC_AUTH_REQUIRED",
        )
        self.assertFalse(result["network_write"])

    def test_platform_advertised_non_regular_type_is_not_writer_available(self):
        client = SimpleNamespace(
            get_authentication_status=lambda: {
                "authenticated": True, "user_id": "user-1",
                "permissions": ["MULTI_SIMULATION", "REGION_AGNOSTIC"],
            },
            get_simulation_capability=lambda: {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "source": "BRAIN_LIVE",
                "simulation_type_choices": [
                    "REGULAR", "SUPER", "REGION_AGNOSTIC",
                ],
                "settings": {}, "required_fields": [], "required_settings": [],
            },
        )

        modes = get_simulation_modes(client=client)

        self.assertFalse(modes["region_agnostic"]["available"])
        self.assertEqual(modes["region_agnostic"]["status"], "UNAVAILABLE")
        self.assertEqual(modes["region_agnostic"]["reason"], "WRITER_UNSUPPORTED")

    def test_multi_mode_requires_live_permission(self):
        client = SimpleNamespace(
            get_authentication_status=lambda: {
                "authenticated": True, "user_id": "user-1", "permissions": [],
            },
            get_simulation_capability=lambda: {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "source": "BRAIN_LIVE", "simulation_type_choices": ["REGULAR", "SUPER"],
                "settings": {}, "required_fields": [], "required_settings": [],
            },
        )
        modes = get_simulation_modes(client=client)
        self.assertFalse(modes["multi"]["available"])
        self.assertEqual(modes["multi"]["reason"], "PERMISSION_UNAVAILABLE")
        self.assertEqual(modes["multi"]["source"], "BRAIN_LIVE")

    def test_settings_validation_prefers_live_options_and_reports_local_only_fallback(self):
        client = SimpleNamespace(
            region="USA", universe="TOP3000", instrument_type="EQUITY",
            get_simulation_capability=lambda: {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "settings": {
                    "region": {"allowed_values": ["USA"]},
                    "neutralization": {"allowed_values": ["SUBINDUSTRY"]},
                },
                "required_fields": [], "required_settings": ["region"],
            },
        )
        valid = validate_simulation_settings(
            {"region": "USA", "neutralization": "SUBINDUSTRY", "delay": 3,
             "truncation": 0.1, "visualization": False}, client=client,
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["validation_source"], "LIVE_OPTIONS")
        invalid = validate_simulation_settings(
            {"region": "GLB"}, client=client,
        )
        self.assertFalse(invalid["valid"])
        self.assertIn("region is not allowed", " ".join(invalid["errors"]))

        offline = validate_simulation_settings(
            {"region": "USA"}, client=SimpleNamespace(
                region="USA", universe="TOP3000", instrument_type="EQUITY",
                get_simulation_capability=lambda: {"status": "UNKNOWN"},
            ),
        )
        self.assertTrue(offline["valid"])
        self.assertEqual(offline["capability_status"], "UNKNOWN")
        self.assertEqual(offline["validation_source"], "LOCAL_ONLY")

    def test_live_preflight_is_read_only_and_contains_pending_guards(self):
        client = SimpleNamespace(
            instrument_type="EQUITY", region="USA", universe="TOP3000", delay=1,
            get_authentication_status=lambda: {
                "authenticated": True, "user_id": "user-1",
                "permissions": ["MULTI_SIMULATION"],
            },
            get_simulation_capability=lambda: {
                "status": "AVAILABLE", "capability_status": "AVAILABLE",
                "source": "BRAIN_LIVE", "simulation_type_choices": ["REGULAR", "SUPER"],
                "settings": {}, "required_fields": [], "required_settings": [],
            },
            get_all_user_alphas=lambda **_kwargs: [],
        )
        with tempfile.TemporaryDirectory() as state:
            result = get_live_preflight(
                client=client,
                config={"simulation": {}, "runtime": {}},
                state_dir=state,
            )
        self.assertFalse(result["network_write"])
        self.assertEqual(result["authentication"]["user_id"], "user-1")
        self.assertTrue(result["simulation_modes"]["multi"]["available"])
        self.assertEqual(result["multi_child_range"], "2..10")
        self.assertIn("pending_execution_count", result)

    def test_pending_execution_read_uses_configured_state_directory(self):
        with mock.patch("wqb_agent.research_api.ExecutionGuard") as guard_factory:
            guard_factory.return_value.entries.return_value = []
            result = get_pending_executions(
                config={"simulation": {}, "runtime": {"state_dir": "synthetic-state"}},
            )
        self.assertEqual(result, {"entries": []})
        guard_factory.assert_called_once_with("synthetic-state", reconcile=False)

    def test_single_alias_and_multi_facade_delegate_to_gateway(self):
        spec = SimulationSpec("rank(close)")
        with mock.patch("wqb_agent.research_api._simulation_gateway") as factory:
            gateway = factory.return_value
            gateway.simulate.return_value = {"status": "DONE"}
            self.assertEqual(simulate(spec), {"status": "DONE"})
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

    def test_named_alpha_reads_are_direct_read_only_facade_calls(self):
        client = mock.Mock()
        client.get_aggregates.return_value = {"kind": "aggregates"}
        client.get_pnl.return_value = {"kind": "pnl"}
        client.get_self_correlation.return_value = {"kind": "correlation"}

        self.assertEqual(get_alpha_aggregates("a1", client=client), {"kind": "aggregates"})
        self.assertEqual(get_alpha_pnl("a1", client=client), {"kind": "pnl"})
        self.assertEqual(
            get_alpha_self_correlation("a1", client=client), {"kind": "correlation"}
        )
        client.get_aggregates.assert_called_once_with("a1")
        client.get_pnl.assert_called_once_with("a1")
        client.get_self_correlation.assert_called_once_with("a1")
        client.get_alpha.assert_not_called()
        client.list_alpha_recordsets.assert_not_called()

    def test_alpha_evidence_facade_keeps_cheap_first_envelope(self):
        client = mock.Mock()
        client.get_alpha.return_value = {"id": "a1", "is": {"sharpe": 1.2}}

        result = get_alpha_evidence("a1", client=client)

        self.assertEqual(
            set(result),
            {
                "alpha_id", "source", "fetched_at", "age_sec", "alpha",
                "aggregates", "pnl", "self_correlation", "recordsets",
                "status", "availability",
            },
        )
        self.assertEqual(result["source"], "LIVE")
        self.assertEqual(result["alpha"], {"id": "a1", "is": {"sharpe": 1.2}})
        self.assertIsNone(result["aggregates"])
        self.assertIsNone(result["pnl"])
        self.assertIsNone(result["self_correlation"])
        self.assertEqual(result["recordsets"], {})
        client.get_alpha.assert_called_once_with("a1")
        client.list_alpha_recordsets.assert_not_called()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_alpha_metrics_use_only_detail_is_projection(self):
        client = mock.Mock()
        client.get_alpha.return_value = {"id": "a1", "is": {"sharpe": 1.2}}

        self.assertEqual(get_alpha_metrics("a1", client=client), {"sharpe": 1.2})
        client.get_alpha.assert_called_once_with("a1")
        client.list_alpha_recordsets.assert_not_called()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_alpha_evidence_requires_live_reads(self):
        client = mock.Mock()

        with self.assertRaisesRegex(ValueError, "LIVE_EVIDENCE_REQUIRED"):
            get_alpha_evidence("a1", client=client, live=False)

        client.get_alpha.assert_not_called()

    def test_compare_alphas_keeps_public_envelope(self):
        client = mock.Mock()
        client.get_alpha.side_effect = [
            {"id": "a1", "is": {"sharpe": 1.2}},
            {"id": "a2", "is": {"sharpe": 0.8}},
        ]

        result = __import__("wqb_agent.research_api", fromlist=["compare_alphas"]).compare_alphas(
            ["a1", "a2"], client=client
        )

        self.assertEqual(
            {key: result[key] for key in ("source", "status", "evidence_status")},
            {"source": "LIVE", "status": "AVAILABLE", "evidence_status": "AVAILABLE"},
        )
        self.assertEqual([item["alpha_id"] for item in result["alphas"]], ["a1", "a2"])
        client.list_alpha_recordsets.assert_not_called()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

    def test_selected_recordsets_are_exposed_by_public_facade(self):
        client = mock.Mock()
        client.get_alpha.return_value = {"id": "a1"}
        client.list_alpha_recordsets.return_value = [
            {"name": "coverage", "title": "Coverage"},
        ]
        client.get_recordset.return_value = {
            "schema": {"properties": {"coverage": {}}},
            "records": [[0.5]],
        }

        result = get_alpha_recordsets("a1", ["coverage"], client=client)

        self.assertEqual(result["coverage"]["rows"], [{"coverage": 0.5}])
        client.get_alpha.assert_called_once_with("a1")
        client.list_alpha_recordsets.assert_called_once_with("a1")
        client.get_recordset.assert_called_once()
        client.get_aggregates.assert_not_called()
        client.get_pnl.assert_not_called()
        client.get_self_correlation.assert_not_called()

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
