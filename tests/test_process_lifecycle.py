"""CPU process lifecycle through the real remote supervisor and cleanup commands."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.ssh_runner import cancel, owned_processes_gone, remote_command


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
                "import subprocess,time,pathlib\n"
                'p=subprocess.Popen(["sleep","60"],start_new_session=True)\n'
                'pathlib.Path("child.ready").write_text(str(p.pid))\n'
                "time.sleep(60)\n"
            )
            setup = destination / "set_env.sh"
            setup.write_text("true\n")
            node = dict(ssh="fixture", node_ip="192.0.2.10", devices=[0, 1])
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
