import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.helpers import utc_timestamp
from wqb_agent.agent import Agent
from wqb_agent.alpha_factory import (
    AlphaFactory,
    AlphaTemplate,
    AlphaTemplateRegistry,
)
from wqb_agent.alpha_feed_cache import WeeklyAlphaFeedCache
from wqb_agent.alpha_feed_workflow import AlphaFeedWorkflow
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.client import WQBQueryTooBroadError
from wqb_agent.daily_cache import DailyResearchCache
from wqb_agent.diversity import (
    derive_budget_priority,
    diversity_audit,
    select_budget_candidates,
    semantic_mechanism_key,
)
from wqb_agent.factory_quota import carry_forward_quota
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.proposal_contract import factory_batch_stats, validate_factory_batch
from wqb_agent.research_guard import parameter_only_change_reason
from wqb_agent.state import Experiment
from wqb_agent.weekly_quota import QuotaExceeded, WeeklySimulationQuota


class TestDailyResearchCache(unittest.TestCase):
    def test_uses_new_york_calendar_day_and_drops_previous_bucket(self):
        now = [utc_timestamp(dt.datetime(2026, 9, 9, 3, 59))]
        cache = DailyResearchCache(clock=lambda: now[0])

        cache.put_simulations([{"alpha_id": "a1", "sharpe": 1.2}])
        self.assertEqual(cache.local_date, "2026-09-08")
        self.assertEqual(cache.simulations(), [{"alpha_id": "a1", "sharpe": 1.2}])

        # 04:00 UTC is midnight in New York after the DST transition period.
        now[0] = utc_timestamp(dt.datetime(2026, 9, 9, 4, 0))
        self.assertEqual(cache.local_date, "2026-09-09")
        self.assertEqual(cache.simulations(), [])
        self.assertEqual(cache.submitted_alphas(), [])
        self.assertEqual(cache.colors(), [])

    def test_cache_is_memory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DailyResearchCache(clock=lambda: utc_timestamp(
                dt.datetime(2026, 9, 9, 12, 0)
            ))
            cache.put_submitted_alphas([{"alpha_id": "a1"}])
            cache.put_colors([{"alpha_id": "a1", "classification": "BLUE"}])
            self.assertFalse(os.listdir(tmp))
            self.assertEqual(cache.snapshot()["local_date"], "2026-09-09")

    def test_same_day_batches_accumulate_without_duplicate_alpha_ids(self):
        cache = DailyResearchCache(clock=lambda: utc_timestamp(
            dt.datetime(2026, 9, 9, 12, 0)
        ))
        cache.put_simulations([{"alpha_id": "a1", "sharpe": 1.0}])
        cache.put_simulations([
            {"alpha_id": "a1", "sharpe": 1.1},
            {"alpha_id": "a2", "sharpe": 0.8},
        ])
        self.assertEqual(cache.simulations(), [
            {"alpha_id": "a1", "sharpe": 1.1},
            {"alpha_id": "a2", "sharpe": 0.8},
        ])


class TestWeeklyAlphaFeedCache(unittest.TestCase):
    def test_persists_time_buckets_and_prunes_oldest_simulations(self):
        now = [utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "weekly.json")
            cache = WeeklyAlphaFeedCache(
                path, clock=lambda: now[0], weekly_simulation_cap=3
            )
            result = cache.refresh({
                "2026-09-07": {
                    "simulations": [{"alpha_id": "old-1"}],
                    "submitted_alphas": [],
                },
                "2026-09-08": {
                    "simulations": [{"alpha_id": "old-2"}],
                    "submitted_alphas": [{"alpha_id": "submitted-1"}],
                },
                "2026-09-09": {
                    "simulations": [
                        {"alpha_id": "today-1"},
                        {"alpha_id": "today-2"},
                    ],
                    "submitted_alphas": [],
                },
            })

            self.assertEqual(result["simulation_count"], 3)
            self.assertEqual(result["pruned_simulation_count"], 1)
            self.assertEqual(result["local_date"], "2026-09-09")
            self.assertTrue(result["updated_at"])
            self.assertTrue(result["expires_at"])
            payload = cache.load()
            self.assertEqual(
                set(payload["days"]), {"2026-09-08", "2026-09-09"}
            )
            self.assertEqual(
                [row["alpha_id"] for row in payload["days"]["2026-09-09"]["simulations"]],
                ["today-1", "today-2"],
            )

    def test_cross_week_load_removes_expired_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "weekly.json")
            first = WeeklyAlphaFeedCache(
                path,
                clock=lambda: utc_timestamp(dt.datetime(2026, 9, 9, 12, 0)),
            )
            first.refresh({"2026-09-09": {"simulations": [], "submitted_alphas": []}})
            next_week = WeeklyAlphaFeedCache(
                path,
                clock=lambda: utc_timestamp(dt.datetime(2026, 9, 14, 12, 0)),
            )

            self.assertIsNone(next_week.load())
            self.assertFalse(os.path.exists(path))


