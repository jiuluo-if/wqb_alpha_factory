import unittest
from unittest import mock

from wqb_agent.alpha_grouping import structural_fingerprint, variant_family_fingerprint
from wqb_agent.remote_colors import preview_remote_colors, sync_remote_colors


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
    def test_structural_similarity_shares_explicit_family_assignment(self):
        rows = [_row("a", "rank(close)"), _row("b", "rank(volume)")]
        key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={key: "BLUE"})

        self.assertEqual({entry["desired_color"] for entry in plan}, {"BLUE"})
        self.assertEqual({entry["structural_group_key"] for entry in plan}, {key})
        self.assertEqual({entry["group_size"] for entry in plan}, {2})

    def test_numeric_variants_share_family_color_but_keep_strict_keys(self):
        rows = [_row("a", "ts_mean(close, 22)"), _row("b", "ts_mean(close, 66)")]
        family_key = variant_family_fingerprint("ts_mean(close, 22)")

        plan = preview_remote_colors(rows, assignments={family_key: "BLUE"})

        self.assertEqual({entry["variant_family_key"] for entry in plan}, {family_key})
        self.assertEqual({entry["desired_color"] for entry in plan}, {"BLUE"})
        self.assertEqual(
            {entry["structural_group_key"] for entry in plan},
            {structural_fingerprint("ts_mean(close, 22)"),
             structural_fingerprint("ts_mean(close, 66)")},
        )
        self.assertNotEqual(
            structural_fingerprint("ts_mean(close, 22)"),
            structural_fingerprint("ts_mean(close, 66)"),
        )
        self.assertEqual({entry["observed_variant_count"] for entry in plan}, {2})

    def test_unassigned_families_never_receive_automatic_collision_colors(self):
        rows = [_row("a", "rank(close)"), _row("b", "scale(close)")]

        plan = preview_remote_colors(rows)

        self.assertEqual({entry["desired_color"] for entry in plan}, {None})
        self.assertEqual({entry["action"] for entry in plan}, {"UNASSIGNED"})

    def test_different_families_use_their_explicit_distinct_colors(self):
        rows = [_row("a", "rank(close)"), _row("b", "scale(close)")]
        assignments = {
            variant_family_fingerprint("rank(close)"): "BLUE",
            variant_family_fingerprint("scale(close)"): "GREEN",
        }

        plan = preview_remote_colors(rows, assignments=assignments)

        self.assertEqual(
            {entry["desired_color"] for entry in plan}, {"BLUE", "GREEN"}
        )

    def test_quality_is_text_only_and_does_not_change_assignment(self):
        row = _row("a", "rank(close)", failed=True)
        key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors([row], assignments={key: "PURPLE"})

        self.assertEqual(plan[0]["desired_color"], "PURPLE")
        self.assertEqual(plan[0]["quality_state"], "FAILED_CHECK")

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
                "alpha_id", "structural_group_key", "group_size", "quality_state",
                "existing_color_state", "expected_old_color", "desired_color", "action",
                "existing_colors", "variant_family_key", "observed_variant_count",
            },
        )
        with self.assertRaises(TypeError):
            plan[0]["desired_color"] = "RED"

    def test_unassigned_groups_are_preserved_without_hash_color(self):
        rows = [_row("a", "rank(close)"), _row("b", "scale(close)")]
        key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={key: "BLUE"})

        unassigned = next(entry for entry in plan if entry["alpha_id"] == "b")
        self.assertIsNone(unassigned["desired_color"])
        self.assertEqual(unassigned["action"], "UNASSIGNED")
        self.assertNotIn("color_index", unassigned)

    def test_more_than_five_explicit_groups_is_rejected_without_collision(self):
        expressions = ["rank(close)", "scale(close)", "zscore(close)",
                       "log(close)", "abs(close)", "sign(close)"]
        rows = [_row(str(index), expression) for index, expression in enumerate(expressions)]
        assignments = {
            variant_family_fingerprint(expression): "BLUE" for expression in expressions
        }

        with self.assertRaisesRegex(ValueError, "MAX_ACTIVE_COLOR_GROUPS"):
            preview_remote_colors(rows, assignments=assignments)

    def test_assignment_outside_snapshot_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_VARIANT_FAMILY"):
            preview_remote_colors(
                [_row("a", "rank(close)")], assignments={"missing": "BLUE"}
            )

    def test_mixed_existing_colors_are_visible_in_review_plan(self):
        rows = [_row("a", "rank(close)", "BLUE"), _row("b", "rank(volume)", "GREEN")]
        key = variant_family_fingerprint("rank(close)")

        plan = preview_remote_colors(rows, assignments={key: "PURPLE"})

        self.assertEqual(plan[0]["existing_colors"], ("BLUE", "GREEN"))
        self.assertEqual(plan[0]["existing_color_state"], "MIXED_EXISTING_COLOR")
        self.assertEqual(plan[0]["quality_state"], "DONE")

    def test_sync_consumes_exact_plan_and_does_not_reclassify_quality(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        patched = []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda _alpha_id: {"color": "BLUE"},
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs))
            or {"color": "GREEN"},
            overwrite=True,
        )

        self.assertEqual(result[0]["action"], "PATCHED")
        self.assertEqual(patched, [(('a', 'GREEN'), {"verify": True})])

    def test_stale_plan_never_patches_even_with_overwrite(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        patched = []

        result = sync_remote_colors(
            plan,
            get_alpha=lambda _alpha_id: {"color": "PURPLE"},
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs)),
            overwrite=True,
        )

        self.assertEqual(result[0]["action"], "STALE_PLAN")
        self.assertEqual(result[0]["expected_old_color"], "BLUE")
        self.assertEqual(result[0]["current_color"], "PURPLE")
        self.assertEqual(patched, [])

    def test_noop_preserve_and_explicit_overwrite_actions(self):
        key = variant_family_fingerprint("rank(close)")
        noop = preview_remote_colors(
            [_row("a", "rank(close)", "GREEN")], assignments={key: "GREEN"}
        )
        self.assertEqual(
            sync_remote_colors(noop, get_alpha=lambda _: {"color": "GREEN"},
                               set_alpha_color=mock.Mock())[0]["action"],
            "NOOP",
        )

        preserved = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )
        setter = mock.Mock()
        self.assertEqual(
            sync_remote_colors(preserved, get_alpha=lambda _: {"color": "BLUE"},
                               set_alpha_color=setter)[0]["action"],
            "PRESERVED_EXISTING",
        )
        setter.assert_not_called()

    def test_readback_mismatch_fails_closed(self):
        key = variant_family_fingerprint("rank(close)")
        plan = preview_remote_colors(
            [_row("a", "rank(close)", "BLUE")], assignments={key: "GREEN"}
        )

        with self.assertRaisesRegex(ValueError, "COLOR_READBACK_MISMATCH"):
            sync_remote_colors(
                plan,
                get_alpha=lambda _: {"color": "BLUE"},
                set_alpha_color=lambda *args, **kwargs: {"color": "RED"},
                overwrite=True,
            )


if __name__ == "__main__":
    unittest.main()
