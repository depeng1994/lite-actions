"""Unit tests for trusted OS-only worker probes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci_core.worker_runner import load_worker, run_probe


class WorkerRunnerTests(unittest.TestCase):
    def test_depeng_registry_is_enabled_and_identity_pinned(self):
        root = Path(__file__).resolve().parents[1]
        worker = load_worker("depeng", root / "config/workers.json")
        self.assertEqual(worker["transport"], "local")
        self.assertTrue(worker["enabled"])
        self.assertEqual(worker["expected_hostname"], "depeng_us_euler.local")

    def test_unknown_or_disabled_worker_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            path.write_text(json.dumps({"offline": {"transport": "local", "enabled": False}}))
            with self.assertRaisesRegex(ValueError, "not registered and enabled"):
                load_worker("offline", path)
            with self.assertRaisesRegex(ValueError, "not registered and enabled"):
                load_worker("missing", path)

    def test_os_probe_runs_and_verifies_expected_hostname(self):
        worker = {"transport": "local", "expected_hostname": "worker.example"}
        output = "WORKER_HOSTNAME=worker.example\nWORKER_ARCH=x86_64\nPRETTY_NAME=Test OS\n"
        completed = __import__("subprocess").CompletedProcess([], 0, stdout=output, stderr="")
        with patch("ci_core.worker_runner.subprocess.run", return_value=completed) as run, \
             patch("builtins.print") as output_print:
            self.assertEqual(run_probe("depeng", worker), 0)
        self.assertIn("/etc/os-release", run.call_args.args[0][-1])
        self.assertTrue(any("WORKER_STATUS=PASS" in str(call) for call in output_print.call_args_list))

    def test_smoke_result_payload_carries_worker_identity(self):
        from ci_core.config import load_config
        from ci_core.reporting import make_report
        root = Path(__file__).resolve().parents[1]
        case = load_config(root)["dispatcher-smoke"]
        report = make_report(case, {
            "id": 42, "run_attempt": 1, "head_sha": "a" * 40,
        }, 0, ["WORKER_ID=depeng", "WORKER_STATUS=PASS"])
        self.assertEqual(report["worker_id"], "depeng")
        self.assertEqual(report["pipeline"], "npu-smi")
        self.assertEqual(report["status"], "PASS")

    def test_hostname_mismatch_fails(self):
        worker = {"transport": "local", "expected_hostname": "other.example"}
        completed = __import__("subprocess").CompletedProcess(
            [], 0, stdout="WORKER_HOSTNAME=depeng_us_euler.local\n", stderr="")
        with patch("ci_core.worker_runner.subprocess.run", return_value=completed):
            self.assertEqual(run_probe("depeng", worker), 1)


if __name__ == "__main__":
    unittest.main()
