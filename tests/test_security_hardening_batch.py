import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from wqb_agent import artifacts
from wqb_agent.client import WQBClient, WQBSimulationError, WQBSubmitUnknownError
from wqb_agent.state import Experiment, Trajectory, TrajectoryIntegrityError
from wqb_agent.validation_report import pbo_proxy

ROOT = Path(__file__).resolve().parent.parent


class DurabilityTests(unittest.TestCase):
    def test_atomic_replace_fsyncs_parent_after_replace_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            order = []
            with mock.patch.object(artifacts, "os", wraps=artifacts.os) as mocked_os:
                mocked_os.replace.side_effect = lambda src, dst: (
                    order.append("replace"), os.replace(src, dst)
                )[1]
                with mock.patch.object(artifacts, "_fsync_parent_directory",
                                       side_effect=lambda value: order.append("dir")):
                    artifacts._atomic_replace(path, b"ok")
            self.assertEqual(order, ["replace", "dir"])
            self.assertEqual(Path(path).read_bytes(), b"ok")
            self.assertFalse(list(Path(tmp).glob("*.tmp.*")))

    def test_atomic_replace_failure_is_not_reported_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(artifacts.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    artifacts._atomic_replace(path, b"bad")
            self.assertFalse(os.path.exists(path))
            self.assertFalse(list(Path(tmp).glob("*.tmp.*")))

    def test_parent_fsync_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(artifacts, "_fsync_parent_directory",
                                   side_effect=OSError("directory fsync failed")):
                with self.assertRaises(OSError):
                    artifacts._atomic_replace(path, b"written-but-not-claimed")
            self.assertEqual(Path(path).read_bytes(), b"written-but-not-claimed")

    def test_unchanged_atomic_json_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self.assertTrue(artifacts.atomic_write_json_if_changed(path, {"x": 1}))
            with mock.patch.object(artifacts, "_fsync_parent_directory") as sync:
                self.assertFalse(artifacts.atomic_write_json_if_changed(path, {"x": 1}))
            sync.assert_not_called()


class TrajectoryIntegrityTests(unittest.TestCase):
    def test_new_and_legacy_ids_and_identity_replay_contract(self):
        experiment = Experiment(1, "h", "rank(x)", {}, ["x"])
        self.assertRegex(experiment.id, re.compile(r"^[0-9a-f]{32}$"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            legacy = experiment.to_dict()
            legacy["id"] = "legacy123456"
            Path(path).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            trajectory = Trajectory(path=path).load()
            self.assertEqual(trajectory.experiments[0].id, "legacy123456")
            self.assertEqual(trajectory.add_many([Experiment.from_dict(legacy)]), [])
            changed = dict(legacy, expression="rank(y)")
            with self.assertRaises(TrajectoryIntegrityError):
                trajectory.add_many([Experiment.from_dict(changed)])

    def test_strict_corruption_blocks_but_torn_final_tail_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            good = Experiment(1, "h", "rank(x)", {}, ["x"]).to_dict()
            later = dict(good, id="later", expression="rank(y)")
            Path(path).write_text(
                "\n".join([json.dumps(good), "{broken", json.dumps(later)]) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(TrajectoryIntegrityError):
                list(Trajectory(path=path).iter_rows(strict=True))
            Path(path).write_text(json.dumps(good) + "\n{\"partial\":", encoding="utf-8")
            stats = {}
            rows = list(Trajectory(path=path).iter_rows(strict=True, stats=stats))
            self.assertEqual([row["id"] for row in rows], [good["id"]])
            self.assertEqual(stats["torn_tail"], 1)

    def test_non_object_canonical_row_is_strictly_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trajectory.jsonl")
            Path(path).write_text('{}\n[]\n', encoding="utf-8")
            with self.assertRaises(TrajectoryIntegrityError):
                list(Trajectory(path=path).iter_rows(strict=True))


class ProgressUrlTests(unittest.TestCase):
    def setUp(self):
        self.client = WQBClient.__new__(WQBClient)
        self.client.base_url = "https://api.example.test"

    def test_relative_and_same_origin_urls_normalize(self):
        self.assertEqual(
            self.client._normalize_progress_url("/simulations/1"),
            "https://api.example.test/simulations/1",
        )
        self.assertEqual(
            self.client._normalize_progress_url("https://api.example.test/simulations/2"),
            "https://api.example.test/simulations/2",
        )

    def test_cross_origin_downgrade_port_and_fragment_are_rejected(self):
        for url in (
            "https://evil.example/simulations/1",
            "http://api.example.test/simulations/1",
            "https://api.example.test:444/simulations/1",
            "https://api.example.test/simulations/1#fragment",
        ):
            with self.assertRaises(WQBSimulationError):
                self.client._normalize_progress_url(url)

    def test_accepted_invalid_location_is_submit_unknown(self):
        response = mock.Mock(headers={"Location": "https://evil.example/job"})
        self.client._wait_submission_slot = mock.Mock()
        self.client._request = mock.Mock(return_value=response)
        with self.assertRaises(WQBSubmitUnknownError):
            self.client.submit_simulation("rank(x)", {})
        self.client._request.assert_called_once()

    def test_invalid_persisted_url_makes_no_get(self):
        self.client._session = mock.Mock()
        with self.assertRaises(WQBSimulationError):
            self.client.get_progress_snapshot("https://evil.example/job")
        self.client._session.assert_not_called()


class PboPartitionTests(unittest.TestCase):
    def test_non_divisible_observations_are_unavailable(self):
        for size in (9, 10, 11):
            rows = [[float(index) for index in range(size)] for _ in range(2)]
            result = pbo_proxy(rows, n_splits=4)
            self.assertEqual(result["status"], "UNAVAILABLE")

    def test_divisible_observations_are_available(self):
        for size in (8, 12):
            rows = [[float(index) for index in range(size)] for _ in range(2)]
            self.assertEqual(pbo_proxy(rows, n_splits=4)["status"], "AVAILABLE")


class PackagingTests(unittest.TestCase):
    def test_package_reference_matches_human_mirror(self):
        from wqb_agent.proposal_contract import load_packaged_operator_syntax_reference

        packaged = load_packaged_operator_syntax_reference()
        mirror = (ROOT / "docs" / "reference" / "OPERATORS_CHEATSHEET.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(packaged["sha256"], __import__("hashlib").sha256(mirror.encode()).hexdigest())

    def test_clean_wheel_contains_and_loads_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel_dir = Path(tmp) / "wheel"
            extract_dir = Path(tmp) / "extract"
            wheel_dir.mkdir()
            extract_dir.mkdir()
            result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(wheel_dir)],
                cwd=ROOT, capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            wheel = next(wheel_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(extract_dir)
            probe = subprocess.run(
                [sys.executable, "-c", "from wqb_agent.proposal_contract import load_packaged_operator_syntax_reference; print(len(load_packaged_operator_syntax_reference()['operators']))"],
                cwd=tmp, env={**os.environ, "PYTHONPATH": str(extract_dir)},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertGreater(int(probe.stdout.strip()), 0)


if __name__ == "__main__":
    unittest.main()
