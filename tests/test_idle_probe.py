import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.idle_probe import observe, observe_health


class IdleProbeTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "host_id": "198.51.100.1:22",
            "ssh": "unused",
            "host_ssh": "node",
            "allocation_groups": [{"devices": [0, 1]}, {"devices": [2, 3]}],
        }
        self.pools = {
            "test": {
                "owner": "blue",
                "enabled": True,
                "process_indexing": "card-die",
                "nodes": [self.node],
            }
        }
        self.execute = Mock(return_value=SimpleNamespace(stdout="process table"))

    def test_single_read_for_both_groups_and_only_occupancy_leaves_worker(self):
        with patch(
            "ci_core.idle_probe._process_rows", return_value=[(0, 1234, "NA")]
        ) as parser:
            result, completed = observe("blue", self.pools, {}, execute=self.execute)
        self.execute.assert_called_once_with("node", "npu-smi info", timeout=20)
        self.assertTrue(parser.call_args.kwargs["require_coverage"])
        self.assertFalse(parser.call_args.kwargs["require_exclusive"])
        self.assertEqual([value[0] for value in result.values()], ["busy", "idle"])
        self.assertIsNotNone(completed)
        self.assertNotIn("1234", str(result))

    def test_skipped_lease_and_failed_probe_never_prove_idle(self):
        self.execute.side_effect = TimeoutError
        result, completed = observe(
            "blue",
            self.pools,
            {
                "test": {
                    "phase": "RUNNING",
                    "allocation": [
                        {"host_id": self.node["host_id"], "devices": [0, 1]}
                    ],
                },
            },
            execute=self.execute,
        )
        self.assertEqual(list(result), [("test", self.node["host_id"], (2, 3))])
        self.assertEqual(next(iter(result.values()))[0], "probe_error")

    def test_visibility_health_counts_failures_without_resetting_last_online(self):
        self.pools["test"]["enabled"] = False
        with patch("ci_core.idle_probe._process_rows", return_value=[]):
            _, completed, health = observe_health("blue", self.pools, {}, execute=self.execute, probe_round=1)
        self.assertIsNotNone(completed)
        key = '["test","198.51.100.1:22",[0,1]]'
        self.assertEqual(health[key], {"v": 0, "mc": 0, "lo": health[key]["lo"], "pr": 1})

        previous = health
        with patch("ci_core.idle_probe._process_rows", side_effect=TimeoutError):
            for round_no in range(2, 7):
                _, completed, previous = observe_health(
                    "blue", self.pools, {}, previous=previous, probe_round=round_no
                    , execute=self.execute
                )
        self.assertIsNone(completed)
        self.assertEqual(previous[key]["v"], 2)
        self.assertEqual(previous[key]["mc"], 5)
        self.assertIsNotNone(previous[key]["lo"])
        self.assertIsNone(completed)
