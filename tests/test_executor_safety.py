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
            "verified_env": {"ASCEND_SET_ENV_PATH": "/cann"},
        }

    def boundaries(self, *, stub_preflight=True):
        stack = ExitStack()
        mocks = SimpleNamespace()
        if stub_preflight:
            stack.enter_context(patch("ci_core.executor.remote_preflight"))
        mocks.broadcast = stack.enter_context(
            patch("ci_core.executor.broadcast_job", return_value=[])
        )
        mocks.launch = stack.enter_context(patch("ci_core.executor.subprocess.Popen"))
        mocks.owned = stack.enter_context(
            patch("ci_core.ssh_runner.owned_processes_gone")
        )
        mocks.revoke = stack.enter_context(
            patch("ci_core.ssh_runner.revoke_remote_lease", return_value={"fixture"})
        )
        mocks.cancel = stack.enter_context(patch("ci_core.ssh_runner.cancel"))
        stack.enter_context(
            patch(
                "ci_core.ssh_runner.allocations",
                return_value=[{"position": 0, "ssh": "fixture", "host_id": "fixture"}],
            )
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
        for stage in ("before", "broadcast"):
            with self.subTest(stage=stage):
                self.cancel.clear()
                stack, mocks = self.boundaries()
                with stack:
                    if stage == "before":
                        self.cancel.set()
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
                self.assertEqual(mocks.cancel.call_count, 1 if cleanup_verified else 3)

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
            stack.enter_context(
                patch(
                    "ci_core.router.verify_definition",
                    return_value={"env_vars": {"ASCEND_SET_ENV_PATH": "/fixture/cann"}},
                )
            )
            router = Router(self.root, "blue", self.service, "reporter")
            stack.callback(router.close)
            header = "| NPU ID | Chip ID | Process id in container |\n"

            def transport(*args, **kwargs):
                output = (
                    header
                    + (
                        "| 0 0 | 2341399 |\n"
                        if busy
                        else "| No running processes found in NPU 0 |\n"
                    )
                    + "+===========================+\n"
                )
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


class TestTransientSshRetry(unittest.TestCase):
    def test_transport_command_uses_30s_connect_timeout(self):
        from ci_core import transport

        cmd = transport.command("node-a", "echo ok")
        self.assertIn("ConnectTimeout=30", cmd)

    def test_execution_node_retries_once_on_timeout(self):
        import json
        import subprocess
        from unittest.mock import patch

        from ci_core.config import execution_node

        node = {
            "ssh": "node-a",
            "environments": {
                "img": {
                    "container_id": "a" * 64,
                    "image_id": "sha256:1234",
                    "image_ref": "repo/img:tag",
                }
            },
        }
        calls = []

        def fake_run(host, script, *, timeout=90, check=True, stdin=None):
            calls.append((host, script, timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(["ssh"], timeout)
            if "docker inspect" in script:
                return subprocess.CompletedProcess(
                    ["ssh"],
                    0,
                    stdout=json.dumps(
                        {
                            "Name": "/c",
                            "Id": "a" * 64,
                            "Image": "sha256:1234",
                            "State": {"Running": True},
                        }
                    ),
                    stderr="",
                )
            if "docker image inspect" in script:
                return subprocess.CompletedProcess(
                    ["ssh"], 0, stdout="sha256:1234\n", stderr=""
                )
            return subprocess.CompletedProcess(["ssh"], 0, stdout="0\n", stderr="")

        with patch("ci_core.config.run", side_effect=fake_run):
            bound = execution_node(node, "img")

        self.assertEqual(bound["ssh"], "node-a/" + ("a" * 64))
        self.assertEqual(len(calls), 4)

    def test_stage_retries_archive_upload_once_on_timeout(self):
        import hashlib
        import subprocess
        from unittest.mock import patch

        from ci_core import ssh_runner

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "src.tar.gz"
            payload = b"dummy-tar-payload"
            archive.write_bytes(payload)
            expected_sha256 = hashlib.sha256(payload).hexdigest()
            upload_attempts = []

            def fake_transport_run(host, cmd, *, timeout=90, check=True, stdin=None):
                upload_attempts.append((host, timeout, stdin.read()))
                if len(upload_attempts) == 1:
                    raise subprocess.TimeoutExpired(["ssh"], timeout)
                return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

            def fake_ssh(host, cmd, timeout=90):
                self.assertTrue(
                    "mkdir -p" in cmd or expected_sha256 in cmd or "tar -xzf" in cmd
                )
                return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

            with (
                patch("ci_core.transport.run", side_effect=fake_transport_run),
                patch("ci_core.ssh_runner.ssh", side_effect=fake_ssh),
            ):
                dest = ssh_runner.stage(
                    {"host_id": "h1", "ssh": "h1", "position": 0},
                    "/tmp/base",
                    archive,
                    "abc123def456",
                )

        self.assertEqual(dest, "/tmp/base/node0")
        self.assertEqual(len(upload_attempts), 2)
        self.assertEqual(upload_attempts[1][2], payload)


if __name__ == "__main__":
    unittest.main()
