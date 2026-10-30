"""A5 host-PID reclaim safety, evidence and cleanup regression tests."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.config import load
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

    def _run_with_outputs(self, outputs, *, kill_rc=0, evidence=None):
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
            patch("ci_core.config.execution_node", return_value={"ssh": self.container_ssh}),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            remote_preflight(
                self.case, {"a5": self.pool},
                ascend_env="/usr/local/Ascend/ascend-toolkit/set_env.sh",
                evidence=evidence,
            )
        return commands

    def test_a5_container_pid_is_only_evidence_host_pid_is_kill_target(self):
        self.assertEqual(process_evidence(A5, [0], indexing="card"), [(0, 2000219)])
        self.assertEqual(process_evidence(EXTERNAL, [0], indexing="card"), [(0, 2341399)])
        self.assertEqual(process_evidence(EMPTY, [0], indexing="card"), [])

    def test_a5_unknown_rows_fail_closed(self):
        for bad in (
            A5.replace("2000219", "NA"),
            A5.replace("2000219", "1"),
            A5.replace("46124", "???"),
            A5.replace("345", "??"),
            A5.replace("python3", ""),
            A5.replace("46124 |", "46124 | extra |"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    process_evidence(bad, [0], indexing="card")

    def test_card_die_mapping_and_container_pid_are_unchanged(self):
        self.assertEqual(process_evidence(A3, [7], indexing="card-die"), [(7, 46124)])
        self.assertEqual(selected_processes(A3, [7]), ["chip 7: pid 46124"])

    def test_external_container_pid_na_kills_only_allocated_host_pid(self):
        evidence = io.StringIO()
        commands = self._run_with_outputs(
            [EXTERNAL, EXTERNAL, EMPTY], evidence=evidence
        )
        self.assertEqual(
            [call for call in commands if call[1].startswith("kill -9 --")],
            [("host-ssh:22", "kill -9 -- 2341399")],
        )
        self.assertTrue(
            all(host == self.container_ssh for host, cmd in commands if "npu-smi info" in cmd)
        )
        lines = evidence.getvalue().splitlines()
        self.assertTrue(any('"host_pid": 2341399' in line and '"container_pid": "NA"' in line for line in lines))
        self.assertTrue(any("RECLAIM_KILL " in line and "host_pid=2341399" in line for line in lines))
        self.assertFalse(any("\\n" in line for line in lines), "literal backslash-n in log")

    def test_container_pid_mismatch_does_not_affect_kill_pid(self):
        commands = self._run_with_outputs([A5, A5, EMPTY])
        self.assertIn(("host-ssh:22", "kill -9 -- 2000219"), commands)
        self.assertFalse(any("kill -9 -- 46124" in cmd for _, cmd in commands))

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
        commands = self._run_with_outputs(
            [EXTERNAL, RESPAWN, RESPAWN, RESPAWN, EMPTY]
        )
        kills = [cmd for _, cmd in commands if cmd.startswith("kill -9 --")]
        self.assertEqual(kills, ["kill -9 -- 2341400"])

    def test_respawn_is_bounded_to_allocated_host_pids(self):
        commands = self._run_with_outputs(
            [EXTERNAL, EXTERNAL, RESPAWN, RESPAWN, EMPTY]
        )
        self.assertEqual(
            [(host, cmd) for host, cmd in commands if cmd.startswith("kill -9 --")],
            [
                ("host-ssh:22", "kill -9 -- 2341399"),
                ("host-ssh:22", "kill -9 -- 2341400"),
            ],
        )
        self.assertFalse(any("2341884" in cmd for _, cmd in commands if "kill -9" in cmd))

    def test_blue_a3_remains_non_preemptive(self):
        self.pool.update(owner="blue", type="ascend-a3", force_reclaim=False, process_indexing="card-die")
        self.case["allocation"][0]["devices"] = [7]
        self.case["docker"] = None
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=A3)) as transport:
            with self.assertRaises(ExternalNpuOccupancy):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertTrue(all(not call.args[1].startswith("kill") for call in transport.call_args_list))

    def test_nonzero_host_kill_with_exited_pid_is_not_failure(self):
        # Process exits after confirm but before host kill: ESRCH (rc=1).
        # Re-scan confirms no selected-card occupancy, so reclaim succeeds.
        evidence = io.StringIO()
        commands = self._run_with_outputs(
            [EXTERNAL, EXTERNAL, EMPTY, EMPTY], kill_rc=1, evidence=evidence
        )
        self.assertEqual(
            [call for call in commands if call[1].startswith("kill -9 --")],
            [("host-ssh:22", "kill -9 -- 2341399")],
        )
        self.assertIn("stage=post_kill_nonzero", evidence.getvalue())
        self.assertIn("rc=1", evidence.getvalue())

    def test_nonzero_host_kill_with_same_pid_still_allocated_fails_closed(self):
        # A nonzero kill is a real failure if the *same host PID* remains.
        with self.assertRaisesRegex(RuntimeError, "kill failed"):
            self._run_with_outputs([EXTERNAL, EXTERNAL, EXTERNAL], kill_rc=1)

    def test_nonzero_kill_after_respawn_retries_only_allocated_host_pids(self):
        # A new PID on the allocated card does not make the old ESRCH fatal,
        # but it must be removed before resources are considered idle.
        observations = iter([EXTERNAL, EXTERNAL, RESPAWN,
                             RESPAWN, RESPAWN, EMPTY])
        kills = []
        def transport(host, script, **kwargs):
            if script.startswith("kill -9 --"):
                kills.append((host, script))
                return SimpleNamespace(returncode=1 if len(kills) == 1 else 0, stdout="")
            self.assertEqual(host, self.container_ssh)
            return SimpleNamespace(returncode=0, stdout=next(observations))
        with (
            patch("ci_core.config.execution_node", return_value={"ssh": self.container_ssh}),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            remote_preflight(self.case, {"a5": self.pool})
        self.assertEqual(kills, [
            ("host-ssh:22", "kill -9 -- 2341399"),
            ("host-ssh:22", "kill -9 -- 2341400"),
        ])

    def test_reclaim_succeeds_on_fourth_round_only(self):
        # Four complete scan/confirm rounds; final verification is idle.
        # A successful fourth kill must not fall into for/else exhaustion.
        evidence = io.StringIO()
        commands = self._run_with_outputs(
            [EXTERNAL, EXTERNAL, RESPAWN, RESPAWN, RESPAWN,
             RESPAWN, EXTERNAL, EXTERNAL, EMPTY],
            evidence=evidence,
        )
        kills = [(host, cmd) for host, cmd in commands
                 if cmd.startswith("kill -9 --")]
        self.assertEqual(kills, [
            ("host-ssh:22", "kill -9 -- 2341399"),
            ("host-ssh:22", "kill -9 -- 2341400"),
            ("host-ssh:22", "kill -9 -- 2341400"),
            ("host-ssh:22", "kill -9 -- 2341399"),
        ])
        self.assertIn("round=3 stage=final", evidence.getvalue())
        self.assertIn("RECLAIM_EVIDENCE", evidence.getvalue())

    def test_persistent_respawn_fails_closed_after_four_rounds(self):
        commands = []
        def transport(host, script, **kwargs):
            commands.append((host, script))
            return SimpleNamespace(returncode=0, stdout=EXTERNAL)
        with (
            patch("ci_core.config.execution_node", return_value={"ssh": self.container_ssh}),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            with self.assertRaisesRegex(RuntimeError, "not idle"):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertEqual(
            len([cmd for host, cmd in commands if cmd.startswith("kill -9 --")]), 4
        )
        self.assertTrue(all(host == "host-ssh:22" for host, cmd in commands if cmd.startswith("kill")))

    def test_shared_host_pid_on_unallocated_chip_never_killed(self):
        shared = EXTERNAL.replace("2341884", "2341399")
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=shared)) as transport:
            with self.assertRaisesRegex(ValueError, "spans allocated"):
                remote_preflight({**self.case, "docker": None}, {"a5": self.pool})
        self.assertFalse(any(call.args[1].startswith("kill") for call in transport.call_args_list))

    def test_unallocated_chip_and_other_container_never_killed(self):
        # A second chip's container PID must not be used for signalling either.
        commands = self._run_with_outputs([EXTERNAL, EXTERNAL, EMPTY])
        self.assertNotIn(("host-ssh:22", "kill -9 -- 2341884"), commands)

    def test_config_rejects_blue_or_a3_force_reclaim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/workers.json").write_text(
                json.dumps({"blue": {"enabled": True}, "yellow": {"enabled": True}})
            )
            for owner, kind in [("blue", "ascend-a5"), ("yellow", "ascend-a3")]:
                (root / "config/pools.json").write_text(
                    json.dumps({"p": {"owner": owner, "type": kind, "force_reclaim": True, "nodes": []}})
                )
                with self.assertRaises(ValueError):
                    load(root, "yellow")

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
                **self.case, "enabled": True, "kind": "npu_smoke",
                "resources": {"nodes": 1, "devices_per_node": 1},
                "execution_timeout_seconds": 30,
            }
            run = {
                "created_at": "2026-10-31T00:00:00Z", "id": 42,
                "name": "Smoke", "run_number": 1,
                "head_sha": "a" * 40, "_ci_test_id": "smoke",
            }
            child = Mock()
            child.poll.return_value = 0
            child.returncode = 0
            commands = []
            probes = 0

            def transport(host, script, **kwargs):
                nonlocal probes
                commands.append((host, script))
                if script.startswith("kill -9 --"):
                    return SimpleNamespace(returncode=0, stdout="")
                probes += 1
                # Launch-time check is clear; simulate a different-container
                # process appearing only at cleanup.
                if probes == 1:
                    return SimpleNamespace(returncode=0, stdout=EMPTY)
                if cleanup_idle and probes >= 4:
                    return SimpleNamespace(returncode=0, stdout=EMPTY)
                return SimpleNamespace(returncode=0, stdout=EXTERNAL)

            with ExitStack() as stack:
                stack.enter_context(patch("ci_core.executor.prepare_profile", return_value={"ASCEND_SET_ENV_PATH": "/cann"}))
                stack.enter_context(patch("ci_core.scheduler.validate_allocation"))
                stack.enter_context(patch("ci_core.config.execution_node", return_value={"ssh": self.container_ssh}))
                stack.enter_context(patch("ci_core.transport.run", side_effect=transport))
                stack.enter_context(patch("ci_core.executor.broadcast_job", return_value=[]))
                stack.enter_context(patch("ci_core.executor.subprocess.Popen", return_value=child))
                stack.enter_context(patch("ci_core.executor.time.sleep"))
                stack.enter_context(patch("ci_core.ssh_runner.allocations", return_value=[{"position": 0}]))
                stack.enter_context(patch("ci_core.ssh_runner.owned_processes_gone"))
                stack.enter_context(patch("ci_core.executor.metadata", return_value={"host": self.container_ssh, "path": "/log"}))
                stack.enter_context(patch("ci_core.executor.select_remote", return_value=["PASS"]))
                result = execute(case, run, root=root)

            self.assertGreaterEqual(probes, 4)
            self.assertEqual(result.release_verified, cleanup_idle)
            self.assertTrue(
                any(host == "host-ssh:22" and cmd == "kill -9 -- 2341399" for host, cmd in commands)
            )
            self.assertTrue(
                all(host == self.container_ssh for host, cmd in commands if "npu-smi info" in cmd)
            )
            log_text = result.log.read_text()
            self.assertIn("RECLAIM_KILL host=192.0.2.1:22", log_text)
            self.assertIn("host_pid=2341399", log_text)
            if not cleanup_idle:
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("release unverified", log_text)


if __name__ == "__main__":
    unittest.main()