class TestWeeklySimulationQuota(unittest.TestCase):
    def test_daily_refresh_preserves_weekly_usage(self):
        now = [utc_timestamp(dt.datetime(2026, 9, 8, 12, 0))]
        quota = WeeklySimulationQuota(
            weekly_cap=11200, daily_cap=1600, clock=lambda: now[0]
        )
        state = quota.initial_state()
        state = quota.reserve(state, 1600)
        self.assertEqual(quota.remaining(state), 0)
        with self.assertRaises(QuotaExceeded):
            quota.reserve(state, 1)

        now[0] = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
        state = quota.normalize_state(state)
        self.assertEqual(state["daily_reserved"], 0)
        self.assertEqual(state["weekly_reserved"], 1600)
        self.assertEqual(quota.remaining(state), 1600)

    def test_week_refresh_clears_weekly_and_daily_usage(self):
        now = [utc_timestamp(dt.datetime(2026, 9, 13, 12, 0))]
        quota = WeeklySimulationQuota(
            weekly_cap=11200, daily_cap=1600, clock=lambda: now[0]
        )
        state = quota.reserve(quota.initial_state(), 1600)
        now[0] = utc_timestamp(dt.datetime(2026, 9, 14, 12, 0))
        state = quota.normalize_state(state)
        self.assertEqual(state["daily_reserved"], 0)
        self.assertEqual(state["weekly_reserved"], 0)
        self.assertEqual(quota.remaining(state), 1600)

    def test_malformed_state_fails_closed(self):
        quota = WeeklySimulationQuota(weekly_cap=11200, daily_cap=1600)
        with self.assertRaises(ValueError):
            quota.normalize_state({"daily_reserved": "not-a-number"})

    def test_state_contains_only_quota_metadata(self):
        quota = WeeklySimulationQuota(weekly_cap=11200, daily_cap=1600)
        state = quota.initial_state()
        self.assertEqual(
            set(state),
            {
                "schema_version", "timezone", "local_date", "week_start",
                "daily_cap", "weekly_cap", "daily_reserved", "weekly_reserved",
            },
        )


