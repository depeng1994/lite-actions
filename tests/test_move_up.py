import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from ci_core.job_state import JobState, ordered_queued
from ci_core.router import TRAIN_QUEUE_TIMEOUT_SECONDS, queue_timeout


class MoveUpTests(unittest.TestCase):
    def test_adjacent_moves_are_atomic_idempotent_and_preserved_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = JobState(root)
            for i in range(1, 5):
                state.admit(
                    [
                        (
                            f"case-{i}",
                            {
                                "phase": "QUEUED",
                                "run": {
                                    "id": i,
                                    "_ci_provider": "gitcode" if i == 4 else "github",
                                },
                                "item": {
                                    "target_pool": "test",
                                    "kind": "npu_smoke",
                                    "order": 0,
                                },
                            },
                        )
                    ]
                )
            for n, pos in enumerate([2, 1, 0], start=1):
                self.assertEqual(
                    state.move_up("case-4", f"cmd{n}"),
                    {"status": "APPLIED", "pos": pos},
                )
                state = JobState(root)
            self.assertEqual(
                state.move_up("case-4", "cmd1"), {"status": "APPLIED", "pos": 2}
            )
            self.assertEqual(
                state.move_up("case-4", "head"), {"status": "NOOP", "pos": 0}
            )
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["case-4", "case-1", "case-2", "case-3"],
            )
            self.assertEqual(
                [state.read()[f"case-{i}"]["admission_seq"] for i in range(1, 5)],
                [1, 2, 3, 4],
            )
            state.update("case-4", phase="RESERVED")
            self.assertEqual(state.move_up("case-4", "leased")["status"], "CONFLICT")
            with concurrent.futures.ThreadPoolExecutor(2) as executor:
                futures = [
                    executor.submit(state.move_up, f"case-{i}", f"concurrent-{i}")
                    for i in [2, 3]
                ]
                self.assertEqual(
                    [future.result()["status"] for future in futures],
                    ["APPLIED", "APPLIED"],
                )
            self.assertEqual(
                len({row["queue_rank"] for _, row in ordered_queued(state.read())}), 3
            )

    def test_train_waits_72h_and_smoke_has_no_timeout(self):
        self.assertEqual(queue_timeout("train"), 259200)
        self.assertLess(71 * 3600 + 59 * 60, TRAIN_QUEUE_TIMEOUT_SECONDS)
        self.assertGreater(72 * 3600 + 60, TRAIN_QUEUE_TIMEOUT_SECONDS)
        self.assertIsNone(queue_timeout("npu_smoke"))
        self.assertEqual(queue_timeout("worker_probe"), 3600)
