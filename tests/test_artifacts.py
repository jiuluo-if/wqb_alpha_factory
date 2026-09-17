"""Tests for atomic local artifact persistence."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wqb_agent import artifacts


class TestAtomicArtifactDurability(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_private_artifact_is_owner_only_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "private.json")
            with mock.patch.object(artifacts.os, "umask", return_value=0o000):
                artifacts.atomic_write_json_if_changed(path, {"secret": True}, private=True)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_private_atomic_update_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "private.json")
            artifacts.atomic_write_json_if_changed(path, {"version": 1}, private=True)
            os.chmod(path, 0o640)
            artifacts.atomic_write_json_if_changed(path, {"version": 2}, private=True)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)

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
