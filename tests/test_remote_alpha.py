import json
import os
import tempfile
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest import mock

from tests.test_targeted_ci import critical_contract
from wqb_agent import research_api
from wqb_agent.alpha_feed_cache import (
    TEMP_RESOURCE_TTL_SEC,
    RemoteAlphaCache,
    refresh_due,
)
from wqb_agent.alpha_grouping import structural_fingerprint, variant_family_fingerprint
from wqb_agent.client import WQBQueryTooBroadError
from wqb_agent.query_errors import QueryTooBroadError
from wqb_agent.remote_alpha_repository import RemoteAlphaRepository
from wqb_agent.remote_colors import preview_remote_colors, sync_remote_colors

FIXED_NOW = 1789560000.0


def _make_cache(path, *, retention_days=7):
    cache = RemoteAlphaCache(
        path, clock=lambda: FIXED_NOW, retention_days=retention_days
    )
    cache.refresh({
        cache.local_date: {
            "simulations": [{"alpha_id": "synthetic-simulation"}],
            "submitted_alphas": [{"alpha_id": "synthetic-submitted"}],
        }
    })
    return cache


def _bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _rewrite_payload(path, **updates):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.update(updates)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)


class TestAlphaFeedCache(unittest.TestCase):
    @critical_contract("cache.refresh_due_rejects_invalid_time")
    def test_refresh_due_fails_safe_on_invalid_timestamps(self):
        self.assertTrue(refresh_due("bad", 0))
        self.assertTrue(refresh_due(10, 0, interval_sec=10))
        self.assertFalse(refresh_due(9, 0, interval_sec=10))

    @critical_contract("cache.read_preserves_bytes_and_freshness")
    def test_matching_read_returns_payload_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            before = _bytes(path)

            self.assertEqual(cache.load()["days"], {
                cache.local_date: {
                    "simulations": [{"alpha_id": "synthetic-simulation"}],
                    "submitted_alphas": [{"alpha_id": "synthetic-submitted"}],
                }
            })
            self.assertEqual(
                cache.freshness_snapshot(now=FIXED_NOW)["freshness"],
                "FRESH",
            )
            self.assertEqual(_bytes(path), before)

    @critical_contract("cache.contract_mismatch_unknown_without_rewrite")
    def test_contract_mismatches_are_unknown_without_deleting_or_rewriting_cache(self):
        cases = (
            ("schema_version", 999, 7),
            ("timezone", "UTC", 7),
            ("week_start", "2099-01-01", 7),
        )
        for field, value, reader_retention in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "remote.json")
                _make_cache(path)
                _rewrite_payload(path, **{field: value})
                before = _bytes(path)
                reader = RemoteAlphaCache(
                    path, clock=lambda: FIXED_NOW,
                    retention_days=reader_retention,
                )

                self.assertIsNone(reader.load())
                self.assertEqual(
                    reader.freshness_snapshot(now=FIXED_NOW)["freshness"],
                    "UNKNOWN",
                )
                self.assertTrue(os.path.exists(path))
                self.assertEqual(_bytes(path), before)

    @critical_contract("cache.retention_mismatch_unknown")
    def test_retention_mismatch_is_unknown_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            _make_cache(path, retention_days=14)
            before = _bytes(path)
            reader = RemoteAlphaCache(
                path, clock=lambda: FIXED_NOW, retention_days=7
            )

            self.assertIsNone(reader.load())
            self.assertEqual(
                reader.freshness_snapshot(now=FIXED_NOW)["freshness"],
                "UNKNOWN",
            )
            self.assertTrue(os.path.exists(path))
            self.assertEqual(_bytes(path), before)

    @critical_contract("cache.read_paths_do_not_cleanup_temp")
    def test_read_paths_do_not_remove_stale_temp_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            stale_temp = path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = FIXED_NOW - TEMP_RESOURCE_TTL_SEC - 1
            os.utime(stale_temp, (stale_at, stale_at))
            before = _bytes(path)
            repository = RemoteAlphaRepository(
                None, cache_path=path, retention_days=7,
                clock=lambda: FIXED_NOW,
            )

            cache.load()
            cache.freshness_snapshot(now=FIXED_NOW)
            repository.list_remote_alphas()
            repository.cache_status()

            self.assertTrue(os.path.exists(stale_temp))
            self.assertEqual(_bytes(path), before)

    @critical_contract("cache.refresh_owns_stale_temp_cleanup")
    def test_refresh_owns_stale_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = _make_cache(path)
            stale_temp = path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = FIXED_NOW - TEMP_RESOURCE_TTL_SEC - 1
            os.utime(stale_temp, (stale_at, stale_at))

            result = cache.refresh({
                cache.local_date: {"simulations": [], "submitted_alphas": []}
            })

            self.assertFalse(os.path.exists(stale_temp))
            self.assertEqual(result["expired_resource_count"], 1)

    @critical_contract("cache.retained_rows_deduped_without_quota_pruning")
    def test_refresh_keeps_retained_rows_and_dedupes_without_quota_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "remote.json")
            cache = RemoteAlphaCache(
                path, clock=lambda: FIXED_NOW, retention_days=3,
            )
            local = cache.local_date
            local_day = date.fromisoformat(local)
            days = {
                local: {
                    "simulations": [
                        {"alpha_id": "synthetic-dup"},
                        {"alpha_id": "synthetic-dup"},
                        {"alpha_id": "synthetic-today"},
                    ],
                    "submitted_alphas": [
                        {"alpha_id": "synthetic-submitted"},
                    ],
                },
                (local_day - timedelta(days=1)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-yesterday"}],
                    "submitted_alphas": [],
                },
                (local_day - timedelta(days=2)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-two-days"}],
                    "submitted_alphas": [],
                },
                (local_day - timedelta(days=3)).isoformat(): {
                    "simulations": [{"alpha_id": "synthetic-expired"}],
                    "submitted_alphas": [],
                },
            }

            result = cache.refresh(days)
            payload = cache.load()

            self.assertEqual(result["simulation_count"], 4)
            self.assertEqual(result["submitted_count"], 1)
            self.assertNotIn("pruned_simulation_count", result)
            self.assertNotIn(
                "synthetic-expired",
                {
                    row["alpha_id"]
                    for bucket in payload["days"].values()
                    for row in bucket["simulations"]
                },
            )
            self.assertEqual(
                {
                    row["alpha_id"]
                    for bucket in payload["days"].values()
                    for row in bucket["simulations"]
                },
                {
                    "synthetic-dup",
                    "synthetic-today",
                    "synthetic-yesterday",
                    "synthetic-two-days",
                },
            )
            self.assertEqual(
                payload["days"][local]["submitted_alphas"],
                [{"alpha_id": "synthetic-submitted"}],
            )


