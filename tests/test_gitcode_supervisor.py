"""Real local subprocess/interlock tests, never an NPU acceptance substitute."""

import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core import gitcode_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.marker = "g4_" + "a" * 64

    def command(self, text):
        return shlex.join([sys.executable, "-I", "-c", text])

    def test_cancelled_marker_denies_a_late_remote_launch(self):
        supervisor.cancel_intent(self.directory, self.marker)
        destination = self.directory / "must-not-exist"
        command = self.command(
            f"from pathlib import Path; Path({str(destination)!r}).write_text('launched')"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                supervisor.program(),
                "launch",
                str(self.directory),
                self.marker,
                command,
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 130)
        self.assertFalse(destination.exists())
        self.assertFalse((self.directory / "job.pgid").exists())
        self.assertEqual((self.directory / "exit_code.txt").read_text(), "130")

    def test_launch_directory_cannot_be_replayed(self):
        destination = self.directory / "executions"
        command = self.command(
            f"from pathlib import Path; p=Path({str(destination)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
        )
        self.assertEqual(supervisor.launch(self.directory, self.marker, command), 0)
        with self.assertRaisesRegex(ValueError, "not reusable"):
            supervisor.launch(self.directory, self.marker, command)
        self.assertEqual(destination.read_text(), "x")

    def test_foreign_marker_does_not_replace_existing_identity(self):
        (self.directory / "job.intent").write_text("g4_" + "b" * 64)
        with self.assertRaisesRegex(ValueError, "foreign"):
            supervisor.cancel_intent(self.directory, self.marker)
        self.assertFalse((self.directory / "cancel.requested").exists())

    def test_lock_serializes_cancellation_against_process_creation(self):
        entered, proceed = threading.Event(), threading.Event()

        def popen(*args, **kwargs):
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError("test did not release creation lock")
            return SimpleNamespace(pid=12345, wait=lambda: 0)

        with (
            patch.object(supervisor.subprocess, "Popen", side_effect=popen) as created,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            launch = workers.submit(
                supervisor.launch,
                self.directory,
                self.marker,
                "trusted-fixture-command",
            )
            self.assertTrue(entered.wait(5))
            cancellation = workers.submit(
                supervisor.cancel_intent, self.directory, self.marker
            )
            self.assertFalse(cancellation.done())
            proceed.set()
            self.assertEqual(launch.result(5), 0)
            self.assertEqual(cancellation.result(5), 0)
            created.assert_called_once()
        self.assertEqual(
            supervisor.launch(self.directory, self.marker, "must-never-run"), 130
        )
        self.assertEqual((self.directory / "cancel.requested").read_text(), self.marker)

    def test_platform_environment_is_not_inherited_by_the_payload(self):
        command = self.command(
            "import os; assert 'GH_TOKEN' not in os.environ; assert 'GITCODE_API_TOKEN' not in os.environ; print(os.environ['LITE_CI_JOB_ID'])"
        )
        with patch.dict(
            os.environ,
            GH_TOKEN="unit-test-secret",
            GITCODE_API_TOKEN="unit-test-secret",
        ):
            self.assertEqual(supervisor.launch(self.directory, self.marker, command), 0)
        self.assertEqual((self.directory / "run.log").read_text().strip(), self.marker)

    def test_symlink_control_file_and_directory_are_rejected(self):
        target = self.directory / "foreign"
        target.write_text("keep")
        (self.directory / ".launch.lock").symlink_to(target)
        with self.assertRaises(OSError):
            supervisor.cancel_intent(self.directory, self.marker)
        self.assertEqual(target.read_text(), "keep")
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            supervisor.cancel_intent(alias, self.marker)

    def test_invalid_marker_rejected_without_any_launch_files(self):
        for value in ("", "../outside", "$(command)", "g4_" + "z" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                supervisor.cancel_intent(self.directory, value)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
