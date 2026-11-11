"""RFC #19 AU-10/11/13/14: integration contracts without production systemctl."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update.update import _run, exclusive
from auto_update.update_log import record
from tests.test_auto_update_version_switch import checkout, settings

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_au10_exclusive_lock_and_idempotent_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exclusive.lock"
            with exclusive(path):
                with self.assertRaisesRegex(Exception, "update_already_running"):
                    with exclusive(path):
                        pass
            with exclusive(path):
                pass
            root = Path(tmp)
            version, sha = checkout(root, "idempotent")
            with (
                patch("auto_update.update.current_release", return_value=(version, sha)),
                patch("auto_update.update.check_source", return_value=sha),
                patch("auto_update.update.stop_worker") as stopped,
            ):
                self.assertEqual(_run(settings(root)), 0)
                stopped.assert_not_called()

    def test_au11_running_task_does_not_skip_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, old_sha = checkout(root, "running")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            stage = root / "staging" / "candidate"
            stage.mkdir(parents=True)
            with (
                patch("auto_update.update.check_source", return_value=new_sha),
                patch("auto_update.update.stage_source", return_value=stage),
                patch("auto_update.update._validate_candidate"),
                patch("auto_update.update.prepare_release", return_value=new),
                patch("auto_update.update.stop_worker") as stop,
                patch("auto_update.update.start_worker"),
                patch("auto_update.update.wait_ready", return_value=(1001, 0.01, 0.02)),
            ):
                self.assertEqual(_run(settings(root)), 0)
                stop.assert_called_once_with("test-worker.service")
                self.assertEqual((root / "current").resolve(), new)
            # Jobs and leases live outside releases. The updater did not delete them.
            self.assertTrue(old.is_dir())

    def test_au13_bootstrap_pins_source_and_new_install_will_load_next(self):
        script = (ROOT / "auto_update/bootstrap.sh").read_text()
        self.assertIn('readlink -f "$ROOT/current"', script)
        self.assertIn('cd "$code_dir"', script)
        self.assertIn("exec /usr/bin/python3 -u -m auto_update.update", script)
        self.assertNotIn("kill -9", script)

    def test_au14_install_logging_timer_and_genericity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "management" / "updates.jsonl"
            record(p, "UPDATED", old_sha="a" * 40, target_sha="b" * 40,
                   switch_to_start_ms=12)
            rows = p.read_text().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["switch_to_start_ms"], 12)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for shell in ("auto_update/bootstrap.sh", "scripts/install_auto_update.sh"):
            subprocess.run(["bash", "-n", str(ROOT / shell)], check=True)
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/install_auto_update.sh"), "--help"],
            capture_output=True, text=True, check=True
        )
        self.assertIn("--worker-unit", result.stdout)
        timer = (ROOT / "deploy/lite-actions-auto-update.timer").read_text()
        self.assertIn("OnUnitInactiveSec=3min", timer)
        self.assertIn("AccuracySec=10s", timer)
        paths = [ROOT / "auto_update" / p for p in
                 ("check_source.py", "update.py", "version_switch.py", "update_log.py")]
        for path in paths:
            text = path.read_text().lower()
            self.assertNotIn("blue", text)
            self.assertNotIn("yellow", text)


if __name__ == "__main__":
    unittest.main()
