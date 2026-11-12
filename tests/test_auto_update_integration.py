"""RFC #19 AU-10/11/13/14: integration contracts without production systemctl."""

import json
import subprocess
import tempfile
import time
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
                patch(
                    "auto_update.update.current_release", return_value=(version, sha)
                ),
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
                patch(
                    "auto_update.update.wait_ready",
                    side_effect=lambda *args, **kwargs: (
                        1001,
                        time.monotonic() - kwargs["switch_time"],
                        time.monotonic() - kwargs["switch_time"] + 0.01,
                    ),
                ),
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
        self.assertIn(
            'configured_python="$(printenv LITE_ACTIONS_UPDATE_PYTHON || true)"', script
        )
        self.assertIn('exec "$PYTHON" -u -m auto_update.update', script)
        self.assertIn("for name in python3 python3.11 python3.12", script)
        self.assertNotIn("kill -9", script)

    def test_au14_install_logging_timer_and_genericity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "management" / "updates.jsonl"
            record(
                p,
                "UPDATED",
                old_sha="a" * 40,
                target_sha="b" * 40,
                switch_to_start_ms=12,
            )
            rows = p.read_text().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["switch_to_start_ms"], 12)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        for shell in ("auto_update/bootstrap.sh", "scripts/install_auto_update.sh"):
            subprocess.run(["bash", "-n", str(ROOT / shell)], check=True)
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/install_auto_update.sh"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--worker-unit", result.stdout)
        timer = (ROOT / "deploy/lite-actions-auto-update.timer").read_text()
        self.assertIn("OnUnitInactiveSec=3min", timer)
        self.assertIn("AccuracySec=10s", timer)
        paths = [
            ROOT / "auto_update" / p
            for p in (
                "check_source.py",
                "update.py",
                "version_switch.py",
                "update_log.py",
            )
        ]
        for path in paths:
            text = path.read_text().lower()
            self.assertNotIn("blue", text)
            self.assertNotIn("yellow", text)

    def test_worker_setup_failure_rolls_back_without_permanently_blocking_sha(
        self,
    ):
        from auto_update.version_switch import SwitchError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, _ = checkout(root, "old")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            stage = root / "staging" / "candidate"
            stage.mkdir(parents=True)
            cfg = settings(root)
            cfg.state_dir.mkdir()

            def restore(root_arg, old_arg, unit, ready_file):
                from auto_update.version_switch import switch_current

                switch_current(root_arg, old_arg)
                return "old_ready"

            with (
                patch("auto_update.update.check_source", return_value=new_sha),
                patch("auto_update.update.stage_source", return_value=stage),
                patch("auto_update.update._validate_candidate"),
                patch("auto_update.update.prepare_release", return_value=new),
                patch("auto_update.update.stop_worker"),
                patch("auto_update.update.start_worker"),
                patch(
                    "auto_update.update.wait_ready",
                    side_effect=SwitchError("worker_not_active"),
                ),
                patch("auto_update.update.restore_old", side_effect=restore),
            ):
                self.assertEqual(_run(cfg), 3)
            self.assertEqual((root / "current").resolve(), old)
            self.assertFalse((cfg.state_dir / "blocked.json").exists())
            txn = json.loads((cfg.state_dir / "transaction.json").read_text())
            self.assertEqual(txn["phase"], "ROLLED_BACK")

    def test_candidate_failure_still_permanently_blocks_sha(self):
        from auto_update.version_switch import SwitchError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, old_sha = checkout(root, "old")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            stage = root / "staging" / "candidate"
            stage.mkdir(parents=True)
            cfg = settings(root)
            cfg.state_dir.mkdir()
            with (
                patch("auto_update.update.check_source", return_value=new_sha),
                patch("auto_update.update.stage_source", return_value=stage),
                patch("auto_update.update._validate_candidate"),
                patch("auto_update.update.prepare_release", return_value=new),
                patch("auto_update.update.stop_worker"),
                patch("auto_update.update.start_worker"),
                patch(
                    "auto_update.update.wait_ready",
                    side_effect=SwitchError("candidate_runtime_invalid"),
                ),
                patch("auto_update.update.restore_old", return_value="old_ready"),
            ):
                self.assertEqual(_run(cfg), 3)
            blocked = json.loads((cfg.state_dir / "blocked.json").read_text())
            self.assertEqual(blocked["sha"], new_sha)


if __name__ == "__main__":
    unittest.main()
