import unittest
from unittest.mock import MagicMock

from ci_core.github_source import bind_request_source
from ci_core.router import ordered_queued, queue_telemetry


class QueueTelemetryTests(unittest.TestCase):
    def test_existing_pr_lookup_also_supplies_author(self):
        client = MagicMock()
        client.request.return_value = {
            "head": {"repo": {"full_name": "test/source", "id": 17}},
            "base": {"ref": "main"},
            "user": {"login": "test-author"},
        }
        commit = "a" * 40
        result = bind_request_source(
            client,
            {"head_sha": commit},
            {"mode": "merge_result", "head_sha": commit, "pr_number": 2},
        )
        client.request.assert_called_once_with("GET", "/pulls/2")
        self.assertEqual(result["_ci_pr_author"], "test-author")

    def test_projection_uses_scheduler_sequence_and_verifies_candidates(self):
        node = {
            "host_id": "198.51.100.1:22",
            "environments": {"training": {"container_id": "a" * 64}},
            "allocation_groups": [
                {"devices": [0, 1], "verified": True},
                {"devices": [2, 3], "verified": True},
            ],
        }
        pool = {"enabled": True, "owner": "blue", "nodes": [node]}
        item = {
            "kind": "npu_smoke",
            "case_id": "case_000_demo",
            "target_pool": "test",
            "resources": {"nodes": 1, "devices_per_node": 2},
            "docker": None,
            "execution_timeout_seconds": 50,
            "order": 0,
        }
        records = {
            f"run-{i}": {
                "phase": "QUEUED",
                "run": {"id": i},
                "admission_seq": 4 - i,
                "item": item,
            }
            for i in range(1, 4)
        }
        records["quarantine"] = {
            "phase": "REPORTED",
            "quarantined": True,
            "allocation": [{"host_id": node["host_id"], "devices": [0, 1]}],
        }
        self.assertEqual(
            [key for key, _ in ordered_queued(records)], ["run-3", "run-2", "run-1"]
        )
        result = queue_telemetry("blue", {"test": pool}, records, True)[
            ("test", "npu_smoke")
        ]
        self.assertEqual(
            [(row["pos"], row["ix"]) for row in result],
            [(0, [[1]]), (1, [[1]]), (2, [[1]])],
        )