FAKE_NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


class FakeAlphaReader:
    def __call__(self, **kwargs):
        now = FAKE_NOW.isoformat().replace("+00:00", "Z")
        if kwargs.get("date_submitted_after") is not None:
            return [{
                "id": "submitted-1",
                "status": "SUBMITTED",
                "dateCreated": "2026-09-16T12:00:00Z",
                "dateSubmitted": now,
            }]
        created = (FAKE_NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        return [{"id": "simulated-1", "dateCreated": created}]

    get_all_user_alphas = __call__


class FakeEvidenceClient(FakeAlphaReader):
    def get_alpha(self, alpha_id):
        return {"id": str(alpha_id), "is": {"sharpe": 1.0}}

    def get_aggregates(self, alpha_id):
        return {"alpha_id": str(alpha_id), "years": []}

    def get_pnl(self, alpha_id):
        return {"alpha_id": str(alpha_id), "records": []}

    def get_self_correlation(self, alpha_id):
        return {"alpha_id": str(alpha_id), "status": "AVAILABLE", "value": 0.2}


class ManySyntheticAlphaReader:
    def __init__(self, count):
        self.rows = [
            {
                "id": f"synthetic-alpha-{index:04d}",
                "dateCreated": "2026-09-16T12:00:00Z",
            }
            for index in range(count)
        ]

    def __call__(self, **kwargs):
        if kwargs.get("date_submitted_after") is not None:
            return []
        return list(self.rows)


class SubmittedAlphaReader:
    def __init__(self, rows):
        self.rows = list(rows)

    def __call__(self, **kwargs):
        if kwargs.get("date_submitted_after") is not None:
            return list(self.rows)
        return []


class TestRemoteAlphaRepository(unittest.TestCase):
    @critical_contract("repository.active_alpha_submitted_date")
    def test_refresh_finds_active_alpha_with_submission_date(self):
        def reader(**kwargs):
            if kwargs.get("date_submitted_after") is not None:
                return ([{
                    "id": "active-submission",
                    "status": "ACTIVE",
                    "dateCreated": "2026-09-22T12:00:00Z",
                    "dateSubmitted": "2026-09-23T12:00:00Z",
                }] if kwargs.get("status") is None else [])
            return []

        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                reader, cache_path=f"{tmp}/remote.json", retention_days=7,
                clock=lambda: 1790164800,
            )

            refreshed = repository.refresh_remote_alphas()
            submitted = [row for row in repository.list_remote_alphas()
                         if row.get("date_submitted")]

            self.assertEqual(refreshed["submitted_count"], 1)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0]["alpha_id"], "active-submission")
            self.assertEqual(submitted[0]["status"], "ACTIVE")
            self.assertEqual(
                [row["alpha_id"] for row in repository.list_remote_alphas(
                    status="SUBMITTED")],
                ["active-submission"],
            )

    @critical_contract("repository.metadata_not_quota_pruned")
    def test_refresh_keeps_all_retained_metadata_without_quota_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                ManySyntheticAlphaReader(1601),
                cache_path=f"{tmp}/remote.json",
                retention_days=1, clock=lambda: 1789560000,
            )

            refreshed = repository.refresh_remote_alphas()

            self.assertEqual(refreshed["rolling_simulated_count"], 1601)
            self.assertEqual(refreshed["simulation_count"], 1601)
            self.assertNotIn("pruned_simulation_count", refreshed)
            self.assertEqual(len(repository.list_remote_alphas()), 1601)

    @critical_contract("repository.refresh_shard_budget")
    def test_remote_feed_refresh_has_a_total_shard_budget(self):
        class BroadReader:
            def __init__(self):
                self.calls = 0

            def __call__(self, **_kwargs):
                self.calls += 1
                raise WQBQueryTooBroadError("too broad")

        with tempfile.TemporaryDirectory() as tmp:
            reader = BroadReader()
            repository = RemoteAlphaRepository(
                reader, cache_path=f"{tmp}/remote.json", clock=lambda: 1789560000,
            )
            with self.assertRaisesRegex(QueryTooBroadError, "shard budget"):
                repository.refresh_remote_alphas(max_shards=3)
            self.assertEqual(reader.calls, 3)

    @critical_contract("repository.configured_rolling_window")
    def test_refresh_and_list_use_configured_rolling_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: FAKE_NOW.timestamp(),
            )

            refreshed = repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas()

            self.assertEqual(refreshed["retention_days"], 3)
            self.assertEqual({row["alpha_id"] for row in listed},
                             {"submitted-1", "simulated-1"})
            submitted = next(row for row in listed if row["alpha_id"] == "submitted-1")
            self.assertEqual(submitted["date_created"], "2026-09-16T12:00:00Z")
            self.assertEqual(submitted["simulation_local_date"], "2026-09-16")
            self.assertEqual(repository.cache_status()["retention_days"], 3)

    @critical_contract("repository.creation_and_submission_dates_distinct")
    def test_submitted_metadata_keeps_submission_day_separate_from_creation_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                SubmittedAlphaReader([{
                    "id": "created-yesterday",
                    "status": "SUBMITTED",
                    "dateCreated": "2026-09-22T12:00:00Z",
                    "dateSubmitted": "2026-09-23T12:00:00Z",
                }]),
                cache_path=f"{tmp}/remote.json", retention_days=7,
                clock=lambda: 1790164800,
            )

            repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas(status="SUBMITTED")

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["local_date"], "2026-09-23")
            self.assertEqual(listed[0]["date_created"], "2026-09-22T12:00:00Z")
            self.assertEqual(listed[0]["simulation_local_date"], "2026-09-22")

    @critical_contract("repository.submitted_without_created_date_visible")
    def test_submitted_metadata_without_creation_date_remains_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                SubmittedAlphaReader([{
                    "id": "missing-created-date",
                    "status": "SUBMITTED",
                    "dateSubmitted": "2026-09-23T12:00:00Z",
                }]),
                cache_path=f"{tmp}/remote.json", retention_days=7,
                clock=lambda: 1790164800,
            )

            repository.refresh_remote_alphas()
            listed = repository.list_remote_alphas(status="SUBMITTED")

            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["local_date"], "2026-09-23")
            self.assertIsNone(listed[0]["date_created"])
            self.assertIsNone(listed[0]["simulation_local_date"])

    @critical_contract("repository.narrow_local_window_no_remote_io")
    def test_list_can_narrow_the_configured_window_without_remote_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                retention_days=3, clock=lambda: FAKE_NOW.timestamp(),
            )
            repository.refresh_remote_alphas()

            listed = repository.list_remote_alphas(days=1)

            self.assertEqual({row["alpha_id"] for row in listed}, {"submitted-1"})

    @critical_contract("repository.invalid_retention_fails_closed")
    def test_retention_window_is_fail_closed(self):
        with self.assertRaises(ValueError):
            RemoteAlphaRepository(FakeAlphaReader(), cache_path="x", retention_days=0)
        with self.assertRaises(ValueError):
            RemoteAlphaRepository(FakeAlphaReader(), cache_path="x", retention_days=91)

    @critical_contract("repository.public_api_no_agent_state")
    def test_public_repository_functions_do_not_require_agent_state_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"remote_cache": {"retention_days": 90}}
            result = research_api.refresh_remote_alphas(
                client=FakeAlphaReader(), config=config, state_dir=tmp
            )
            self.assertEqual(result["retention_days"], 90)
            listed = research_api.list_remote_alphas(config=config, state_dir=tmp)
            self.assertEqual(len(listed), 2)

    @critical_contract("repository.live_vs_cache_source")
    def test_public_remote_repository_reads_are_explicitly_live_or_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeEvidenceClient()
            live = research_api.get_remote_alpha_evidence(
                "alpha-1", client=client, state_dir=tmp
            )
            self.assertEqual(live["source"], "LIVE")
            self.assertEqual(
                research_api.get_remote_alpha("missing", state_dir=tmp), None
            )

    @critical_contract("repository.explicit_purge_owner")
    def test_purge_remote_cache_is_explicit_main_cache_delete_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(), cache_path=f"{tmp}/remote.json",
                clock=lambda: 1789560000,
            )
            repository.refresh_remote_alphas()
            self.assertTrue(os.path.exists(repository.cache.path))

            self.assertTrue(repository.purge_remote_cache())
            self.assertFalse(os.path.exists(repository.cache.path))

    @critical_contract("repository.status_read_no_temp_cleanup")
    def test_remote_cache_status_read_does_not_remove_stale_temp_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RemoteAlphaRepository(
                FakeAlphaReader(),
                cache_path=f"{tmp}/.alpha_feed_cache/remote.json",
                clock=None,
            )
            repository.refresh_remote_alphas()
            stale_temp = repository.cache.path + ".tmp.stale"
            with open(stale_temp, "w", encoding="utf-8") as handle:
                handle.write("synthetic temp")
            stale_at = time.time() - 7 * 24 * 60 * 60 - 1
            os.utime(stale_temp, (stale_at, stale_at))
            with open(repository.cache.path, "rb") as handle:
                before = handle.read()

            status = research_api.remote_cache_status(state_dir=tmp)

            self.assertIn(status["freshness"], {"FRESH", "STALE"})
            self.assertTrue(os.path.exists(stale_temp))
            with open(repository.cache.path, "rb") as handle:
                self.assertEqual(handle.read(), before)


