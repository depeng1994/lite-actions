"""Durable admission must be idempotent and bind each case to one plan."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.job_state import JobState


class AdmissionTests(unittest.TestCase):
    def test_redelivery_and_conflicts_are_atomic(self):
        run = {
            "_ci_provider": "gitcode",
            "_ci_repo": "cann/torchtitan-npu",
            "id": "run-1",
            "run_attempt": 1,
            "_ci_dispatch_id": "dispatch-1",
            "head_sha": "a" * 40,
            "path": "smoke.yml",
        }
        record = {"run": run, "digest": "plan-1", "item": {"case_id": "first"}}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"LITE_ACTIONS_STATE_DIR": ""}),
        ):
            state = JobState(Path(folder))
            self.assertEqual(state.admit([("first", record)]), ["first"])
            self.assertEqual(state.admit([("first", record)]), [])
            before = state.read()

            changed_item = copy.deepcopy(record)
            changed_item["item"]["case_id"] = "changed"
            changed_plan = copy.deepcopy(record)
            changed_plan["digest"] = "other-plan"
            for batch in (
                [("first", changed_item)],
                [("second", changed_plan)],
                [("second", record), ("third", changed_plan)],
            ):
                with self.subTest(batch=batch), self.assertRaises(ValueError):
                    state.admit(batch)
                self.assertEqual(state.read(), before)

            # A rollback may leave records without queue metadata; a later
            # upgrade must continue after the largest already assigned order.
            persisted = json.loads(state.path.read_text())
            del persisted["__lite_actions_metadata__"]
            state.path.write_text(json.dumps(persisted))
            other = copy.deepcopy(record)
            other["run"]["id"] = "run-2"
            self.assertEqual(state.admit([("second", other)]), ["second"])
            self.assertEqual(state.read()["second"]["admission_seq"], 2)


if __name__ == "__main__":
    unittest.main()
