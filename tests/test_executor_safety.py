"""CPU checks for cancellation and external occupancy through the real executor."""

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.executor import execute
from ci_core.resources import reserve
from ci_core.router import Router, task_key
from tests.scenarios import Service, definition, write_workspace


class ExecutorSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_workspace(self.root)
        self.service = Service([definition("smoke", kind="npu_smoke")], self.root)
        self.cancel = threading.Event()
        self.run = {
            **self.service.runs[20],
            "_ci_test_id": "smoke",
            "_ci_cancel_event": self.cancel,
        }
        self.case = {
            "enabled": True,
            "pool": "pool_blue",
            "kind": "npu_smoke",
            "docker": "training",
            "resources": {"nodes": 1, "devices_per_node": 2},
            "allocation": [{"host_id": "192.0.2.10:22", "devices": [0, 1]}],
            "execution_timeout_seconds": 90,
        }

    def boundaries(self, *, stub_preflight=True):
        stack = ExitStack()
        mocks = SimpleNamespace()
        mocks.profile = stack.enter_context(
            patch(
                "ci_core.executor.prepare_profile",
                return_value={"ASCEND_SET_ENV_PATH": "/cann"},
            )
        )
        if stub_preflight:
            mocks.preflight = stack.enter_context(
                patch("ci_core.executor.remote_preflight")
            )
        mocks.broadcast = stack.enter_context(
            patch("ci_core.executor.broadcast_job", return_value=[])
        )
        mocks.launch = stack.enter_context(patch("ci_core.executor.subprocess.Popen"))
        mocks.owned = stack.enter_context(
            patch("ci_core.ssh_runner.owned_processes_gone")
        )
        stack.enter_context(
            patch("ci_core.ssh_runner.allocations", return_value=[{"position": 0}])
        )
        stack.enter_context(
            patch("ci_core.config.execution_node", return_value={"ssh": "fixture"})
        )
        stack.enter_context(
            patch(
                "ci_core.executor.metadata",
                return_value={"host": "fixture", "path": "/log"},
            )
        )
        stack.enter_context(
            patch("ci_core.executor.select_remote", return_value=["fixture PASS"])
        )
        stack.enter_context(patch("ci_core.executor.time.sleep"))
        return stack, mocks

    def test_cancel_during_preparation_never_starts_child(self):
        for stage in ("before", "profile", "preflight", "broadcast"):
            with self.subTest(stage=stage):
                self.cancel.clear()
                stack, mocks = self.boundaries()
                with stack:
                    if stage == "before":
                        self.cancel.set()
                    elif stage == "profile":

                        def profile(*args, **kwargs):
                            self.cancel.set()
                            return {"ASCEND_SET_ENV_PATH": "/cann"}

                        mocks.profile.side_effect = profile
                    elif stage == "preflight":
                        mocks.preflight.side_effect = lambda *args, **kwargs: (
                            self.cancel.set()
                        )
                    else:

                        def broadcast(case, pools, run, phase, **kwargs):
                            if phase == "begin":
                                self.cancel.set()
                            return []

                        mocks.broadcast.side_effect = broadcast
                    result = execute(self.case, self.run, root=self.root)
                self.assertEqual(result.exit_code, 130)
                self.assertTrue(result.release_verified)
                mocks.launch.assert_not_called()
                mocks.owned.assert_not_called()
                if stage == "broadcast":
                    self.assertEqual(mocks.broadcast.call_args.kwargs["released"], True)

    def test_cancel_at_exit_never_reports_success_and_cleanup_remains_required(self):
        for cleanup_verified in (True, False):
            with self.subTest(cleanup_verified=cleanup_verified):
                self.cancel.clear()
                stack, mocks = self.boundaries()
                with stack:
                    child = Mock(returncode=0)
                    child.poll.side_effect = lambda: (self.cancel.set(), 0)[1]
                    mocks.launch.return_value = child
                    if not cleanup_verified:
                        mocks.owned.side_effect = RuntimeError("cleanup unknown")
                    result = execute(self.case, self.run, root=self.root)
                self.assertEqual(result.exit_code, 130 if cleanup_verified else 125)
                self.assertEqual(result.release_verified, cleanup_verified)
                mocks.launch.assert_called_once()
                self.assertEqual(
                    mocks.broadcast.call_args.kwargs["released"], cleanup_verified
                )

    def test_external_occupancy_requeues_and_tries_another_allocation(self):
        busy = True
        stack, mocks = self.boundaries(stub_preflight=False)
        with stack:
            stack.enter_context(
                patch.dict(
                    "os.environ",
                    LITE_ACTIONS_ALLOWED_ACTORS="maintainer",
                    LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "leases"),
                    LITE_ACTIONS_STATE_DIR="",
                )
            )
            stack.enter_context(self.service.connected())
            stack.enter_context(patch("ci_core.router.verify_definition"))
            router = Router(self.root, "blue", self.service, "reporter")
            stack.callback(router.close)
            header = "| NPU ID | Chip ID | Process id in container |\n"

            def transport(*args, **kwargs):
                output = header + ("| 0 0 | 2341399 |\n" if busy else "")
                return SimpleNamespace(returncode=0, stdout=output)

            def launch(*args, **kwargs):
                kwargs["stdout"].write("CASE_RESULT smoke PASS 0\n")
                return Mock(poll=Mock(return_value=0), returncode=0)

            mocks.launch.side_effect = launch
            with patch("ci_core.transport.run", side_effect=transport):
                router.receive([self.service.runs[20]])
                router.pump()
                for future, _, _ in router.active.values():
                    future.result(10)
                key = task_key(
                    self.service.runs[20], self.service.plans[20]["cases"][0]
                )
                router.state.update(key, unavailable=[{"until": 0, "allocation": []}])
                devices = [
                    {"host_id": host, "devices": [0, 1, 2, 3]}
                    for host in ("192.0.2.10:22", "192.0.2.11:22")
                ]
                with reserve(self.root / "leases/devices", devices):
                    router.pump()
                    record = router.state.read()[key]
                    self.assertEqual(record["phase"], "QUEUED")
                    self.assertFalse(record["quarantined"])
                    self.assertEqual(record["allocation"], [])
                    self.assertEqual(len(record["unavailable"]), 1)
                    avoided = record["unavailable"][0]["allocation"]
                    mocks.launch.assert_not_called()

                busy = False
                router.pump()
                for future, _, _ in router.active.values():
                    future.result(10)
                router.pump()
                router.flush()
                record = router.state.read()[key]
                self.assertNotEqual(record["allocation"], avoided)
                self.assertEqual(record["report"]["status"], "PASS")
                self.assertFalse(record["quarantined"])
                mocks.launch.assert_called_once()

    def test_cancel_with_unverified_cleanup_quarantines_allocation(self):
        entered, release = threading.Event(), threading.Event()
        stack, mocks = self.boundaries()
        with stack:
            stack.enter_context(
                patch.dict(
                    "os.environ",
                    LITE_ACTIONS_ALLOWED_ACTORS="maintainer",
                    LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "leases"),
                    LITE_ACTIONS_STATE_DIR="",
                )
            )
            stack.enter_context(self.service.connected())
            stack.enter_context(patch("ci_core.router.verify_definition"))
            router = Router(self.root, "blue", self.service, "reporter")
            stack.callback(router.close)
            child = Mock(returncode=0)

            def finish_after_cancel():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("fixture wait timed out")
                return 0

            child.poll.side_effect = finish_after_cancel
            mocks.launch.return_value = child
            mocks.owned.side_effect = RuntimeError("cleanup unknown")
            try:
                router.receive([self.service.runs[20]])
                router.pump()
                self.assertTrue(entered.wait(10))
                key = task_key(
                    self.service.runs[20], self.service.plans[20]["cases"][0]
                )
                router.active[key][1].set()
                release.set()
                router.active[key][0].result(10)
                record = router.state.read()[key]
                self.assertEqual(record["report"]["status"], "CANCELLED")
                self.assertFalse(record["report"]["cleanup_verified"])
                self.assertTrue(record["quarantined"])
                remaining = [
                    {"host_id": "192.0.2.10:22", "devices": [2, 3]},
                    {"host_id": "192.0.2.11:22", "devices": [0, 1, 2, 3]},
                ]
                with reserve(self.root / "leases/devices", remaining):
                    self.assertIsNone(router.claim(self.case))
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
