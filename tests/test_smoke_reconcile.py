"""Reconciliation must check the same process identity used for execution."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ci_core.executor import task_dir
from scripts import reconcile
from tests.test_smoke_case_identity import run_identity


class SmokeReconciliationTests(unittest.TestCase):
    def test_repeated_smoke_checks_each_instance_before_releasing_quarantine(self):
        root = Path(reconcile.__file__).resolve().parents[1]
        for case_id in ("case_000_smoke_alpha", "case_002_smoke_alpha"):
            run = run_identity("gitcode")
            run.pop("_ci_test_id")
            record = {
                "run": run,
                "item": {
                    "worker": "blue",
                    "test_id": "smoke_alpha",
                    "case_id": case_id,
                },
                "use_instance_id": True,
                "quarantined": True,
                "runtime": {"pool": "pool_blue", "docker": "training"},
                "allocation": [{"host_id": "192.0.2.10:22", "devices": [0, 1]}],
            }
            state = MagicMock()
            state.read.return_value = {"task": record}
            expected = task_dir(
                {**run, "_ci_test_id": "smoke_alpha", "_ci_case_id": case_id},
                root,
            ).name
            pools = {"pool_blue": {"nodes": [{"host_id": "192.0.2.10:22"}]}}
            with (
                self.subTest(case_id=case_id),
                patch(
                    "sys.argv",
                    ["reconcile.py", "--worker-id", "blue", "--task-key", "task"],
                ),
                patch.object(reconcile, "JobState", return_value=state),
                patch.object(reconcile, "load", return_value=({}, pools)),
                patch.object(
                    reconcile, "execution_node", return_value={"ssh": "fixture"}
                ),
                patch.object(
                    reconcile,
                    "run",
                    side_effect=subprocess.CalledProcessError(
                        1, "owned process still active"
                    ),
                ) as check,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                reconcile.main()
            self.assertIn(expected, check.call_args.args[1])
            state.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
