"""RFC #19 AU-12: Ready must bind to a real PID, start-time and SHA."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.worker_ready import process_identity, publish_ready, verify_ready


class ReadyTests(unittest.TestCase):
    def test_au12_good_ready_and_old_or_fake_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "runtime" / "ready.json"
            sha = "a" * 40
            with patch("ci_core.worker_ready.actual_sha", return_value=sha):
                data = publish_ready(Path(tmp), target)
            self.assertEqual(data["pid"], os.getpid())
            self.assertEqual(
                (data["boot_id"], data["process_start_ticks"]),
                process_identity(os.getpid()),
            )
            self.assertTrue(
                verify_ready(
                    target,
                    expected_sha=sha,
                    expected_pid=os.getpid(),
                    not_before=data["ready_at"] - 1,
                )
            )
            self.assertFalse(
                verify_ready(
                    target,
                    expected_sha="b" * 40,
                    expected_pid=os.getpid(),
                    not_before=data["ready_at"] - 1,
                )
            )
            self.assertFalse(
                verify_ready(
                    target,
                    expected_sha=sha,
                    expected_pid=os.getpid(),
                    not_before=data["ready_at"] + 1,
                )
            )
            forged = json.loads(target.read_text())
            forged["process_start_ticks"] += 1
            target.write_text(json.dumps(forged))
            self.assertFalse(
                verify_ready(
                    target,
                    expected_sha=sha,
                    expected_pid=os.getpid(),
                    not_before=time.time() - 20,
                )
            )


if __name__ == "__main__":
    unittest.main()
