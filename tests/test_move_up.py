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

    def test_workflow_swap_is_durable_idempotent_and_preserves_unshared_queues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = JobState(root)
            for workflow in ("a", "b"):
                state.admit(
                    [
                        (
                            f"{workflow}-case",
                            {
                                "phase": "QUEUED",
                                "run": {
                                    "id": workflow,
                                    "_ci_provider": "github",
                                    "_ci_repo": "depeng1994/lite-actions",
                                    "run_attempt": 1,
                                },
                                "item": {
                                    "target_pool": "shared",
                                    "kind": "npu_smoke",
                                    "order": 0,
                                },
                            },
                        )
                    ]
                )
            state.admit(
                [
                    (
                        "other-case",
                        {
                            "phase": "QUEUED",
                            "run": {
                                "id": "other",
                                "_ci_provider": "github",
                                "_ci_repo": "depeng1994/lite-actions",
                                "run_attempt": 1,
                            },
                            "item": {
                                "target_pool": "isolated",
                                "kind": "npu_smoke",
                                "order": 0,
                            },
                        },
                    )
                ]
            )
            payload = {
                "a": {
                    "provider": "github",
                    "repo": "depeng1994/lite-actions",
                    "run_id": "b",
                    "attempt": 1,
                },
                "b": {
                    "provider": "github",
                    "repo": "depeng1994/lite-actions",
                    "run_id": "a",
                    "attempt": 1,
                },
                "op_id": "priority-1",
            }
            result = state.swap_workflow(payload, "op-1")
            self.assertEqual(result["status"], "APPLIED")
            self.assertEqual(state.swap_workflow(payload, "cmd-retry"), result)
            self.assertEqual(state.swap_workflow(payload, "cmd-retry-2"), result)
            ordered = [
                key
                for key, _ in ordered_queued(state.read())
                if key in {"a-case", "b-case"}
            ]
            self.assertEqual(ordered, ["b-case", "a-case"])
            self.assertEqual(state.read()["other-case"]["queue_rank"], 1)

    def test_late_discovery_and_requeue_keep_swapped_workflow_order(self):
        with tempfile.TemporaryDirectory() as directory:
            state = JobState(Path(directory))

            def entry(key, run_id):
                return (
                    key,
                    {
                        "phase": "QUEUED",
                        "run": {
                            "id": run_id,
                            "_ci_provider": "github",
                            "_ci_repo": "depeng1994/lite-actions",
                            "run_attempt": 1,
                        },
                        "item": {
                            "target_pool": "shared",
                            "kind": "npu_smoke",
                            "order": 0,
                        },
                    },
                )

            state.admit([entry("a-case", "a")])
            state.admit([entry("b-case", "b")])
            payload = {
                "a": {
                    "provider": "github",
                    "repo": "depeng1994/lite-actions",
                    "run_id": "b",
                    "attempt": 1,
                },
                "b": {
                    "provider": "github",
                    "repo": "depeng1994/lite-actions",
                    "run_id": "a",
                    "attempt": 1,
                },
                "op_id": "late-1",
            }
            self.assertEqual(
                state.swap_workflow(payload, "cmd-late")["status"], "APPLIED"
            )
            state.admit([entry("b-late-case", "b")])
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["b-case", "b-late-case", "a-case"],
            )
            state.update("b-case", phase="RESERVED")
            state.update("b-case", phase="QUEUED")
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["b-case", "b-late-case", "a-case"],
            )
