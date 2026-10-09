"""CPU-only contract checks for shared A3/A5 N-node SSH execution."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_core.config import load_config
from ci_core import ssh_runner

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = "2026-10-09_12-13-14_run-123_attempt-1"


class SharedSshRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pools = json.loads((ROOT / "config/pools.json").read_text())
        cls.cases = load_config(ROOT)

    def test_same_runner_used_for_a3_and_a5(self):
        for name, count in (("a3-8p", 1), ("a3-16p", 2), ("a5-64p", 8)):
            with self.subTest(case=name):
                case = self.cases[name]
                self.assertEqual(case["runner"]["kind"], "ssh")
                nodes = ssh_runner.allocations(case, self.pools)
                self.assertEqual(len(nodes), count)
                self.assertEqual(sum(len(node["devices"]) for node in nodes), count * 8)
                for node in nodes:
                    dest = f"/mnt/share/ci_tests/{RUN_DIR}/node{node['position']}"
                    cmd = ssh_runner.remote_command(node, nodes, case["runner"],
                                                     dest, RUN_DIR, 90)
                    syntax = subprocess.run(["bash", "-n", "-c", cmd],
                                             capture_output=True)
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
                    self.assertIn("LITE_CI_JOB_ID", cmd)
                    self.assertIn("run.log", cmd)
                    self.assertIn("job.pgid", cmd)
                    self.assertIn(f"NNODES={count}", cmd)
                    self.assertNotIn("github.com", cmd)

    def test_distributed_nodes_use_same_ordered_hccl_ips(self):
        case = self.cases["a5-64p"]
        nodes = ssh_runner.allocations(case, self.pools)
        ips = ",".join(f"7.246.125.{n}" for n in range(19,27))
        for node in nodes:
            cmd = ssh_runner.remote_command(node, nodes, case["runner"],
                                             "/mnt/share/ci_tests/test/node0", "test", 90)
            self.assertIn(ips, cmd)
            self.assertIn("NGPU=8", cmd)
            self.assertIn("run_a5_64p_multinode_tests", cmd)

    def test_source_commit_is_always_exact(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "invalid commit SHA"):
                ssh_runner.model_archive("main", Path(td))
            with self.assertRaisesRegex(ValueError, "unsafe run"):
                ssh_runner.run(self.cases["a3-8p"], "a"*40, "../unsafe")

    def test_reserved_node_environment_cannot_be_overridden(self):
        from shutil import copyfile
        import copy
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            copyfile(ROOT / "config/pools.json", root / "config/pools.json")
            cases = json.loads((ROOT / "config/pipelines.json").read_text())
            cases["a3-16p"]["runner"]["env"]["NODE_IPS"] = "127.0.0.1"
            (root / "config/pipelines.json").write_text(json.dumps(cases))
            with self.assertRaisesRegex(ValueError, "invalid fixed runner environment"):
                load_config(root)

    def test_staging_error_prevents_any_launch(self):
        case = self.cases["a3-16p"]
        with patch.object(ssh_runner, "model_archive", return_value=Path("/no/file")), \
             patch.object(ssh_runner, "stage", side_effect=RuntimeError("stage error")), \
             patch.object(ssh_runner, "ssh") as remote:
            with self.assertRaisesRegex(RuntimeError, "staging failed"):
                ssh_runner.run(case, "a" * 40, RUN_DIR)
            remote.assert_not_called()

    def test_shell_launch_uses_owned_process_group_and_can_be_terminated(self):
        import os
        import re
        import signal
        import time
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            (dest / "repo").mkdir()
            stamp = "local-ci-pgid-test"
            node = {"position": 0, "host_id": "local", "node_ip": "127.0.0.1",
                    "ssh": "localhost", "devices": [0]}
            runner = {"module": "tests.integration_tests.placeholder", "mode": "single"}
            cmd = ssh_runner.remote_command(node, [node], runner, str(dest), stamp, 90)
            cmd = cmd.replace("python3 -u -m tests.integration_tests.placeholder "
                              + ssh_runner.q(str(dest / "output")), "sleep 30")
            proc = subprocess.Popen(["bash", "-lc", cmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            pid = None
            try:
                for _ in range(80):
                    path = dest / "job.pgid"
                    if path.exists() and path.read_text().strip():
                        pid = int(path.read_text().strip())
                        break
                    time.sleep(.05)
                self.assertIsNotNone(pid, "job.pgid never written")
                self.assertEqual(os.getpgid(pid), pid)
                env = Path(f"/proc/{pid}/environ").read_bytes()
                self.assertIn(f"LITE_CI_JOB_ID={stamp}".encode()+b"\x00", env)
                os.killpg(pid, signal.SIGTERM)
                self.assertNotEqual(proc.wait(timeout=8), 0)
                self.assertTrue((dest / "exit_code.txt").is_file())
            finally:
                if pid is not None and proc.poll() is None:
                    os.killpg(pid, signal.SIGKILL)
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=8)

    def test_failed_node_cancels_siblings_and_preserves_causal_error(self):
        import threading
        import contextlib
        import io
        import time
        from concurrent.futures import TimeoutError
        case = self.cases["a3-16p"]
        canceled = threading.Event()
        cleaned = []
        nodes = ssh_runner.allocations(case, self.pools)

        def staged(node, base, _archive, _sha):
            return f"{base}/node{node['position']}"

        def fake_ssh(host, command, **kwargs):
            if host == nodes[0]["ssh"]:
                if not canceled.wait(timeout=3):
                    raise AssertionError("failed to cancel stuck sibling promptly")
                return subprocess.CompletedProcess([host,command],143,"","")
            return subprocess.CompletedProcess([host,command],1,"","")

        def fake_cancel(node, dest, stamp):
            cleaned.append(node["host_id"])
            canceled.set()

        output = io.StringIO()
        with patch.object(ssh_runner, "model_archive", return_value=Path("/virtual/source.tar.gz")), \
             patch.object(ssh_runner, "stage", side_effect=staged), \
             patch.object(ssh_runner, "ssh", side_effect=fake_ssh), \
             patch.object(ssh_runner, "cancel", side_effect=fake_cancel), \
             contextlib.redirect_stdout(output):
            rc = ssh_runner.run(case, "a"*40, RUN_DIR)
        self.assertEqual(rc, 1)
        self.assertIn(nodes[0]["host_id"], cleaned)
        self.assertIn(f"RESULT_HOST: {nodes[1]['ssh']}", output.getvalue())

    def test_a5_requires_verified_runtime_paths_before_enable(self):
        import copy
        invalid = copy.deepcopy(self.pools)
        case = self.cases["a5-64p"]
        nodes = ssh_runner.allocations(case, invalid)
        self.assertTrue(all(not node.get("hf_assets_path") for node in nodes))
        self.assertTrue(all(not node.get("ckpt_init_path") for node in nodes))


if __name__ == "__main__":
    unittest.main()