class TestAgentColorAndOptimizerTriggers(unittest.TestCase):
    def test_remote_alpha_feed_refreshes_week_buckets_and_today_count_together(self):
        class FeedClient:
            def __init__(self):
                self.calls = []

            def get_all_user_alphas(
                self, *, status, limit, max_pages=None, max_results=1000, **kwargs
            ):
                self.calls.append((status, limit, max_pages))
                if status == "SUBMITTED":
                    return [{
                        "id": "submitted-1",
                        "status": "SUBMITTED",
                        "dateSubmitted": "2026-09-08T20:00:00-04:00",
                    }]
                return [
                    {
                        "id": "today-1",
                        "status": "UNSUBMITTED",
                        "dateCreated": "2026-09-09T08:00:00-04:00",
                    },
                    {
                        "id": "old-1",
                        "status": "UNSUBMITTED",
                        "dateCreated": "2026-09-08T08:00:00-04:00",
                    },
                ]

        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent.__new__(Agent)
            agent.client = FeedClient()
            agent.daily_cache = DailyResearchCache(clock=lambda: utc_timestamp(
                dt.datetime(2026, 9, 9, 12, 0)
            ))
            agent.alpha_feed_cache = WeeklyAlphaFeedCache(
                os.path.join(tmp, "weekly.json"),
                clock=lambda: utc_timestamp(dt.datetime(2026, 9, 9, 12, 0)),
            )
            agent.alpha_feed_workflow = AlphaFeedWorkflow(
                alpha_reader=agent.client.get_all_user_alphas,
                daily_cache=agent.daily_cache,
                weekly_cache=agent.alpha_feed_cache,
            )
            snapshot = agent.refresh_remote_alpha_feed(limit=20)

            self.assertEqual(snapshot["submitted_count"], 1)
            self.assertEqual(snapshot["today_simulated_count"], 1)
            self.assertEqual(snapshot["weekly_simulated_count"], 2)
            self.assertEqual(
                agent.daily_cache.submitted_alphas()[0]["alpha_id"],
                "submitted-1",
            )
            self.assertEqual(agent.daily_cache.simulations()[0]["alpha_id"], "today-1")
            self.assertEqual(agent.client.calls, [
                ("SUBMITTED", 20, None), ("UNSUBMITTED", 20, None),
            ])
            self.assertEqual(
                agent.alpha_feed_cache.load()["days"]["2026-09-09"]["simulations"][0]["alpha_id"],
                "today-1",
            )

    def test_remote_alpha_feed_splits_a_platform_broad_window(self):
        class BroadClient:
            def __init__(self):
                self.calls = []
                self.broad = {"SUBMITTED": True, "UNSUBMITTED": True}

            def get_all_user_alphas(self, **kwargs):
                self.calls.append(kwargs)
                status = kwargs["status"]
                if self.broad[status]:
                    self.broad[status] = False
                    raise WQBQueryTooBroadError("too broad")
                if status == "SUBMITTED":
                    return [{
                        "id": "submitted-1",
                        "status": status,
                        "dateSubmitted": "2026-09-09T08:00:00-04:00",
                    }]
                return [{
                    "id": "today-1",
                    "status": status,
                    "dateCreated": "2026-09-09T08:00:00-04:00",
                }]

        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent.__new__(Agent)
            agent.client = BroadClient()
            agent.daily_cache = DailyResearchCache(clock=lambda: utc_timestamp(
                dt.datetime(2026, 9, 9, 12, 0)
            ))
            agent.alpha_feed_cache = WeeklyAlphaFeedCache(
                os.path.join(tmp, "weekly.json"),
                clock=lambda: utc_timestamp(dt.datetime(2026, 9, 9, 12, 0)),
            )
            agent.alpha_feed_workflow = AlphaFeedWorkflow(
                alpha_reader=agent.client.get_all_user_alphas,
                daily_cache=agent.daily_cache,
                weekly_cache=agent.alpha_feed_cache,
            )

            snapshot = agent.refresh_remote_alpha_feed(limit=100)

            self.assertEqual(snapshot["today_simulated_count"], 1)
            self.assertEqual(len(agent.client.calls), 6)
            self.assertTrue(all(call["max_results"] == 1000 for call in agent.client.calls))

    def test_settled_result_updates_color_cache_immediately(self):
        agent = Agent.__new__(Agent)
        agent.daily_cache = DailyResearchCache(clock=lambda: utc_timestamp(
            dt.datetime(2026, 9, 9, 12, 0)
        ))
        experiment = {
            "alpha_id": "alpha-1",
            "status": "DONE",
            "economic_mechanism": "已验证的事件后价格漂移机制",
            "falsification": "若跨年份稳定性失败则停止该方向",
            "health": {"ok": True},
            "quality_label": "SUCCESS",
            "metrics": {
                "sharpe": 1.1, "fitness": 0.8, "turnover": 0.2,
                "returns": 0.1, "drawdown": 0.05, "margin": 0.03,
                "checks": [],
            },
        }
        agent._cache_color_result(experiment)
        self.assertEqual(agent.daily_cache.colors(), [{
            "alpha_id": "alpha-1", "classification": "BLUE",
        }])

    def test_optimizer_gate_reports_missing_agent_child_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Agent(object(), {"simulation": {}, "agent": {"state_dir": tmp}})
            parent = {
                "status": "DONE",
                "expression": "rank(field)",
                "fields_used": ["field"],
                "datasets": ["fundamental6"],
                "metrics": {"sharpe": 1.1, "fitness": 0.8, "turnover": 0.2},
                "health": {"ok": True},
                "field_understanding": {"field": "已核验字段"},
                "field_analysis": {"field": {"data_type": "MATRIX"}},
                "field_source": {"kind": "brain_api"},
                "field_hypothesis_basis": {"field": {"mechanism": "质量变化"}},
            }
            report = agent.optimizer_gate_report([parent])
        self.assertEqual(report["done_parent_count"], 1)
        self.assertEqual(report["ready_parent_count"], 0)
        self.assertEqual(report["blocked_reasons"]["PARENT_INCREMENTAL_EVIDENCE_INSUFFICIENT"], 1)


