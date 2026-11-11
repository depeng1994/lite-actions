"""RFC #19 AU-08..09: transaction recovery and manual intervention."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update.update import UpdateError, _recover
from auto_update.version_switch import SwitchError, current_release, switch_current
from tests.test_auto_update_version_switch import checkout, settings


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old, self.old_sha = checkout(self.root, "old")
        self.new, self.new_sha = checkout(self.root, "new")
        os.symlink(self.new, self.root / "current")
        self.config = settings(self.root)
        self.config.state_dir.mkdir()
        self.transaction = {
            "phase": "SWITCHING",
            "old_sha": self.old_sha,
            "target_sha": self.new_sha,
            "unit": "test-worker.service",
        }
        (self.config.state_dir / "transaction.json").write_text(json.dumps(self.transaction))

    def test_au08_interrupted_switch_recovers_previous_release(self):
        def restore(root, old, unit, ready_path):
            switch_current(root, old)
            return "process_active"
        with patch("auto_update.update.restore_old", side_effect=restore):
            self.assertTrue(_recover(self.config))
        self.assertEqual(current_release(self.root)[1], self.old_sha)
        state = json.loads((self.config.state_dir / "transaction.json").read_text())
        self.assertEqual(state["phase"], "ROLLED_BACK")
        self.assertEqual(
            json.loads((self.config.state_dir / "blocked.json").read_text())["sha"],
            self.new_sha,
        )

    def test_au09_failed_rollback_requires_manual_recovery(self):
        with patch("auto_update.update.restore_old", side_effect=SwitchError("rollback_failed")):
            with self.assertRaises(UpdateError):
                _recover(self.config)
        state = json.loads((self.config.state_dir / "transaction.json").read_text())
        self.assertEqual(state["phase"], "RECOVERY_REQUIRED")
        with self.assertRaises(UpdateError):
            _recover(self.config)


if __name__ == "__main__":
    unittest.main()
