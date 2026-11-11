"""RFC #19 AU-04..07: version switching and rollback safety."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update.update import Settings, UpdateError, _run
from auto_update.version_switch import SwitchError, current_release, switch_current


def checkout(root: Path, message: str) -> tuple[Path, str]:
    folder = root / ("checkout-" + message)
    folder.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (folder / "marker.txt").write_text(message)
    git("add", ".")
    git("commit", "-qm", message)
    sha = git("rev-parse", "HEAD")
    dest = root / "releases" / sha
    dest.parent.mkdir(exist_ok=True)
    folder.rename(dest)
    return dest, sha


def settings(root: Path) -> Settings:
    return Settings(
        root=root, source="https://github.com/depeng1994/lite-actions.git",
        ref="refs/heads/main", unit="test-worker.service",
        state_dir=root / "runtime", ready_file=root / "ready.json",
        log_file=root / "updates.jsonl", lock_file=root / "lock", ready_timeout=120,
    )


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.sha_a = checkout(self.root, "A")
        self.b, self.sha_b = checkout(self.root, "B")
        os.symlink(self.a, self.root / "current")

    def test_au04_atomic_switch_to_new_release(self):
        old, old_sha = current_release(self.root)
        self.assertEqual((old, old_sha), (self.a, self.sha_a))
        switch_current(self.root, self.b)
        self.assertEqual(current_release(self.root), (self.b, self.sha_b))
        self.assertTrue(self.a.is_dir())

    def test_au05_preflight_failure_does_not_stop(self):
        stage = self.root / "staging" / "trial"
        stage.mkdir(parents=True)
        with (
            patch("auto_update.update.check_source", return_value=self.sha_b),
            patch("auto_update.update.stage_source", return_value=stage),
            patch("auto_update.update._validate_candidate", side_effect=UpdateError("bad_candidate")),
            patch("auto_update.update.stop_worker") as stop,
        ):
            self.assertEqual(_run(settings(self.root)), 2)
            stop.assert_not_called()
        self.assertEqual(current_release(self.root)[1], self.sha_a)

    def test_au06_stop_failure_cannot_switch_current(self):
        stage = self.root / "staging" / "trial"
        stage.mkdir(parents=True)
        with (
            patch("auto_update.update.check_source", return_value=self.sha_b),
            patch("auto_update.update.stage_source", return_value=stage),
            patch("auto_update.update._validate_candidate"),
            patch("auto_update.update.prepare_release", return_value=self.b),
            patch("auto_update.update.stop_worker", side_effect=SwitchError("worker_stop_failed")),
            patch("auto_update.update.restore_old", return_value="process_active"),
            patch("auto_update.update.switch_current") as switching,
        ):
            self.assertEqual(_run(settings(self.root)), 3)
            switching.assert_not_called()
        self.assertEqual(current_release(self.root)[1], self.sha_a)

    def test_au07_start_failure_rolls_back_old_code(self):
        stage = self.root / "staging" / "trial"
        stage.mkdir(parents=True)
        def rollback(root, old, unit, ready_file):
            switch_current(root, old)
            return "process_active"
        with (
            patch("auto_update.update.check_source", return_value=self.sha_b),
            patch("auto_update.update.stage_source", return_value=stage),
            patch("auto_update.update._validate_candidate"),
            patch("auto_update.update.prepare_release", return_value=self.b),
            patch("auto_update.update.stop_worker"),
            patch("auto_update.update.start_worker", side_effect=SwitchError("start_failed")),
            patch("auto_update.update.restore_old", side_effect=rollback),
        ):
            self.assertEqual(_run(settings(self.root)), 3)
        self.assertEqual(current_release(self.root)[1], self.sha_a)
        self.assertEqual((self.root / "runtime" / "blocked.json").exists(), True)


if __name__ == "__main__":
    unittest.main()
