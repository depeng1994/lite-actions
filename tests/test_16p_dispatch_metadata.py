"""Offline checks for the 16P coordinator's raw-log location contract."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "legacy_16p_dispatch", ROOT / "legacy/github/scripts/a3-16p-dispatch.py"
)
COORD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COORD)


class SixteenPMetadataTests(unittest.TestCase):
    def test_two_hosts_and_launch_command(self):
        self.assertEqual(len(COORD.HOSTS), 2)
        self.assertEqual(len(COORD.IPS), 2)
        for index, host in enumerate(COORD.HOSTS):
            env = COORD.remote_env("/mnt/share/ci_tests/example", host)
            for value in ("NNODES=2", "NGPU=8", "COMPILE_ENABLE=0", "STEPS=5"):
                self.assertIn(value, env)
            cmd = COORD.node_command(
                f"/mnt/share/ci_tests/example/node{index}", host
            )
            self.assertIn("run_16p_multinode_tests launch", cmd)
            self.assertIn("exit_code.txt", cmd)
            self.assertEqual(subprocess.run(["bash", "-n", "-c", cmd],
                              capture_output=True).returncode, 0)

    def test_coordinator_returns_metadata_without_ssh_log_selection(self):
        cases = [
            ({COORD.HOSTS[0]: 0, COORD.HOSTS[1]: 0}, True,
             "PASS", COORD.HOSTS[0], "/node0/run.log"),
            ({COORD.HOSTS[0]: 0, COORD.HOSTS[1]: 1}, False,
             "FAIL", COORD.HOSTS[1], "/node1/run.log"),
        ]
        for statuses, verified, status, host, suffix in cases:
            with self.subTest(status=status):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    COORD.print_result("/mnt/share/ci_tests/example", statuses,
                                       verified)
                result_line = next(line for line in output.getvalue().splitlines()
                                   if line.startswith("[16P_RESULT] "))
                data = json.loads(result_line.partition("] ")[2])
                self.assertEqual(data["status"], status)
                self.assertEqual(data["log_host"], host)
                self.assertTrue(data["log_file"].endswith(suffix))
                self.assertNotIn("last_20_lines", data)


if __name__ == "__main__":
    unittest.main()
