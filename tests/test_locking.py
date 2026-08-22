import os
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from awewarm.locking import LockBusy, local_lock_paths, process_lock


def _try_lock_in_child(path, result):
    try:
        with process_lock(path, timeout_seconds=0):
            result.put("acquired")
    except LockBusy:
        result.put("busy")


class ProcessLockTests(unittest.TestCase):
    def test_local_locks_cover_both_config_and_state_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config" / "config.json"
            state = root / "state" / "state.json"
            with mock.patch.dict(os.environ, {
                "AWEWARM_CONFIG": str(config),
                "AWEWARM_STATE": str(state),
            }):
                self.assertEqual(
                    local_lock_paths(),
                    sorted([config.with_name("awewarm.lock"), state.with_name("awewarm.lock")]),
                )

    def test_second_holder_fails_while_first_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "awewarm.lock"
            with process_lock(path, timeout_seconds=0):
                with self.assertRaises(LockBusy):
                    with process_lock(path, timeout_seconds=0):
                        self.fail("a second holder entered the critical section")

    def test_child_process_cannot_enter_while_parent_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "awewarm.lock"
            context = multiprocessing.get_context("spawn")
            result = context.Queue()
            with process_lock(path, timeout_seconds=0):
                child = context.Process(target=_try_lock_in_child, args=(path, result))
                child.start()
                self.assertEqual(result.get(timeout=5), "busy")
                child.join(timeout=5)
            self.assertEqual(child.exitcode, 0)

    def test_lock_can_be_acquired_again_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "awewarm.lock"
            with process_lock(path, timeout_seconds=0):
                pass
            with process_lock(path, timeout_seconds=0):
                pass
