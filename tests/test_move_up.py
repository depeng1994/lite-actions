import tempfile
import unittest
from pathlib import Path

from ci_core.job_state import JobState, ordered_queued
from ci_core.router import TRAIN_QUEUE_TIMEOUT_SECONDS, queue_timeout


class MoveUpTests(unittest.TestCase):
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
            self.assertEqual(
                [
                    key
                    for key, r in ordered_queued(state.read())
                    if r["item"]["target_pool"] == "isolated"
                ],
                ["other-case"],
            )
            self.assertNotIn("queue_rank", state.read()["other-case"])

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

    def test_w1_w2_w3_requeue_consecutive_swaps_multicase_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            state = JobState(Path(directory))

            def entry(key, run_id, *, order=0, pool="shared"):
                return (
                    key,
                    {
                        "phase": "QUEUED",
                        "run": {
                            "id": run_id,
                            "_ci_provider": "github",
                            "_ci_repo": "depeng1994/lite-actions",
                            "run_attempt": 1,
                            "created_at": f"2026-01-01T00:00:0{order}Z",
                        },
                        "item": {
                            "target_pool": pool,
                            "kind": "npu_smoke",
                            "order": order,
                        },
                    },
                )

            def wf(run_id):
                return {
                    "provider": "github",
                    "repo": "depeng1994/lite-actions",
                    "run_id": run_id,
                    "attempt": 1,
                }

            state.admit([entry("w1-1", "w1", order=0)])
            state.admit([entry("w2-1", "w2", order=0), entry("w2-2", "w2", order=1)])
            state.admit([entry("w3-1", "w3", order=0), entry("w3-2", "w3", order=1)])

            # Swap W3 <-> W2: must keep W1 first and preserve intra-workflow order.
            res1 = state.swap_workflow(
                {"a": wf("w3"), "b": wf("w2"), "op_id": "op-3-2"}, "cmd-1"
            )
            self.assertEqual(res1["status"], "APPLIED")
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["w1-1", "w3-1", "w3-2", "w2-1", "w2-2"],
            )

            # Requeueing W1 must NOT demote W1 behind W3/W2!
            state.update("w1-1", phase="RESERVED")
            state.update("w1-1", phase="QUEUED")
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["w1-1", "w3-1", "w3-2", "w2-1", "w2-2"],
            )

            # Second consecutive swap W3 <-> W1 moves W3 to front ahead of W1.
            res2 = state.swap_workflow(
                {"a": wf("w3"), "b": wf("w1"), "op_id": "op-3-1"}, "cmd-2"
            )
            self.assertEqual(res2["status"], "APPLIED")
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["w3-1", "w3-2", "w1-1", "w2-1", "w2-2"],
            )

            # Brand new W4 stays at tail; late-discovered W3 case stays in W3 slot.
            state.admit([entry("w4-1", "w4", order=0)])
            state.admit([entry("w3-3", "w3", order=2)])
            self.assertEqual(
                [key for key, _ in ordered_queued(state.read())],
                ["w3-1", "w3-2", "w3-3", "w1-1", "w2-1", "w2-2", "w4-1"],
            )

            # Target already in-flight returns CONFLICT and prunes terminal entries.
            state.update("w4-1", phase="RUNNING")
            conflict = state.swap_workflow(
                {"a": wf("w4"), "b": wf("w2"), "op_id": "op-conflict"}, "cmd-3"
            )
            self.assertEqual(conflict["status"], "CONFLICT")
            self.assertEqual(conflict["pools"][0]["st"], "CONFLICT")

            for key in ("w2-1", "w2-2"):
                state.update(key, phase="REPORTED")
            state.update("w1-1", phase="QUEUED")
            raw = state._read_raw()
            wf_order = raw["__lite_actions_metadata__"]["workflow_order"]
            self.assertNotIn("github:depeng1994/lite-actions:w2:1", wf_order)
