"""Tests for artifact append semantics."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from wqb_agent import artifacts
from wqb_agent.artifacts import append_jsonl_if_unique


class TestAppendJsonlSemantics(unittest.TestCase):
    def test_owning_lock_makes_duplicate_check_atomic_for_concurrent_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            owner_lock = threading.Lock()
            barrier = threading.Barrier(2)
            results = []

            def append_once():
                barrier.wait()
                try:
                    result = append_jsonl_if_unique(
                        path, {"event_id": "same"}, ("event_id",),
                        lock=owner_lock,
                    )
                except Exception as exc:  # assert the failure at the caller
                    result = exc
                results.append(result)

            workers = [threading.Thread(target=append_once) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(1.0)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(sorted(results, key=repr), [False, True])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(len([json.loads(line) for line in handle]), 1)


class TestAtomicArtifactDurability(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
