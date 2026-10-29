"""Capability tests for named-worker OS smoke dispatch."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_core import dispatch
from ci_core.config import load_config
from ci_core.reporting import make_report
from ci_core.worker_runner import PROBE, load_worker, run_probe

ROOT = Path(__file__).resolve().parents[1]
OS_OUTPUT = (
    "WORKER_HOSTNAME=worker.example\n"
    "WORKER_ARCH=x86_64\n"
    "WORKER_KERNEL=Linux 6.6.0\n"
    "OS_RELEASE_BEGIN\n"
    'PRETTY_NAME="Test OS"\n'
    "OS_RELEASE_END\n"
    "WORKER_PYTHON=Python 3.11.6\n"
)


class WorkerRunnerTests(unittest.TestCase):
    def test_pipeline_pins_depeng_as_the_smoke_target(self):
        case = load_config(ROOT)["dispatcher-smoke"]
        worker = load_worker("depeng", ROOT / "config/workers.json")
        self.assertEqual(case["runner"], {"kind": "worker", "worker_id": "depeng"})
        self.assertEqual(worker["expected_hostname"], "depeng_us_euler.local")
        self.assertTrue(worker["enabled"])

    def test_unknown_and_disabled_workers_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({
                "offline": {"transport": "local", "enabled": False},
            }))
            for worker_id in ("offline", "missing"):
                with self.subTest(worker_id=worker_id):
                    with self.assertRaisesRegex(ValueError, "not registered and enabled"):
                        load_worker(worker_id, path)

    def test_local_probe_requires_and_reports_os_information(self):
        completed = subprocess.CompletedProcess([], 0, stdout=OS_OUTPUT, stderr="")
        with patch("ci_core.worker_runner.subprocess.run", return_value=completed) as run, \
             patch("builtins.print") as output:
            self.assertEqual(run_probe("depeng", {
                "transport": "local", "expected_hostname": "worker.example",
            }), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["bash", "-lc"])
        self.assertIn("/etc/os-release", command[2])
        self.assertNotIn("npu-smi", command[2])
        printed = "\n".join(str(call) for call in output.call_args_list)
        self.assertIn("WORKER_ID=depeng", printed)
        self.assertIn("WORKER_STATUS=PASS", printed)

    def test_ssh_worker_uses_only_configured_alias_and_keeps_host_key_checks(self):
        completed = subprocess.CompletedProcess([], 0, stdout=OS_OUTPUT, stderr="")
        worker = {"transport": "ssh", "host": "depeng-ssh", "expected_hostname": "worker.example"}
        with patch("ci_core.worker_runner.subprocess.run", return_value=completed) as run, \
             patch("builtins.print"):
            self.assertEqual(run_probe("depeng", worker), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("depeng-ssh", command)
        self.assertIn("BatchMode=yes", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)
        self.assertIn("/etc/os-release", command[-1])

    def test_ssh_alias_is_validated_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({
                "bad": {"transport": "ssh", "enabled": True, "host": "-oProxyCommand=evil"},
            }))
            with self.assertRaisesRegex(ValueError, "trusted SSH config alias"):
                load_worker("bad", path)

    def test_incomplete_os_data_never_reports_pass(self):
        completed = subprocess.CompletedProcess(
            [], 0, stdout="WORKER_HOSTNAME=worker.example\nWORKER_ARCH=x86_64\n", stderr="")
        with patch("ci_core.worker_runner.subprocess.run", return_value=completed), \
             patch("builtins.print") as output:
            self.assertEqual(run_probe("depeng", {"transport": "local"}), 1)
        self.assertTrue(any("incomplete_os_information" in str(call) for call in output.call_args_list))

    def test_wrong_hostname_and_failed_probe_are_failures(self):
        mismatch = subprocess.CompletedProcess([], 0, stdout=OS_OUTPUT, stderr="")
        with patch("ci_core.worker_runner.subprocess.run", return_value=mismatch), \
             patch("builtins.print") as output:
            self.assertEqual(run_probe("depeng", {
                "transport": "local", "expected_hostname": "other.example",
            }), 1)
        self.assertTrue(any("expected_hostname_mismatch" in str(call)
                            for call in output.call_args_list))
        failed = subprocess.CompletedProcess([], 255, stdout="", stderr="ssh unreachable")
        with patch("ci_core.worker_runner.subprocess.run", return_value=failed), \
             patch("builtins.print") as output:
            self.assertEqual(run_probe("depeng", {"transport": "local"}), 255)
        self.assertTrue(any("WORKER_STATUS=FAIL" in str(call)
                            for call in output.call_args_list))

    def test_probe_timeout_is_a_bounded_failure(self):
        with patch("ci_core.worker_runner.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["ssh"], 30)), \
             patch("builtins.print") as output:
            self.assertEqual(run_probe("depeng", {"transport": "ssh", "host": "worker"}), 124)
        self.assertTrue(any("reason=timeout" in str(call)
                            for call in output.call_args_list))

    def test_dispatch_to_worker_produces_identity_bearing_pass_report(self):
        # Exercise the actual dispatcher -> worker subprocess -> OS probe path,
        # not a mocked execute() function. Only the unrelated preflight is stubbed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ci_core").mkdir()
            (root / "config").mkdir()
            shutil.copy2(ROOT / "ci_core/worker_runner.py", root / "ci_core/worker_runner.py")
            (root / "config/pools.json").write_text(json.dumps({
                "local-smoke": {"type": "local", "nodes": []},
            }))
            (root / "config/workers.json").write_text(json.dumps({
                "depeng": {"transport": "local", "enabled": True, "capabilities": ["smoke"]},
            }))
            case = {
                "name": "dispatcher-smoke", "pipeline_id": "npu-smi",
                "pool": "local-smoke", "allocation": [], "timeout_seconds": 15,
                "runner": {"kind": "worker", "worker_id": "depeng"},
            }
            run = {
                "id": 42, "run_attempt": 1, "head_sha": "a" * 40,
                "created_at": "2026-10-30T00:00:00Z", "name": "Dispatcher-Smoke",
            }
            with patch.object(dispatch, "preflight_before_launch"):
                execution = dispatch.execute(case, run, root=root)
            self.assertEqual(execution.exit_code, 0, execution.tail)
            self.assertTrue(any("WORKER_ID=depeng" in line for line in execution.tail))
            self.assertTrue(any("OS_RELEASE_BEGIN" in line for line in execution.tail))
            report = make_report(case, run, execution.exit_code, execution.tail, execution.log)
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["worker_id"], "depeng")
            self.assertEqual(report["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