def _row(alpha_id, expression, color=None, *, failed=False, status="DONE"):
    checks = [{"name": "syntax", "pass": not failed}]
    return {
        "alpha_id": alpha_id,
        "alpha": {
            "regular": expression,
            "status": status,
            "color": color,
            "is": {"sharpe": 99, "fitness": -99, "turnover": 99, "checks": checks},
        },
    }


class TestRemoteColors(unittest.TestCase):





    @critical_contract("remote_color.structural_similarity_family_assignment")
    def test_structural_variants_share_the_explicit_family_assignment(self):
        rows = [_row("a", "rank(close)"), _row("b", "rank(volume)")]
        family_key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={family_key: "BLUE"})

        self.assertEqual({entry["desired_color"] for entry in plan}, {"BLUE"})
        self.assertEqual({entry["structural_group_key"] for entry in plan}, {family_key})
        self.assertEqual({entry["family_member_count"] for entry in plan}, {2})

    @critical_contract("remote_color.numeric_variants_family_color_strict_keys")
    def test_numeric_variants_share_family_color_and_keep_distinct_structural_keys(self):
        rows = [
            _row("a", "ts_mean(close, 22)"),
            _row("b", "ts_mean(close, 66)"),
        ]
        family_key = variant_family_fingerprint("ts_mean(close, 22)")

        plan = preview_remote_colors(rows, assignments={family_key: "BLUE"})

        self.assertEqual({entry["variant_family_key"] for entry in plan}, {family_key})
        self.assertEqual({entry["desired_color"] for entry in plan}, {"BLUE"})
        self.assertEqual(
            {entry["structural_group_key"] for entry in plan},
            {
                structural_fingerprint("ts_mean(close, 22)"),
                structural_fingerprint("ts_mean(close, 66)"),
            },
        )
        self.assertNotEqual(
            structural_fingerprint("ts_mean(close, 22)"),
            structural_fingerprint("ts_mean(close, 66)"),
        )
        self.assertEqual({entry["family_member_count"] for entry in plan}, {2})
        self.assertEqual({entry["observed_execution_count"] for entry in plan}, {2})

    @critical_contract("remote_color.unassigned_no_automatic_color")
    def test_unassigned_families_never_receive_automatic_colors(self):
        plan = preview_remote_colors([
            _row("a", "rank(close)"),
            _row("b", "scale(close)"),
        ])

        self.assertEqual({entry["desired_color"] for entry in plan}, {None})
        self.assertEqual({entry["action"] for entry in plan}, {"UNASSIGNED"})
        self.assertTrue(all("color_index" not in entry for entry in plan))

    @critical_contract("remote_color.safety_constants_distinct")
    def test_safety_constant_families_do_not_share_color_assignment(self):
        rows = [
            _row("a", "divide(close, 0.001)"),
            _row("b", "divide(close, 0.01)"),
        ]
        family_key = variant_family_fingerprint("divide(close, 0.001)")

        plan = preview_remote_colors(rows, assignments={family_key: "BLUE"})

        self.assertEqual({entry["desired_color"] for entry in plan}, {None, "BLUE"})
        self.assertEqual(
            {entry["variant_family_key"] for entry in plan},
            {
                variant_family_fingerprint("divide(close, 0.001)"),
                variant_family_fingerprint("divide(close, 0.01)"),
            },
        )

    @critical_contract("remote_color.distinct_explicit_families_distinct_colors")
    def test_distinct_families_receive_only_their_explicit_assignments(self):
        rows = [_row("a", "rank(close)"), _row("b", "scale(close)")]
        assignments = {
            variant_family_fingerprint("rank(close)"): "BLUE",
            variant_family_fingerprint("scale(close)"): "GREEN",
        }

        plan = preview_remote_colors(rows, assignments=assignments)

        self.assertEqual(
            {entry["alpha_id"]: entry["desired_color"] for entry in plan},
            {"a": "BLUE", "b": "GREEN"},
        )

    @critical_contract("remote_color.unassigned_other_family_no_hash")
    def test_unassigned_family_is_preserved_without_hash_color(self):
        rows = [_row("a", "rank(close)"), _row("b", "scale(close)")]
        assigned_key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={assigned_key: "BLUE"})

        unassigned = next(entry for entry in plan if entry["alpha_id"] == "b")
        self.assertIsNone(unassigned["desired_color"])
        self.assertEqual(unassigned["action"], "UNASSIGNED")
        self.assertNotIn("color_index", unassigned)

    @critical_contract("remote_color.max_five_family_limit")
    def test_more_than_five_explicit_family_groups_are_rejected(self):
        expressions = [
            "rank(close)", "scale(close)", "zscore(close)",
            "log(close)", "abs(close)", "sign(close)",
        ]
        rows = [_row(str(index), expression) for index, expression in enumerate(expressions)]
        assignments = {
            variant_family_fingerprint(expression): "BLUE"
            for expression in expressions
        }

        with self.assertRaisesRegex(ValueError, "MAX_ACTIVE_COLOR_GROUPS"):
            preview_remote_colors(rows, assignments=assignments)

    @critical_contract("remote_color.mixed_existing_colors_reported")
    def test_mixed_existing_family_colors_are_visible_in_the_plan(self):
        rows = [
            _row("a", "rank(close)", "BLUE"),
            _row("b", "rank(volume)", "GREEN"),
        ]
        family_key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={family_key: "PURPLE"})

        self.assertEqual({entry["existing_colors"] for entry in plan}, {("BLUE", "GREEN")})
        self.assertEqual(
            {entry["existing_color_state"] for entry in plan},
            {"MIXED_EXISTING_COLOR"},
        )
        self.assertEqual({entry["quality_state"] for entry in plan}, {"DONE"})

    @critical_contract("remote_color.quality_text_only")
    def test_quality_is_text_only_and_does_not_change_assignment(self):
        row = _row("a", "rank(close)", failed=True)
        key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors([row], assignments={key: "PURPLE"})

        self.assertEqual(plan[0]["desired_color"], "PURPLE")
        self.assertEqual(plan[0]["quality_state"], "FAILED_CHECK")

    @critical_contract("remote_color.preview_immutable_fields")
    def test_preview_entries_are_immutable_and_have_review_fields(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", color="BLUE")],
            assignments={key: "GREEN"},
        )

        self.assertIsInstance(plan, tuple)
        self.assertEqual(
            set(plan[0]),
            {
                "alpha_id", "structural_group_key", "quality_state",
                "existing_color_state", "expected_old_color", "desired_color", "action",
                "existing_colors", "variant_family_key", "family_member_count",
                "observed_execution_count",
            },
        )
        with self.assertRaises(TypeError):
            plan[0]["desired_color"] = "RED"



    @critical_contract("remote_color.assignment_outside_snapshot_rejected")
    def test_assignment_outside_snapshot_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_VARIANT_FAMILY"):
            preview_remote_colors(
                [_row("a", "rank(close)")], assignments={"missing": "BLUE"}
            )


    @critical_contract("remote_color.sync_exact_plan_no_quality_reclassification")
    def test_sync_consumes_exact_plan_and_does_not_reclassify_quality(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        patched = []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda _alpha_id: {
                "regular": "rank(close)", "color": "BLUE",
            },
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs))
            or {"color": "GREEN"},
            overwrite=True,
        )

        self.assertEqual(result[0]["action"], "PATCHED")
        self.assertEqual(patched, [(('a', 'GREEN'), {"verify": True})])

    @critical_contract("remote_color.sync_exact_plan_no_quality_reclassification")
    def test_sync_never_expands_plan_to_new_alpha_ids(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        current_alphas = {
            "a": {"regular": "rank(close)", "color": "BLUE"},
            "new": {"regular": "scale(close)", "color": None},
        }
        read_ids, patch_ids = [], []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda alpha_id: read_ids.append(alpha_id) or current_alphas[alpha_id],
            set_alpha_color=lambda alpha_id, color, **kwargs: patch_ids.append(
                alpha_id
            ) or {"color": color},
            overwrite=True,
        )

        self.assertEqual([item["action"] for item in result], ["PATCHED"])
        self.assertEqual(read_ids, ["a"])
        self.assertEqual(patch_ids, ["a"])

    @critical_contract("remote_color.stale_plan_no_patch")
    def test_stale_plan_never_patches_even_with_overwrite(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        patched = []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda _alpha_id: {
                "regular": "rank(close)", "color": "PURPLE",
            },
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs)),
            overwrite=True,
        )

        self.assertEqual(result[0]["action"], "STALE_PLAN")
        self.assertEqual(result[0]["expected_old_color"], "BLUE")
        self.assertEqual(result[0]["current_color"], "PURPLE")
        self.assertEqual(patched, [])

        identity_patches = []
        identity_changed = sync_remote_colors(
            plan,
            get_alpha=lambda _alpha_id: {
                "regular": "scale(close)", "color": "BLUE",
            },
            set_alpha_color=lambda *args, **kwargs: identity_patches.append(
                (args, kwargs)
            ) or {"color": "GREEN"},
            overwrite=True,
        )
        self.assertEqual(identity_changed[0]["action"], "STALE_PLAN")
        self.assertEqual(identity_patches, [])

    @critical_contract("remote_color.stale_plan_no_patch")
    def test_one_stale_entry_does_not_patch_another_reviewed_alpha(self):
        expressions = {"a": "rank(close)", "b": "scale(close)"}
        assignments = {
            variant_family_fingerprint(expression): "GREEN"
            for expression in expressions.values()
        }
        plan = preview_remote_colors([
            _row(alpha_id, expression, "BLUE")
            for alpha_id, expression in expressions.items()
        ], assignments=assignments)
        patched = []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda alpha_id: {
                "regular": expressions[alpha_id],
                "color": "PURPLE" if alpha_id == "a" else "BLUE",
            },
            set_alpha_color=lambda alpha_id, color, **kwargs: patched.append(
                (alpha_id, color)
            ) or {"color": color},
            overwrite=True,
        )

        self.assertEqual(
            {item["alpha_id"]: item["action"] for item in result},
            {"a": "STALE_PLAN", "b": "PATCHED"},
        )
        self.assertEqual(patched, [("b", "GREEN")])

    @critical_contract("remote_color.noop_preserve_overwrite")
    def test_noop_preserve_and_explicit_overwrite_actions(self):
        key = variant_family_fingerprint("rank(close)")
        noop = preview_remote_colors(
            [_row("a", "rank(close)", "GREEN")], assignments={key: "GREEN"}
        )
        noop_setter = mock.Mock()
        self.assertEqual(
            sync_remote_colors(
                noop,
                get_alpha=lambda _: {
                    "regular": "rank(close)", "color": "GREEN",
                },
                set_alpha_color=noop_setter,
            )[0]["action"],
            "NOOP",
        )
        noop_setter.assert_not_called()

        preserved = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        setter = mock.Mock()
        self.assertEqual(
            sync_remote_colors(
                preserved,
                get_alpha=lambda _: {
                    "regular": "rank(close)", "color": "BLUE",
                },
                set_alpha_color=setter,
            )[0]["action"],
            "PRESERVED_EXISTING",
        )
        setter.assert_not_called()

    @critical_contract("remote_color.readback_mismatch_fails_closed")
    def test_readback_mismatch_fails_closed(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )

        with self.assertRaisesRegex(ValueError, "COLOR_READBACK_MISMATCH"):
            sync_remote_colors(
                plan,
                get_alpha=lambda _: {
                    "regular": "rank(close)", "color": "BLUE",
                },
                set_alpha_color=lambda *args, **kwargs: {"color": "RED"},
                overwrite=True,
            )

    @critical_contract("remote_color.partial_failure_not_success")
    def test_single_patch_failure_does_not_return_batch_success(self):
        expressions = {"a": "rank(close)", "b": "scale(close)"}
        assignments = {
            variant_family_fingerprint(expression): "GREEN"
            for expression in expressions.values()
        }
        plan = preview_remote_colors([
            _row(alpha_id, expression, "BLUE")
            for alpha_id, expression in expressions.items()
        ], assignments=assignments)
        patch_attempts = []

        def set_color(alpha_id, color, *, verify):
            patch_attempts.append((alpha_id, color, verify))
            if len(patch_attempts) == 2:
                raise RuntimeError("synthetic second patch failed")
            return {"color": color}

        def get_alpha(alpha_id):
            return {"regular": expressions[alpha_id], "color": "BLUE"}

        with self.assertRaisesRegex(RuntimeError, "synthetic second patch failed"):
            sync_remote_colors(
                plan,
                get_alpha=get_alpha,
                set_alpha_color=set_color,
                overwrite=True,
            )

        self.assertEqual(len(patch_attempts), 2)
        self.assertTrue(all(item[2] for item in patch_attempts))


if __name__ == "__main__":
    unittest.main()
