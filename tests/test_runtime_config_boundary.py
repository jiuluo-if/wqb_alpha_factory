import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import main as main_entry
import wqb_agent.config as config_module
from wqb_agent.agent import Agent
from wqb_agent.audit import audit_state
from wqb_agent.behavior import extract_behavior_series
from wqb_agent.config import (
    AppConfig,
    FactoryConfig,
    RemoteCacheConfig,
    normalize_config,
    parse_config,
)
from wqb_agent.diagnostics import DiagnosticEvent
from wqb_agent.doctor import run_doctor
from wqb_agent.incremental_policy import IncrementalValuePolicy
from wqb_agent.protocol import retry_after_seconds
from wqb_agent.schema import ARTIFACT_SCHEMAS, CURRENT_SCHEMA_VERSION, migrate_artifact
from wqb_agent.state import Experiment
from wqb_agent.trial_ledger import TrialLedger
from wqb_agent.workspace_snapshot import read_workspace_snapshot


class TestRuntimeConfigBoundary(unittest.TestCase):

    def test_remote_cache_retention_is_typed_and_bounded(self):
        typed = parse_config({
            "simulation": {}, "agent": {},
            "remote_cache": {"retention_days": 14},
        })
        self.assertIsInstance(typed.remote_cache, RemoteCacheConfig)
        self.assertEqual(typed.remote_cache.retention_days, 14)
        for value in (0, 91, "bad"):
            with self.assertRaises(ValueError):
                parse_config({
                    "simulation": {}, "agent": {},
                    "remote_cache": {"retention_days": value},
                })

    def test_feed_and_heartbeat_intervals_are_typed_positive_and_bounded(self):
        typed = parse_config({"simulation": {}, "agent": {
            "alpha_feed_refresh_interval_sec": 3600,
            "heartbeat_interval_sec": 10,
        }})
        self.assertEqual(typed.runtime.alpha_feed_refresh_interval_sec, 3600)
        self.assertEqual(typed.runtime.heartbeat_interval_sec, 10)
        with self.assertRaisesRegex(ValueError, "alpha_feed_refresh_interval_sec"):
            parse_config({"simulation": {}, "agent": {"alpha_feed_refresh_interval_sec": 0}})
        with self.assertRaisesRegex(ValueError, "heartbeat_interval_sec"):
            parse_config({"simulation": {}, "agent": {"heartbeat_interval_sec": 301}})

    def test_normalize_config_is_the_only_raw_config_boundary(self):
        raw = {"simulation": {}, "agent": {"max_rounds": 3}}
        typed = normalize_config(raw)
        self.assertIsInstance(typed, AppConfig)
        self.assertIs(normalize_config(typed), typed)

    def test_normalized_config_has_typed_sections_only_and_is_copy_isolated(self):
        raw = {
            "simulation": {"neutralization": "SUBINDUSTRY"},
            "agent": {
                "smoke_dataset": "pv1",
                "quality": {"promising_sharpe": 0.9},
            },
        }
        typed = normalize_config(raw)
        self.assertFalse(hasattr(typed, "agent"))
        self.assertFalse(hasattr(typed, "simulation"))
        self.assertEqual(typed.runtime.smoke_dataset, "pv1")
        self.assertEqual(
            typed.simulation_config.settings["neutralization"], "SUBINDUSTRY"
        )
        raw["agent"]["quality"]["promising_sharpe"] = 99
        raw["simulation"]["neutralization"] = "GLOBAL"
        self.assertEqual(typed.runtime.quality["promising_sharpe"], 0.9)
        self.assertEqual(
            typed.simulation_config.settings["neutralization"], "SUBINDUSTRY"
        )

    def test_main_state_dir_override_reports_config_error_before_agent_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "invalid.json")
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump({"simulation": {}}, handle)
            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "main.py", "--doctor", "--offline",
                    "--config", config_path, "--state-dir", tmp,
                ],
            ), redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main_entry.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("config.agent", output.getvalue())

    def test_cli_state_dir_override_is_typed_and_does_not_mutate_original(self):
        typed = normalize_config({
            "simulation": {},
            "agent": {"state_dir": "original-state"},
        })
        apply_override = getattr(config_module, "apply_cli_overrides", None)
        self.assertIsNotNone(apply_override)
        overridden = apply_override(typed, state_dir="override-state")
        self.assertIsInstance(overridden, AppConfig)
        self.assertEqual(overridden.runtime.state_dir, "override-state")
        self.assertEqual(typed.runtime.state_dir, "original-state")
        self.assertFalse(hasattr(typed, "agent"))

    def test_runtime_scalar_max_concurrent_sims_must_be_positive(self):
        with self.assertRaisesRegex(
            ValueError, "config.agent.max_concurrent_sims"
        ):
            parse_config({
                "simulation": {},
                "agent": {"max_concurrent_sims": 0},
            })

    def test_runtime_scalar_poll_timeout_rejects_negative_values(self):
        with self.assertRaisesRegex(
            ValueError, "config.agent.poll_timeout_sec"
        ):
            parse_config({
                "simulation": {},
                "agent": {"poll_timeout_sec": -1},
            })

    def test_runtime_scalar_poll_timeout_rejects_nan(self):
        with self.assertRaisesRegex(
            ValueError, "config.agent.poll_timeout_sec"
        ):
            parse_config({
                "simulation": {},
                "agent": {"poll_timeout_sec": float("nan")},
            })

    def test_max_proposals_per_round_rejects_values_above_hard_limit(self):
        with self.assertRaisesRegex(
            ValueError, "config.agent.max_proposals_per_round"
        ):
            parse_config({
                "simulation": {},
                "agent": {"max_proposals_per_round": 101},
            })

    def test_config_example_remains_valid_with_factory_quotas(self):
        with open("config.example.json", encoding="utf-8") as handle:
            config = normalize_config(json.load(handle))
        self.assertEqual(config.factory.daily_simulation_cap, 1600)
        self.assertEqual(config.factory.weekly_simulation_cap, 11200)
        self.assertEqual(config.runtime.max_proposals_per_round, 100)

    def test_default_typed_config_is_runtime_ready(self):
        typed = normalize_config(AppConfig())
        self.assertEqual(typed.simulation_config.settings["neutralization"], "SUBINDUSTRY")
        self.assertEqual(typed.runtime.memory["max_lineages"], 256)
        self.assertEqual(typed.runtime.field_selection["mode"], "semantic_random")
        self.assertEqual(typed.runtime.search_policy["max_pending_per_arm"], 1)
        self.assertEqual(typed.runtime.quality["promising_sharpe"], 0.9)
        self.assertEqual(typed.runtime.yearly_policy["min_years"], 2)

    def test_config_boolean_strings_are_strict_and_fail_closed(self):
        config = parse_config({
            "simulation": {},
            "agent": {
                "research_integrity": "false",
                "search_policy": {"enabled": "false"},
            },
        })
        self.assertFalse(config.runtime.research_integrity)
        self.assertFalse(config.search.enabled)
        with self.assertRaises(ValueError):
            parse_config({
                "simulation": {},
                "agent": {"research_integrity": "maybe"},
            })

    def test_parse_config_keeps_typed_factory_and_nested_runtime_models(self):
        config = parse_config({"simulation": {}, "agent": {
            "factory": {"max_simulations": 12, "max_runtime_sec": 99},
            "search_policy": {"max_simulations": 12},
            "max_rounds": 8,
        }})
        self.assertIsInstance(config, AppConfig)
        self.assertIsInstance(config.factory, FactoryConfig)
        self.assertEqual(config.factory.max_simulations, 12)
        self.assertEqual(config.factory.max_runtime_sec, 99)
        self.assertEqual(config.factory.daily_simulation_cap, 12)
        self.assertEqual(config.factory.weekly_simulation_cap, 12)
        self.assertEqual(config.runtime.max_rounds, 8)

    def test_factory_partial_operator_policy_defaults_on_and_supports_kill_switch(self):
        self.assertTrue(parse_config({"simulation": {}, "agent": {}}).factory.include_partial_operator_branches)
        disabled = parse_config({"simulation": {}, "agent": {
            "factory": {"include_partial_operator_branches": False},
        }})
        self.assertFalse(disabled.factory.include_partial_operator_branches)
        self.assertFalse(disabled.runtime.factory["include_partial_operator_branches"])

    def test_factory_weekly_and_daily_caps_are_typed_and_validated(self):
        config = parse_config({"simulation": {}, "agent": {
            "factory": {
                "max_simulations": 300,
                "daily_simulation_cap": 1600,
                "weekly_simulation_cap": 11200,
            },
            "search_policy": {"max_simulations": 100},
        }})
        self.assertEqual(config.factory.max_simulations, 11200)
        self.assertEqual(config.factory.daily_simulation_cap, 1600)
        self.assertEqual(config.factory.weekly_simulation_cap, 11200)
        self.assertEqual(config.runtime.factory["max_simulations"], 300)
        with self.assertRaises(ValueError):
            parse_config({"simulation": {}, "agent": {
                "factory": {
                    "daily_simulation_cap": 1601,
                    "weekly_simulation_cap": 1600,
                },
            }})

    def test_factory_retry_governance_parameters_are_typed_and_validated(self):
        config = parse_config({"simulation": {}, "agent": {"factory": {
            "max_route_attempts": 0,
            "max_no_gain_attempts": 1,
            "blocker_recheck_sec": 12.5,
        }}})
        self.assertEqual(config.runtime.factory["max_route_attempts"], 0)
        self.assertEqual(config.runtime.factory["max_no_gain_attempts"], 1)
        self.assertEqual(config.runtime.factory["blocker_recheck_sec"], 12.5)
        for key, value in (("max_route_attempts", -1), ("max_no_gain_attempts", 0),
                           ("blocker_recheck_sec", "NaN"), ("blocker_recheck_sec", -1)):
            with self.assertRaises(ValueError):
                parse_config({"simulation": {}, "agent": {"factory": {key: value}}})

    def test_agent_typed_config_does_not_round_trip_through_legacy_dict(self):
        typed = parse_config({"simulation": {"neutralization": "SUBINDUSTRY"}, "agent": {}})
        agent = Agent(object(), typed)
        self.assertEqual(agent.simulation_settings["neutralization"], "SUBINDUSTRY")

    def test_invalid_config_fails_closed(self):
        with self.assertRaises(ValueError):
            parse_config({"simulation": {}, "agent": {"incremental_value": {"mode": "unknown"}}})

    def test_discovery_plus_validation_budget_cannot_exceed_factory_cap(self):
        with self.assertRaises(ValueError):
            parse_config({"simulation": {}, "agent": {
                "factory": {"max_simulations": 5},
                "search_policy": {"max_simulations": 4, "validation_max_simulations": 2},
            }})

    def test_research_allocation_is_typed(self):
        config = parse_config({"simulation": {}, "agent": {
            "research_allocation": {
                "max_simulations": 8,
                "maximum": {"EXPLOIT": 2, "VALIDATION": 3},
            },
            "search_policy": {"max_simulations": 8},
            "factory": {"max_simulations": 10},
        }})
        self.assertEqual(config.research_allocation.max_simulations, 8)
        self.assertEqual(config.research_allocation.maximum["VALIDATION"], 3)

    def test_agent_runtime_values_are_typed_once_and_consumed_by_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = {
                "simulation": {"neutralization": "SUBINDUSTRY"},
                "agent": {
                    "state_dir": tmp,
                    "max_rounds": 7,
                    "candidates_per_round": 11,
                    "max_concurrent_sims": 2,
                    "poll_timeout_sec": 123,
                    "trajectory_window": 17,
                    "fields_cache_ttl_sec": 456,
                },
            }
            typed = parse_config(raw)
            self.assertEqual(typed.runtime.state_dir, tmp)
            self.assertEqual(typed.runtime.max_rounds, 7)
            self.assertEqual(typed.runtime.poll_timeout_sec, 123)
            agent = Agent(object(), typed)
            self.assertEqual(agent.state_dir, tmp)
            self.assertEqual(agent.max_rounds, 7)
            self.assertEqual(agent.candidates_per_round, 11)
            self.assertEqual(agent.simulator.poll_timeout_sec, 123)
            self.assertEqual(agent.trajectory.max_len, 17)

    def test_runtime_component_defaults_are_resolved_at_config_boundary(self):
        config = normalize_config({"simulation": {}, "agent": {}})
        runtime = config.runtime
        self.assertEqual(config.simulation_config.settings["neutralization"], "SUBINDUSTRY")
        self.assertEqual(
            {key: runtime.memory[key] for key in (
                "max_lessons", "max_avoid", "max_next", "max_hypotheses",
                "max_short_term", "short_term_window", "promote_hits",
                "max_garbage", "garbage_max_age_rounds", "next_max_age_rounds",
                "max_lineages", "max_seen_expressions", "max_used_hypotheses",
            )},
            {
                "max_lessons": 20, "max_avoid": 30, "max_next": 15,
                "max_hypotheses": 12, "max_short_term": 30,
                "short_term_window": 5, "promote_hits": 2,
                "max_garbage": 200, "garbage_max_age_rounds": 60,
                "next_max_age_rounds": 20, "max_lineages": 256,
                "max_seen_expressions": 4096, "max_used_hypotheses": 256,
            },
        )
        self.assertEqual(
            {key: runtime.field_selection[key] for key in (
                "mode", "random_fraction", "random_seed",
            )},
            {"mode": "semantic_random", "random_fraction": 0.35, "random_seed": "newwqb"},
        )
        self.assertEqual(runtime.search_policy["max_pending_per_arm"], 1)
        self.assertEqual(runtime.search_policy["ucb_exploration"], 1.0)
        self.assertEqual(runtime.quality["promising_sharpe"], 0.9)
        self.assertEqual(runtime.quality["promising_fitness"], 0.6)
        self.assertEqual(runtime.yearly_policy["min_years"], 2)
