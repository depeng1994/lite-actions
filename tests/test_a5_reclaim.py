"""A5 PID parsing, selective reclaim, cleanup and trusted configuration regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.config import load
from ci_core.executor import ExternalNpuOccupancy, process_evidence, remote_preflight, execute

A5 = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
+===========================+
| 0 | 2000219 | python3 | 345 | 46124 |
| 1 | 2000220 | python3 | 345 | 99887 |
"""
EMPTY = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
| No running processes found in NPU 0 |
"""

class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.node = {"host_id": "192.0.2.1:22", "ssh": "192.0.2.1:22"}
        self.pool = {"owner": "yellow", "type": "ascend-a5", "process_indexing": "card", "force_reclaim": True, "nodes": [self.node]}
        self.case = {"pool": "a5", "allocation": [{"host_id": self.node["host_id"], "devices": [0]}]}

    def test_a5_container_pid_and_selected_chip(self):
        self.assertEqual(process_evidence(A5, [0], indexing="card"), [(0, 46124)])
        self.assertEqual(process_evidence(EMPTY, [0], indexing="card"), [])

    def test_unknown_row_fails_closed(self):
        with self.assertRaises(ValueError):
            process_evidence(A5.replace("46124", "???"), [0], indexing="card")

    def test_reclaim_kills_only_selected_container_pid(self):
        commands = []
        outputs = iter([A5, A5, EMPTY])
        def run(host, cmd, **kw):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout=next(outputs) if "npu-smi" in cmd else "")
        with patch("ci_core.transport.run", side_effect=run):
            remote_preflight(self.case, {"a5": self.pool})
        self.assertEqual([x for x in commands if x.startswith("kill")], ["kill -9 -- 46124"])

    def test_blue_does_not_preempt(self):
        self.pool.update(owner="blue", type="ascend-a3", force_reclaim=False)
        self.pool["process_indexing"] = "card"
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=A5)):
            with self.assertRaises(ExternalNpuOccupancy):
                remote_preflight(self.case, {"a5": self.pool})

    def test_failed_kill_fails_closed(self):
        def run(host, cmd, **kw):
            return SimpleNamespace(returncode=1 if cmd.startswith("kill") else 0, stdout=A5)
        with patch("ci_core.transport.run", side_effect=run):
            with self.assertRaises(RuntimeError):
                remote_preflight(self.case, {"a5": self.pool})

    def test_persistent_process_fails_closed(self):
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=A5)):
            with self.assertRaises(RuntimeError):
                remote_preflight(self.case, {"a5": self.pool})

    def test_shared_pid_outside_allocation_never_killed(self):
        shared = A5.replace("99887", "46124")
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=shared)) as run:
            with self.assertRaises(ValueError):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertFalse(any(c.args[1].startswith("kill") for c in run.call_args_list))

    def test_config_rejects_blue_and_a3_force_reclaim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/workers.json").write_text(json.dumps({"blue": {"enabled": True}, "yellow": {"enabled": True}}))
            for owner, kind in [("blue", "ascend-a5"), ("yellow", "ascend-a3")]:
                (root / "config/pools.json").write_text(json.dumps({"p": {"owner": owner, "type": kind, "force_reclaim": True, "nodes": []}}))
                with self.assertRaises(ValueError):
                    load(root, "yellow")


    def test_cleanup_rechecks_devices_before_verified_release(self):
        from contextlib import ExitStack
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/pools.json").write_text(json.dumps({"a5": self.pool}))
            case = dict(self.case, enabled=True, docker=None, kind="npu_smoke", resources={"nodes": 1, "devices_per_node": 1}, execution_timeout_seconds=30)
            run = {"created_at": "2026-10-31T00:00:00Z", "id": 42, "name": "Smoke", "run_number": 1, "head_sha": "a" * 40, "_ci_test_id": "smoke"}
            child = Mock()
            child.poll.return_value = 0
            child.returncode = 0
            for cleanup_idle in (True, False):
                calls = []
                def preflight(*args, **kwargs):
                    calls.append(kwargs)
                    if len(calls) > 1 and not cleanup_idle:
                        raise RuntimeError("still occupied")
                with ExitStack() as stack:
                    stack.enter_context(patch("ci_core.executor.prepare_profile", return_value={"ASCEND_SET_ENV_PATH": "/cann"}))
                    stack.enter_context(patch("ci_core.scheduler.validate_allocation"))
                    stack.enter_context(patch("ci_core.executor.remote_preflight", side_effect=preflight))
                    stack.enter_context(patch("ci_core.executor.broadcast_job", return_value=[]))
                    stack.enter_context(patch("ci_core.executor.subprocess.Popen", return_value=child))
                    stack.enter_context(patch("ci_core.executor.time.sleep"))
                    stack.enter_context(patch("ci_core.ssh_runner.allocations", return_value=[{"position": 0}]))
                    stack.enter_context(patch("ci_core.ssh_runner.owned_processes_gone"))
                    stack.enter_context(patch("ci_core.config.execution_node", return_value={"ssh": "host/container"}))
                    stack.enter_context(patch("ci_core.executor.metadata", return_value={"host": "host/container", "path": "/log"}))
                    stack.enter_context(patch("ci_core.executor.select_remote", return_value=["PASS"]))
                    result = execute(case, run, root=root)
                self.assertGreaterEqual(len(calls), 2)
                self.assertEqual(result.release_verified, cleanup_idle)
                if not cleanup_idle:
                    self.assertNotEqual(result.exit_code, 0)

if __name__ == "__main__":
    unittest.main()
