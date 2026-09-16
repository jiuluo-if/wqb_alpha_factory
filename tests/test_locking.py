import json
import multiprocessing
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.locking import OwnerBusyError, single_instance_scope


def _hold_owner(state_dir, ready, release):
    with single_instance_scope(state_dir, operation="process-a"):
        ready.set()
        release.wait(10)


def _try_owner(state_dir, result):
    try:
        with single_instance_scope(state_dir, operation="process-b"):
            result.put("entered")
    except OwnerBusyError:
        result.put("busy")


class TestSingleInstanceScope(unittest.TestCase):
    def test_nested_same_thread_is_reentrant_without_metadata_rewrite(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with single_instance_scope(state_dir, operation="factory-run"):
                with open(f"{state_dir}/run.lock", encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)["operation"], "factory-run")
                with single_instance_scope(state_dir, operation="run-proposals"):
                    with open(f"{state_dir}/run.lock", encoding="utf-8") as handle:
                        self.assertEqual(json.load(handle)["operation"], "factory-run")
                self.assertTrue((__import__("pathlib").Path(state_dir) / "run.lock").exists())
            self.assertFalse((__import__("pathlib").Path(state_dir) / "run.lock").exists())

    def test_other_thread_cannot_reenter_same_process_owner(self):
        with tempfile.TemporaryDirectory() as state_dir:
            entered = []
            with single_instance_scope(state_dir, operation="outer"):
                def attempt():
                    try:
                        with single_instance_scope(state_dir, operation="thread"):
                            entered.append("entered")
                    except OwnerBusyError:
                        entered.append("busy")

                thread = threading.Thread(target=attempt)
                thread.start()
                thread.join()

        self.assertEqual(entered, ["busy"])

    def test_owner_can_delegate_only_to_explicit_simulation_worker(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with single_instance_scope(state_dir, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()
                result = []

                def use_delegation():
                    try:
                        delegation.validate(state_dir)
                    except OwnerBusyError:
                        result.append("busy")
                    else:
                        result.append("authorized")

                thread = threading.Thread(target=use_delegation)
                thread.start()
                thread.join()

            self.assertEqual(result, ["authorized"])

    def test_delegation_expires_when_owner_is_released(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with single_instance_scope(state_dir, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()
            with self.assertRaises(OwnerBusyError):
                delegation.validate(state_dir)

    def test_delegation_is_bound_to_state_dir(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with tempfile.TemporaryDirectory() as other_state_dir:
                with single_instance_scope(state_dir, operation="outer") as owner:
                    delegation = owner.delegate_simulation_worker()
                    with self.assertRaises(OwnerBusyError):
                        delegation.validate(other_state_dir)

    def test_delegation_is_not_a_general_owner_reentry(self):
        with tempfile.TemporaryDirectory() as state_dir:
            with single_instance_scope(state_dir, operation="outer") as owner:
                delegation = owner.delegate_simulation_worker()
                result = []

                def attempt():
                    try:
                        with single_instance_scope(state_dir, operation="worker"):
                            result.append("entered")
                    except OwnerBusyError:
                        result.append("busy")

                thread = threading.Thread(target=attempt)
                thread.start()
                thread.join()

            self.assertEqual(result, ["busy"])

    def test_other_process_cannot_enter_state_owner(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as state_dir:
            ready = context.Event()
            release = context.Event()
            result = context.Queue()
            first = context.Process(target=_hold_owner, args=(state_dir, ready, release))
            second = context.Process(target=_try_owner, args=(state_dir, result))
            first.start()
            self.assertTrue(ready.wait(10))
            second.start()
            second.join(10)
            release.set()
            first.join(10)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(result.get(timeout=2), "busy")


if __name__ == "__main__":
    unittest.main()