class TestFactoryRunnerAccounting(unittest.TestCase):
    def test_new_session_carries_forward_same_day_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
            quota = WeeklySimulationQuota(
                weekly_cap=11200, daily_cap=1600, clock=lambda: now
            )
            prior_quota = quota.reserve(quota.initial_state(), 300)
            previous = {
                "schema_version": 1,
                "created_by_version": "test",
                "session_id": "old-session",
                "started_at": now - 100,
                "deadline": now - 1,
                "status": "STOPPED",
                "rounds_completed": 1,
                "simulations_reserved": 300,
                "simulation_cap": 11200,
                "quota": prior_quota,
            }
            with open(
                os.path.join(tmp, "factory_session.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(previous, handle)

            agent = SimpleNamespace(state_dir=tmp, alpha_factory=Mock())
            result = AIFactoryRunner(
                agent,
                factory=agent.alpha_factory,
                clock=lambda: now,
                sleeper=lambda _seconds: None,
            ).run(
                duration_sec=0,
                max_simulations=11200,
                daily_simulation_cap=1600,
                weekly_simulation_cap=11200,
            )

        self.assertNotEqual(result["session_id"], "old-session")
        self.assertEqual(result["quota"]["daily_reserved"], 300)
        self.assertEqual(result["quota"]["weekly_reserved"], 300)

    def test_legacy_aggregate_is_migrated_before_new_session(self):
        now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
        quota = WeeklySimulationQuota(weekly_cap=11200, daily_cap=1600, clock=lambda: now)
        carried = carry_forward_quota(
            {"simulations_reserved": 700}, quota
        )
        self.assertEqual(carried["daily_reserved"], 700)
        self.assertEqual(carried["weekly_reserved"], 700)

    def test_carry_forward_delegates_day_and_week_rollover(self):
        old_clock = utc_timestamp(dt.datetime(2026, 9, 8, 12, 0))
        old_quota = WeeklySimulationQuota(
            weekly_cap=11200, daily_cap=1600, clock=lambda: old_clock
        )
        old_state = old_quota.reserve(old_quota.initial_state(), 700)
        new_day = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
        new_quota = WeeklySimulationQuota(
            weekly_cap=11200, daily_cap=1600, clock=lambda: new_day
        )
        carried = carry_forward_quota({"quota": old_state}, new_quota)
        self.assertEqual(carried["daily_reserved"], 0)
        self.assertEqual(carried["weekly_reserved"], 700)

        new_week = utc_timestamp(dt.datetime(2026, 9, 14, 12, 0))
        rollover_quota = WeeklySimulationQuota(
            weekly_cap=11200, daily_cap=1600, clock=lambda: new_week
        )
        carried = carry_forward_quota({"quota": old_state}, rollover_quota)
        self.assertEqual(carried["daily_reserved"], 0)
        self.assertEqual(carried["weekly_reserved"], 0)

    def test_carry_forward_fails_closed_for_malformed_or_over_cap_state(self):
        now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
        quota = WeeklySimulationQuota(weekly_cap=1000, daily_cap=100, clock=lambda: now)
        with self.assertRaises(ValueError):
            carry_forward_quota(
                {"quota": {"daily_reserved": "bad"}}, quota
            )
        with self.assertRaises(ValueError):
            carry_forward_quota(
                {"simulations_reserved": 1001}, quota
            )

    def test_carry_forward_preserves_usage_when_cap_increases_and_rejects_decrease(self):
        now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
        old_quota = WeeklySimulationQuota(weekly_cap=1000, daily_cap=100, clock=lambda: now)
        old_state = old_quota.reserve(old_quota.initial_state(), 100)
        increased = WeeklySimulationQuota(weekly_cap=2000, daily_cap=200, clock=lambda: now)
        self.assertEqual(
            carry_forward_quota({"quota": old_state}, increased)["weekly_reserved"],
            100,
        )
        decreased = WeeklySimulationQuota(weekly_cap=50, daily_cap=50, clock=lambda: now)
        with self.assertRaises(ValueError):
            carry_forward_quota({"quota": old_state}, decreased)

    def test_budget_cap_restart_remains_blocked_without_new_agent_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
            quota = WeeklySimulationQuota(weekly_cap=100, daily_cap=100, clock=lambda: now)
            previous = {
                "session_id": "old-session",
                "started_at": now - 100,
                "deadline": now - 1,
                "status": "SIMULATION_BUDGET_CAP",
                "rounds_completed": 1,
                "simulations_reserved": 100,
                "simulation_cap": 100,
                "quota": quota.reserve(quota.initial_state(), 100),
            }
            with open(os.path.join(tmp, "factory_session.json"), "w", encoding="utf-8") as handle:
                json.dump(previous, handle)
            agent = SimpleNamespace(
                state_dir=tmp,
                alpha_factory=Mock(),
                next_round_no=Mock(side_effect=AssertionError("suggestion must not run")),
                run_suggestion_round=Mock(side_effect=AssertionError("suggestion must not run")),
                run_proposals=Mock(side_effect=AssertionError("proposals must not run")),
                checkpoints=SimpleNamespace(unfinished_except=lambda _round: None),
            )
            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory, clock=lambda: now
            ).run(
                duration_sec=10,
                max_simulations=100,
                daily_simulation_cap=100,
                weekly_simulation_cap=100,
            )

        self.assertEqual(result["status"], "SIMULATION_BUDGET_CAP")
        self.assertEqual(result["quota"]["daily_reserved"], 100)
        self.assertEqual(result["quota"]["weekly_reserved"], 100)
        agent.next_round_no.assert_not_called()
        agent.run_suggestion_round.assert_not_called()
        agent.run_proposals.assert_not_called()

    def test_malformed_previous_quota_returns_reconcile_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = utc_timestamp(dt.datetime(2026, 9, 9, 12, 0))
            previous = {
                "session_id": "old-session",
                "started_at": now - 100,
                "deadline": now - 1,
                "status": "STOPPED",
                "rounds_completed": 1,
                "simulations_reserved": 10,
                "simulation_cap": 100,
                "quota": {"daily_reserved": "invalid"},
            }
            with open(os.path.join(tmp, "factory_session.json"), "w", encoding="utf-8") as handle:
                json.dump(previous, handle)
            agent = SimpleNamespace(state_dir=tmp, alpha_factory=Mock())
            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory, clock=lambda: now
            ).run(
                duration_sec=0,
                max_simulations=100,
                daily_simulation_cap=100,
                weekly_simulation_cap=100,
            )

        self.assertEqual(result["status"], "RECONCILE_REQUIRED")
        self.assertEqual(result["last_action"], "INVALID_SIMULATION_QUOTA")

    def test_known_expressions_include_completed_checkpoint_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment = Experiment(1, "h", "rank(old_field)", {}, ["old_field"])
            experiment.status = "DONE"
            CheckpointStore(tmp).write(1, {"id": "h"}, [experiment], complete=True)
            agent = SimpleNamespace(
                state_dir=tmp,
                memory=SimpleNamespace(seen_expressions=set()),
                trajectory=SimpleNamespace(experiments=[]),
                checkpoints=CheckpointStore(tmp),
            )
            known = AIFactoryRunner(agent, factory=Mock())._known_expressions()
        self.assertIn("rank(old_field)", known)

    def test_none_without_checkpoint_is_not_counted_as_completed_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            proposals = [
                {
                    "expression": f"rank(field_{index})",
                    "proposal_origin": "factory",
                    "research_layer": "exploration",
                    "research_role": "EXPLORE",
                    "experiment_stage": "BASELINE",
                    "exploration_objective": "signal_discovery",
                    "datasets": ["d1"],
                }
                for index in range(100)
            ]
            agent = SimpleNamespace(
                state_dir=tmp,
                alpha_factory=Mock(),
                factory_config={},
                min_factory_datasets=1,
                min_cross_dataset_pairs=0,
                next_round_no=lambda: 1,
                run_suggestion_round=Mock(return_value={
                    "research_space": {
                        "id": "h1", "statement": "test", "datasets": ["d1"]
                    },
                    "fields": [],
                    "operator_reference": {
                        "operators": ["rank"], "status": "LIVE_VERIFIED",
                        "availability": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
                    },
                    "field_source": {"kind": "brain_api", "snapshot_date": "today"},
                }),
                run_proposals=Mock(return_value=None),
                last_run_stats={
                    "accepted": 0, "rejected": 100, "skipped": 0,
                    "status": "PREFLIGHT_BLOCKED",
                },
                checkpoints=SimpleNamespace(
                    unfinished_except=lambda _round: None,
                    scan=lambda: [],
                ),
                memory=SimpleNamespace(seen_expressions=set()),
                trajectory=SimpleNamespace(experiments=[]),
            )
            agent.alpha_factory.generate_factory_batch.return_value = proposals
            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory, quiet=True,
                clock=lambda: now[0], sleeper=sleep,
            ).run(
                duration_sec=3, idle_sleep_sec=1, max_simulations=11200,
                daily_simulation_cap=1600, weekly_simulation_cap=11200,
            )

        self.assertEqual(result["rounds_completed"], 0)
        self.assertEqual(result["simulations_reserved"], 0)
        self.assertEqual(result["last_action"], "STOP_PREFLIGHT_BLOCKER")
        self.assertEqual(result["last_result"]["status"], "PREFLIGHT_BLOCKED")
        self.assertEqual(result["last_result"]["agent_status"], "PREFLIGHT_BLOCKED")
        self.assertEqual(result["blocker"]["kind"], "PREFLIGHT")
        self.assertNotIn("expression", result["blocker"])
        self.assertEqual(result["quota"]["daily_cap"], 1600)
        self.assertEqual(result["quota"]["weekly_cap"], 11200)
        self.assertEqual(agent.run_proposals.call_count, 2)

    def test_checkpoint_recovery_records_the_checkpoint_round_in_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            experiment = Experiment(7, "h", "rank(field)", {}, ["field"])
            experiment.status = "UNKNOWN"
            experiment.progress_url = "https://api.worldquantbrain.com/simulations/known"
            CheckpointStore(tmp).write(
                7, {"id": "h", "_round": 7}, [experiment], complete=False
            )
            with open(os.path.join(tmp, "proposals.json"), "w", encoding="utf-8") as handle:
                json.dump({"round_no": 7}, handle)

            def recover(_path):
                now[0] = 2.0
                return None

            agent = SimpleNamespace(
                state_dir=tmp,
                alpha_factory=Mock(),
                checkpoints=CheckpointStore(tmp),
                run_proposals=recover,
            )
            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory,
                clock=lambda: now[0], sleeper=lambda _seconds: None,
            ).run(duration_sec=1, max_simulations=10)

        self.assertEqual(result["last_round"], 7)
        self.assertEqual(result["last_action"], "CHECKPOINT_BLOCKED")

    def test_daily_quota_blocks_batch_before_production_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            proposals = [
                {
                    "expression": f"rank(field_{index})",
                    "proposal_origin": "factory",
                    "datasets": ["d1"],
                }
                for index in range(100)
            ]
            agent = SimpleNamespace(
                state_dir=tmp,
                alpha_factory=Mock(),
                factory_config={},
                min_factory_datasets=1,
                min_cross_dataset_pairs=0,
                next_round_no=lambda: 1,
                run_suggestion_round=Mock(return_value={
                    "research_space": {
                        "id": "h1", "statement": "test", "datasets": ["d1"]
                    },
                    "fields": [],
                    "operator_reference": {
                        "operators": ["rank"], "status": "LIVE_VERIFIED",
                        "availability": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
                    },
                    "field_source": {"kind": "brain_api", "snapshot_date": "today"},
                }),
                run_proposals=Mock(),
                checkpoints=SimpleNamespace(
                    unfinished_except=lambda _round: None,
                    scan=lambda: [],
                ),
                memory=SimpleNamespace(seen_expressions=set()),
                trajectory=SimpleNamespace(experiments=[]),
            )
            agent.alpha_factory.generate_factory_batch.return_value = proposals
            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory, quiet=True,
                clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            ).run(
                duration_sec=1, idle_sleep_sec=1, max_simulations=11200,
                daily_simulation_cap=50, weekly_simulation_cap=11200,
            )

        self.assertEqual(result["status"], "SIMULATION_BUDGET_CAP")
        self.assertEqual(result["last_action"], "FACTORY_BATCH_BUDGET_BLOCKED")
        self.assertEqual(result["quota"]["daily_reserved"], 0)
        agent.run_proposals.assert_not_called()


