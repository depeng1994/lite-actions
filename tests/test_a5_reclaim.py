"""A5 host-PID reclaim safety, evidence and cleanup regression tests."""

import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.executor import (
    ExternalNpuOccupancy,
    execute,
    process_evidence,
    remote_preflight,
    selected_processes,
)

A5 = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
+===========================+
| 0 | 2000219 | python3 | 345 | 46124 |
| 1 | 2000220 | python3 | 345 | 99887 |
"""
EXTERNAL = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
+===========================+
| 0 | 2341399 | VLLMWorker_TP | 82070 | NA |
| 1 | 2341884 | VLLMWorker_TP | 81877 | NA |
"""
RESPAWN = EXTERNAL.replace("2341399", "2341400")
EMPTY = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
| No running processes found in NPU 0 |
"""
A3 = """| NPU ID | Chip ID | Process id in container |
| 3 1 | 46124 |
+===========================+
"""


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "host_id": "192.0.2.1:22",
            "ssh": "worker-ssh:22",
            "host_ssh": "host-ssh:22",
        }
        self.pool = {
            "owner": "yellow",
            "type": "ascend-a5",
            "process_indexing": "card",
            "force_reclaim": True,
            "nodes": [self.node],
        }
        self.case = {
            "pool": "a5",
            "allocation": [{"host_id": self.node["host_id"], "devices": [0]}],
            "docker": "vllm-kimi-wangjinyin",
        }
        self.container_ssh = "worker-ssh:22/" + ("a" * 64)

    def _run_with_outputs(self, outputs, *, kill_rc=0):
        commands = []
        snapshots = iter(outputs)

        def transport(host, script, **kwargs):
            commands.append((host, script))
            if script.startswith("kill -9 -- "):
                return SimpleNamespace(returncode=kill_rc, stdout="")
            self.assertEqual(host, self.container_ssh)
            self.assertIn("npu-smi info", script)
            return SimpleNamespace(returncode=0, stdout=next(snapshots))

        with (
            patch(
                "ci_core.config.execution_node",
                return_value={"ssh": self.container_ssh},
            ),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            remote_preflight(
                self.case,
                {"a5": self.pool},
                ascend_env="/usr/local/Ascend/ascend-toolkit/set_env.sh",
            )
        return commands

    def test_a5_unknown_rows_fail_closed(self):
        for bad in (
            A5.replace("2000219", "NA"),
            A5.replace("2000219", "1"),
            A5.replace("46124 |", "46124 | extra |"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    process_evidence(bad, [0], indexing="card")

    def test_card_die_mapping_and_container_pid_are_unchanged(self):
        self.assertEqual(process_evidence(A3, [7], indexing="card-die"), [(7, 46124)])
        self.assertEqual(selected_processes(A3, [7]), ["chip 7: pid 46124"])

    def test_reclaim_targets_allocated_host_pid_not_container_pid(self):
        for output, host_pid in ((EXTERNAL, 2341399), (A5, 2000219)):
            with self.subTest(host_pid=host_pid):
                commands = self._run_with_outputs([output, output, EMPTY])
                self.assertEqual(
                    [call for call in commands if call[1].startswith("kill -9 --")],
                    [("host-ssh:22", f"kill -9 -- {host_pid}")],
                )

    def test_fallback_host_ssh_without_host_ssh_key(self):
        self.node.pop("host_ssh")
        commands = self._run_with_outputs([EXTERNAL, EXTERNAL, EMPTY])
        self.assertIn(("worker-ssh:22", "kill -9 -- 2341399"), commands)

    def test_reject_container_suffixed_kill_host(self):
        self.node["host_ssh"] = self.container_ssh
        with patch("ci_core.transport.run") as transport:
            with self.assertRaisesRegex(ValueError, "host-only"):
                remote_preflight(self.case, {"a5": self.pool})
        transport.assert_not_called()

    def test_no_kill_if_pid_changed_before_confirmation(self):
        commands = self._run_with_outputs([EXTERNAL, RESPAWN, RESPAWN, RESPAWN, EMPTY])
        kills = [cmd for _, cmd in commands if cmd.startswith("kill -9 --")]
        self.assertEqual(kills, ["kill -9 -- 2341400"])

    def test_respawn_is_bounded_to_allocated_host_pids(self):
        commands = self._run_with_outputs([EXTERNAL, EXTERNAL, RESPAWN, RESPAWN, EMPTY])
        self.assertEqual(
            [(host, cmd) for host, cmd in commands if cmd.startswith("kill -9 --")],
            [
                ("host-ssh:22", "kill -9 -- 2341399"),
                ("host-ssh:22", "kill -9 -- 2341400"),
            ],
        )
        self.assertFalse(
            any("2341884" in cmd for _, cmd in commands if "kill -9" in cmd)
        )

    def test_node_wait_overrides_legacy_pool_reclaim(self):
        self.node["occupancy_policy"] = "wait"
        with (
            patch(
                "ci_core.config.execution_node",
                return_value={"ssh": self.container_ssh},
            ),
            patch(
                "ci_core.transport.run",
                return_value=SimpleNamespace(returncode=0, stdout=EXTERNAL),
            ) as transport,
        ):
            with self.assertRaises(ExternalNpuOccupancy):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertFalse(
            any(call.args[1].startswith("kill") for call in transport.call_args_list)
        )

    def test_cancel_stops_legacy_preemption_in_preflight_and_cleanup(self):
        for cleanup in (False, True):
            cancel, scans = threading.Event(), []
            if cleanup:
                cancel.set()

            def transport(host, command, **kwargs):
                scans.append(command)
                if len(scans) == 2 and not cleanup:
                    cancel.set()
                return SimpleNamespace(returncode=0, stdout=EXTERNAL)

            with (
                self.subTest(cleanup=cleanup),
                patch(
                    "ci_core.config.execution_node",
                    return_value={"ssh": self.container_ssh},
                ),
                patch("ci_core.transport.run", side_effect=transport),
                self.assertRaises(
                    ExternalNpuOccupancy if cleanup else InterruptedError
                ),
            ):
                remote_preflight(
                    self.case, {"a5": self.pool}, cleanup=cleanup, cancel=cancel
                )
            self.assertFalse(any(command.startswith("kill") for command in scans))

    def test_nonzero_host_kill_with_exited_pid_is_not_failure(self):
        # Process exits after confirm but before host kill: ESRCH (rc=1).
        # Re-scan confirms no selected-card occupancy, so reclaim succeeds.
        commands = self._run_with_outputs([EXTERNAL, EXTERNAL, EMPTY, EMPTY], kill_rc=1)
        self.assertEqual(
            [call for call in commands if call[1].startswith("kill -9 --")],
            [("host-ssh:22", "kill -9 -- 2341399")],
        )

    def test_nonzero_host_kill_with_same_pid_still_allocated_fails_closed(self):
        # A nonzero kill is a real failure if the *same host PID* remains.
        with self.assertRaisesRegex(RuntimeError, "kill failed"):
            self._run_with_outputs([EXTERNAL, EXTERNAL, EXTERNAL], kill_rc=1)

    def test_persistent_respawn_fails_closed(self):
        commands = []

        def transport(host, script, **kwargs):
            commands.append((host, script))
            return SimpleNamespace(returncode=0, stdout=EXTERNAL)

        with (
            patch(
                "ci_core.config.execution_node",
                return_value={"ssh": self.container_ssh},
            ),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            with self.assertRaisesRegex(RuntimeError, "not idle"):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertTrue(any(cmd.startswith("kill -9 --") for _, cmd in commands))
        self.assertTrue(
            all(
                host == "host-ssh:22"
                for host, cmd in commands
                if cmd.startswith("kill")
            )
        )

    def test_shared_host_pid_on_unallocated_chip_never_killed(self):
        shared = EXTERNAL.replace("2341884", "2341399")
        with patch(
            "ci_core.transport.run",
            return_value=SimpleNamespace(returncode=0, stdout=shared),
        ) as transport:
            with self.assertRaisesRegex(ValueError, "spans allocated"):
                remote_preflight({**self.case, "docker": None}, {"a5": self.pool})
        self.assertFalse(
            any(call.args[1].startswith("kill") for call in transport.call_args_list)
        )

    def test_cleanup_reclaims_host_pid_and_verifies_release(self):
        self._execute_with_cleanup(cleanup_idle=True)

    def test_cleanup_respawn_keeps_release_unverified(self):
        self._execute_with_cleanup(cleanup_idle=False)

    def _execute_with_cleanup(self, *, cleanup_idle):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/pools.json").write_text(json.dumps({"a5": self.pool}))
            case = {
                **self.case,
                "enabled": True,
                "kind": "npu_smoke",
                "resources": {"nodes": 1, "devices_per_node": 1},
                "execution_timeout_seconds": 30,
                "verified_env": {"ASCEND_SET_ENV_PATH": "/cann"},
            }
            run = {
                "created_at": "2026-10-31T00:00:00Z",
                "id": 42,
                "name": "Smoke",
                "run_number": 1,
                "head_sha": "a" * 40,
                "_ci_test_id": "smoke",
            }
            child = Mock()
            child.poll.return_value = 0
            child.returncode = 0
            commands = []
            first_probe = True
            signalled = False

            def transport(host, script, **kwargs):
                nonlocal first_probe, signalled
                commands.append((host, script))
                if script.startswith("kill -9 --"):
                    signalled = True
                    return SimpleNamespace(returncode=0, stdout="")
                idle = first_probe or (cleanup_idle and signalled)
                first_probe = False
                return SimpleNamespace(returncode=0, stdout=EMPTY if idle else EXTERNAL)

            with ExitStack() as stack:
                stack.enter_context(patch("ci_core.scheduler.validate_allocation"))
                stack.enter_context(
                    patch(
                        "ci_core.config.execution_node",
                        return_value={"ssh": self.container_ssh},
                    )
                )
                stack.enter_context(
                    patch("ci_core.transport.run", side_effect=transport)
                )
                stack.enter_context(
                    patch("ci_core.executor.broadcast_job", return_value=[])
                )
                stack.enter_context(
                    patch("ci_core.executor.subprocess.Popen", return_value=child)
                )
                stack.enter_context(patch("ci_core.executor.time.sleep"))
                stack.enter_context(
                    patch(
                        "ci_core.ssh_runner.allocations", return_value=[{"position": 0}]
                    )
                )
                stack.enter_context(patch("ci_core.ssh_runner.owned_processes_gone"))
                stack.enter_context(
                    patch(
                        "ci_core.executor.metadata",
                        return_value={"host": self.container_ssh, "path": "/log"},
                    )
                )
                stack.enter_context(
                    patch("ci_core.executor.select_remote", return_value=["PASS"])
                )
                result = execute(case, run, root=root)

            self.assertEqual(result.release_verified, cleanup_idle)
            self.assertTrue(
                any(
                    host == "host-ssh:22" and cmd == "kill -9 -- 2341399"
                    for host, cmd in commands
                )
            )
            if not cleanup_idle:
                self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
