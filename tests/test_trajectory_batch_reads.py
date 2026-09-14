"""Batch trajectory read contracts.

``Trajectory.find_rows()`` and ``research_api.compare_experiments()`` resolve
several identities with one canonical streaming pass.  They must keep the
single-id ``find_row`` semantics exactly: the latest legal revision wins, a row
whose execution identity contradicts the first legal match is ignored, and a
missing identity stays missing instead of being fabricated.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from wqb_agent.research_api import compare_experiments, get_experiment
from wqb_agent.state import (
    RESEARCH_SETTLED_REVISION,
    Experiment,
    Trajectory,
    json_literal_prefilter,
    literal_line_matcher,
)


def _experiment(identity, *, expression="rank(field_a)", status="DONE"):
    experiment = Experiment(1, "h-1", expression, {"decay": 4}, ["field_a"], ["pv1"])
    experiment.id = identity
    experiment.proposal_id = f"proposal-{identity}"
    experiment.status = status
    experiment.metrics = {"sharpe": 1.0} if status == "DONE" else None
    return experiment


def _write(path, experiments, revision=None):
    with open(path, "w", encoding="utf-8") as handle:
        for experiment in experiments:
            row = experiment.to_dict()
            if revision:
                row["trajectory_revision"] = revision
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class TestFindRowsBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_batch_reads_")
        self.path = os.path.join(self.tmp, "trajectory.jsonl")

    def _trajectory(self):
        return Trajectory(path=self.path, persist=True)

    def test_batch_matches_single_id_reads(self):
        _write(self.path, [_experiment("e0"), _experiment("e1"), _experiment("e2")])
        trajectory = self._trajectory()
        found = trajectory.find_rows(["e0", "e1", "e2"])
        self.assertEqual(set(found), {"e0", "e1", "e2"})
        for identity in ("e0", "e1", "e2"):
            self.assertEqual(found[identity], trajectory.find_row(identity))

    def test_batch_resolves_proposal_identity(self):
        _write(self.path, [_experiment("e0")])
        found = self._trajectory().find_rows(["proposal-e0"])
        self.assertEqual(found["proposal-e0"]["id"], "e0")

    def test_batch_keeps_latest_settlement_revision(self):
        early = _experiment("e0")
        _write(self.path, [early])
        trajectory = self._trajectory()
        trajectory.load()
        # Execution identity (including created_at) must stay immutable, so the
        # settlement revision is derived from the persisted row instead of a
        # freshly constructed Experiment.
        settled = Experiment.from_dict(early.to_dict())
        settled.final_outcome = {"reward": 2.0, "reward_quality": "FINAL_EVIDENCE"}
        settled.research_classification = {"label": "PROMISING"}
        self.assertTrue(trajectory.settle(settled))
        found = self._trajectory().find_rows(["e0"])
        self.assertEqual(found["e0"]["final_outcome"]["reward"], 2.0)
        self.assertEqual(found["e0"], self._trajectory().find_row("e0"))

    def test_batch_ignores_identity_conflicting_revision(self):
        early = _experiment("e0")
        conflicting = _experiment("e0", expression="rank(field_b)")
        _write(self.path, [early, conflicting], revision=RESEARCH_SETTLED_REVISION)
        found = self._trajectory().find_rows(["e0"])
        self.assertEqual(found["e0"]["expression"], "rank(field_a)")

    def test_batch_finds_identities_that_need_escaping(self):
        identity = 'e"0\\中'
        _write(self.path, [_experiment(identity)])
        trajectory = self._trajectory()
        found = trajectory.find_rows([identity])
        self.assertEqual(found[identity], trajectory.find_row(identity))
        self.assertIsNotNone(found[identity])

    def test_missing_identity_stays_missing(self):
        _write(self.path, [_experiment("e0")])
        found = self._trajectory().find_rows(["e0", "missing"])
        self.assertIsNone(found["missing"])
        self.assertIsNotNone(found["e0"])

    def test_empty_or_unpersisted_reads_are_empty(self):
        self.assertEqual(self._trajectory().find_rows([]), {})
        self.assertEqual(self._trajectory().find_rows(None), {})
        self.assertEqual(
            Trajectory(path=self.path, persist=False).find_rows(["e0"]), {}
        )

    def test_iter_rows_prefilter_never_hides_matching_rows(self):
        _write(self.path, [_experiment("e0"), _experiment("e1")])
        trajectory = self._trajectory()
        self.assertEqual(
            {row["id"] for row in trajectory.iter_rows()}, {"e0", "e1"}
        )
        self.assertEqual(
            {row["id"] for row in trajectory.iter_rows(prefilter="e1")}, {"e1"}
        )
        self.assertEqual(
            {
                row["id"]
                for row in trajectory.iter_rows(prefilter=("e0", "e1"))
            },
            {"e0", "e1"},
        )

    def test_canonical_round_reads_beyond_bounded_memory_and_keeps_settings_identity(self):
        experiments = []
        for identity, settings in (
            ("round-a", {"decay": 4}),
            ("round-b", {"decay": 5}),
        ):
            experiment = Experiment(
                77, "h-77", "rank(field_a)", settings, ["field_a"], ["pv1"]
            )
            experiment.id = identity
            experiment.status = "DONE"
            experiments.append(experiment)
        third = _experiment("round-c")
        third.round = 76
        experiments.append(third)
        _write(self.path, experiments)

        trajectory = Trajectory(max_len=1, path=self.path).load()
        stats = {}
        rows = list(trajectory.iter_canonical_round(77, stats=stats))

        self.assertEqual({row["id"] for row in rows}, {"round-a", "round-b"})
        self.assertEqual(stats.get("identity_mismatch_rows", 0), 0)

    def test_canonical_round_reports_identity_conflicting_revision(self):
        early = _experiment("round-conflict")
        early.round = 78
        conflicting = _experiment("round-conflict", expression="rank(field_b)")
        conflicting.round = 78
        _write(self.path, [early, conflicting], revision=RESEARCH_SETTLED_REVISION)

        stats = {}
        rows = list(Trajectory(path=self.path).iter_canonical_round(78, stats=stats))

        self.assertEqual([row["expression"] for row in rows], ["rank(field_a)"])
        self.assertEqual(stats["identity_mismatch_rows"], 1)

    def test_iter_rows_decodes_escaped_lines_instead_of_skipping(self):
        row = _experiment("e0").to_dict()
        row["proposal_id"] = None
        line = json.dumps(row, ensure_ascii=False)
        # ``\u0030`` decodes to ``0``: the raw text no longer holds the id, so
        # only the escape guard can keep the row reachable.
        escaped = line.replace('"e0"', '"e\\u0030"')
        self.assertNotEqual(escaped, line)
        self.assertNotIn("e0", escaped)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(escaped + "\n")
        trajectory = self._trajectory()
        self.assertEqual(
            [row["id"] for row in trajectory.iter_rows(prefilter="e0")], ["e0"]
        )
        self.assertEqual(trajectory.contains_ids(["e0"]), {"e0"})

    def test_contains_ids_matches_batch_read_semantics(self):
        complex_id = 'e"0\\中'
        control_id = "e\u0001z"
        _write(
            self.path,
            [
                _experiment("e0"),
                _experiment(complex_id),
                _experiment(control_id),
                _experiment("e2"),
            ],
        )
        found = self._trajectory().contains_ids(
            ["e0", complex_id, control_id, "e2", "missing", 7]
        )
        self.assertEqual(found, {"e0", complex_id, control_id, "e2"})

    def test_contains_ids_fails_closed_for_escapable_identities(self):
        complex_id = '\u4e2d"0\\'
        _write(self.path, [_experiment(complex_id)])
        # No raw prefilter is legal for this identity, so the read must fall
        # back to decoding every line instead of silently reporting absence.
        self.assertIsNone(json_literal_prefilter(complex_id))
        self.assertEqual(self._trajectory().contains_ids([complex_id]), {complex_id})

    def test_contains_ids_decodes_only_candidate_lines(self):
        _write(self.path, [_experiment(f"e{index}") for index in range(50)])
        trajectory = self._trajectory()
        decoded = []
        real_loads = json.loads

        def counting_loads(line):
            decoded.append(line)
            return real_loads(line)

        with mock.patch.object(json, "loads", counting_loads):
            found = trajectory.contains_ids(["e7", "e42"])
        self.assertEqual(found, {"e7", "e42"})
        self.assertEqual(len(decoded), 2)

    def test_literal_line_matcher_escapes_and_compiles_batches(self):
        self.assertEqual(literal_line_matcher("e0"), "e0")
        self.assertIsNone(literal_line_matcher(None))
        self.assertIsNone(literal_line_matcher(()))
        batch = literal_line_matcher(("e0", "e1"))
        self.assertIsNotNone(batch.search('{"id": "e1"}'))
        self.assertIsNone(batch.search('{"id": "e9"}'))
        escaped = literal_line_matcher(("a.b", "zzz"))
        self.assertIsNone(escaped.search("axb"))
        self.assertIsNotNone(escaped.search("a.b"))


class TestCompareExperimentsBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wqb_compare_")
        self.path = os.path.join(self.tmp, "trajectory.jsonl")

    def test_compare_reads_append_only_history_once(self):
        _write(self.path, [_experiment("e0"), _experiment("e1"), _experiment("e2")])
        original = Trajectory.iter_rows
        calls = []

        def counting(self, **kwargs):
            calls.append(kwargs)
            yield from original(self, **kwargs)

        with mock.patch.object(Trajectory, "iter_rows", counting):
            result = compare_experiments(["e0", "e1", "e2"], state_dir=self.tmp)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row["id"] for row in result["experiments"]], ["e0", "e1", "e2"])
        self.assertEqual(result["missing"], [])

    def test_compare_reports_missing_and_keeps_request_order(self):
        _write(self.path, [_experiment("e0")])
        result = compare_experiments(["e0", "missing"], state_dir=self.tmp)
        self.assertEqual([row["id"] for row in result["experiments"]], ["e0"])
        self.assertEqual(result["missing"], ["missing"])

    def test_single_id_read_matches_batch_read(self):
        _write(self.path, [_experiment("e0")])
        self.assertEqual(
            get_experiment("e0", state_dir=self.tmp),
            compare_experiments(["e0"], state_dir=self.tmp)["experiments"][0],
        )