class TestTemplateLiveOperatorEvidence(unittest.TestCase):
    """2026-09-11 平台实测回归：模板不得使用 live 平台确定性拒绝的算子用法。

    证据（USA/EQUITY/TOP3000/Delay1，rounds 1-4 真实 Simulation）：
    - 三参 `normalize(x, true, 0.0)` → `Invalid number of inputs : 2,
      should be exactly 1 input(s)`；
    - 裸位置参数 `gaussian` 驱动（`quantile`/`ts_quantile`）→
      `Attempted to use unknown variable "gaussian"`；
    - `group_rank(rank(group_backfill(...)))` 嵌套 → `Invalid number of
      inputs : 3, should be exactly 2 input(s)`；
    - 双参 `winsorize(x, 4)` / `hump(x, 0.01)` → `Invalid number of inputs
      : 2, should be exactly 1 input(s)`（round_4 实测：此 region 下
      normalize/winsorize/hump 均仅 1 输入）；
    - `ts_regression(..., 20, 0)` 的 lookback=0 → `Got invalid value "0"
      for attribute "lookback"`（round_4 实测，改省略该参数）。
    rounds 1-3 的 63 项 FAILED 与 round 4 的 15 项 FAILED 全部归因于
    上述用法。修复后的 7 个模板必须只使用本平台已实证算子。
    """

    # 156 条 DONE 表达式 + 项目历史 trajectory 使用统计中已被平台接受的算子。
    # winsorize/hump：2026-09-11 平台实测双参形式被拒（exactly 1 input），
    # 单参 winsorize(x)/hump(x) 为平台明确要求的合法形式。
    # ts_regression/ts_step：平台按名接受（round_4 的拒绝发生在属性值层：
    # lookback=0 非法），省略 lookback 的 3 参形式为当前模板用法。
    LIVE_ACCEPTED_OPERATORS = {
        "rank", "winsorize", "hump", "ts_zscore", "ts_delta", "ts_rank",
        "subtract", "ts_backfill", "group_mean", "group_neutralize",
        "ts_regression", "ts_step", "ts_mean", "ts_std_dev", "ts_corr",
        "signed_power", "multiply", "add", "divide", "abs",
    }
    REJECTED_SNIPPETS = (
        "gaussian", "group_rank(", "group_backfill(",
        # 2026-09-11 round_4：ts_regression 的 lookback=0 被平台拒绝
        "ts_step(1), 20, 0",
        # 2026-09-11 round_9：2 参 group_mean(X, G) 被平台拒绝（exactly 3 inputs），
        # 仅 3 参形式 group_mean(X, 1, G) 有 38 次 DONE 实证（r1-r7 group_scaled_mean）
        "group_mean(ts_backfill({p}, 20), {g}))",
    )
    REPAIRED_TEMPLATE_IDS = (
        "toy_scale_surprise",
        "toy_sync_corr",
        "toy_regression_residual",
        "toy_quality_backfill",
        "toy_signed_power_risk",
        "toy_pair_spread",
    )

    def _template_by_id(self, template_id):
        from wqb_agent.alpha_factory import ECONOMIC_TEMPLATES
        for template in ECONOMIC_TEMPLATES:
            if template.template_id == template_id:
                return template
        self.fail(f"template {template_id!r} missing from ECONOMIC_TEMPLATES")

    def _concrete(self, expression):
        return (
            expression
            .replace("{p}", "cashflow_fin")
            .replace("{s}", "cashflow_op")
            .replace("{t}", "cashflow_invst")
            .replace("{g}", "subindustry")
        )

    def test_repaired_templates_use_only_live_accepted_operators(self):
        from wqb_agent.expression import analyze_expression

        for template_id in self.REPAIRED_TEMPLATE_IDS:
            expression = self._concrete(self._template_by_id(template_id).expression)
            parsed = analyze_expression(expression)
            operators = set(parsed.operators)
            rejected = operators - self.LIVE_ACCEPTED_OPERATORS
            self.assertEqual(
                rejected, set(),
                f"{template_id} uses platform-unproven operators "
                f"{sorted(rejected)}: {expression}",
            )

    def test_repaired_templates_avoid_rejected_syntax(self):
        for template_id in self.REPAIRED_TEMPLATE_IDS:
            expression = self._template_by_id(template_id).expression
            for snippet in self.REJECTED_SNIPPETS:
                self.assertNotIn(
                    snippet, expression,
                    f"{template_id} still contains live-rejected usage "
                    f"{snippet!r}: {expression}",
                )
            # 多参 normalize(x, ...) 从未在本平台实证，禁止任何参数形式。
            self.assertNotIn(
                "normalize(", expression,
                f"{template_id} uses unverified normalize: {expression}",
            )

    def _top_level_arity(self, expression, call_name):
        """各 call_name 调用的顶层实参数（深度感知，避免嵌套调用误报）。"""
        arities = []
        needle = f"{call_name}("
        idx = 0
        while True:
            start = expression.find(needle, idx)
            if start == -1:
                break
            depth = 0
            args = 1
            i = start + len(needle) - 1
            while i < len(expression):
                char = expression[i]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif char == "," and depth == 1:
                    args += 1
                i += 1
            arities.append(args)
            idx = start + len(needle)
        return arities

    def test_registry_wide_guard_against_rejected_drivers(self):
        from wqb_agent.alpha_factory import ECONOMIC_TEMPLATES

        for template in ECONOMIC_TEMPLATES:
            expression = template.expression
            for snippet in self.REJECTED_SNIPPETS:
                self.assertNotIn(
                    snippet, expression,
                    f"{template.template_id} reintroduced live-rejected "
                    f"usage {snippet!r}",
                )
            # 2026-09-11 平台实测：此 region 下 normalize、winsorize、hump
            # 均只接受恰好 1 个输入；多参形式一律拒绝（嵌套调用合法）。
            for call in ("normalize", "winsorize", "hump"):
                for arity in self._top_level_arity(expression, call):
                    self.assertEqual(
                        arity, 1,
                        f"{template.template_id} uses multi-arg {call} "
                        f"({arity} inputs): {expression}",
                    )
            # 2026-09-11 round_9 平台实测：此 region 下 group_mean 只接受恰好 3 个
            # 顶层输入（group_mean(X, 1, G)）；2 参形式被拒绝，3 参形式 r1-r7 共 38 次 DONE。
            for arity in self._top_level_arity(expression, "group_mean"):
                self.assertEqual(
                    arity, 3,
                    f"{template.template_id} uses non-3-arg group_mean "
                    f"({arity} inputs): {expression}",
                )
