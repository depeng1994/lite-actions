"""CPU process lifecycle through the real remote supervisor and cleanup commands."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.ssh_runner import cancel, owned_processes_gone, remote_command


@unittest.skipUnless(shutil.which("timeout"), "Requires timeout command")
class DistributedSmokeLaunchTests(unittest.TestCase):
    def test_each_node_receives_its_allocated_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup = root / "set_env.sh"
            setup.write_text("true\n")
            nodes = [
                {
                    "node_ip": f"192.0.2.{i + 10}",
                    "position": i,
                    "devices": list(range(8)),
                }
                for i in range(2)
            ]
            profile = {
                "kind": "npu_smoke",
                "env_vars": {"ASCEND_SET_ENV_PATH": str(setup), "NODE_RANK": "invalid"},
            }
            for node in nodes:
                destination = root / f"node{node['position']}"
                repo = destination / "repo"
                package = repo / "tests/integration_tests/tools/lite_actions"
                package.mkdir(parents=True)
                for folder in (
                    repo / "tests",
                    repo / "tests/integration_tests",
                    repo / "tests/integration_tests/tools",
                    package,
                ):
                    (folder / "__init__.py").write_text("")
                (package / "entrypoint.py").write_text(
                    "import json,os,pathlib\n"
                    "pathlib.Path('rank.json').write_text(json.dumps("
                    "{k: os.environ[k] for k in ('NODE_RANK','NODE_IPS','NNODES','NGPU')}))\n"
                )
                command = remote_command(
                    node,
                    nodes,
                    profile,
                    str(destination),
                    "rank-check",
                    30,
                    "dispatch_smoke_16p",
                )
                subprocess.run(
                    ["bash", "-lc", command],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=40,
                )
                result = json.loads((repo / "rank.json").read_text())
                self.assertEqual(
                    result,
                    {
                        "NODE_RANK": str(node["position"]),
                        "NODE_IPS": "192.0.2.10,192.0.2.11",
                        "NNODES": "2",
                        "NGPU": "8",
                    },
                )


@unittest.skipUnless(
    sys.platform == "linux" and os.geteuid() == 0,
    "Requires root Linux PID visibility, exercised on the blue worker",
)
class ProcessLifecycleTests(unittest.TestCase):
    def test_cancel_removes_detached_owned_children_and_preserves_unrelated_process(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            repo = destination / "repo"
            package = repo / "tests/integration_tests/tools/lite_actions"
            package.mkdir(parents=True)
            for folder in [
                repo / "tests",
                repo / "tests/integration_tests",
                repo / "tests/integration_tests/tools",
                package,
            ]:
                (folder / "__init__.py").write_text("")
            (package / "entrypoint.py").write_text(
                "import dependency_marker\n"
                "import subprocess,time,pathlib\n"
                'p=subprocess.Popen(["sleep","60"],start_new_session=True)\n'
                'pathlib.Path("child.ready").write_text(str(p.pid))\n'
                "time.sleep(60)\n"
            )
            setup = destination / "set_env.sh"
            setup.write_text("true\n")
            dependency = destination / "private-dependency"
            dependency.mkdir()
            (dependency / "dependency_marker.py").write_text("")
            node = dict(
                ssh="fixture",
                node_ip="192.0.2.10",
                devices=[0, 1],
                pythonpath=str(dependency),
            )
            profile = dict(env_vars={"ASCEND_SET_ENV_PATH": str(setup)})
            marker = "isolated-" + destination.name
            command = remote_command(
                node, [node], profile, str(destination), marker, 90, "training"
            )
            launcher = subprocess.Popen(
                ["bash", "-lc", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            foreign = subprocess.Popen(
                ["sleep", "60"], env={**os.environ, "LITE_CI_JOB_ID": "unrelated-task"}
            )

            def local_ssh(host, script, **kwargs):
                result = subprocess.run(
                    ["bash", "-lc", script], capture_output=True, text=True, timeout=35
                )
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                return result

            try:
                deadline = time.monotonic() + 10
                while not (repo / "child.ready").exists():
                    if time.monotonic() > deadline:
                        self.fail("fixture model did not start")
                    time.sleep(0.02)
                # An exit file alone must never release a surviving CPU child.
                (destination / "exit_code.txt").write_text("0\n")
                with patch("ci_core.ssh_runner.ssh", side_effect=local_ssh):
                    with self.assertRaises(RuntimeError):
                        owned_processes_gone(node, str(destination), marker)
                    cancel(node, str(destination), marker)
                    owned_processes_gone(node, str(destination), marker)
                launcher.wait(5)
                self.assertIsNone(foreign.poll())
            finally:
                with patch("ci_core.ssh_runner.ssh", side_effect=local_ssh):
                    cancel(node, str(destination), marker)
                if launcher.poll() is None:
                    launcher.kill()
                launcher.wait(5)
                foreign.terminate()
                foreign.wait(5)
