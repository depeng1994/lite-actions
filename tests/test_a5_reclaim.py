"""A5 PID parsing, selective reclaim, cleanup and trusted configuration regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.config import load
from ci_core.executor import ExternalNpuOccupancy, process_evidence, remote_preflight

A5 = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
+===========================+
| 0 | 2000219 | python3 | 345 | 46124 |
| 1 | 2000220 | python3 | 345 | 99887 |
"""
EMPTY = """| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |
| No running processes found in NPU 0 |
"""

class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.node = {"host_id": "192.0.2.1:22", "ssh": "192.0.2.1:22"}
        self.pool = {"owner": "yellow", "type": "ascend-a5", "process_indexing": "card", "force_reclaim": True, "nodes": [self.node]}
        self.case = {"pool": "a5", "allocation": [{"host_id": self.node["host_id"], "devices": [0]}]}

    def test_a5_container_pid_and_selected_chip(self):
        self.assertEqual(process_evidence(A5, [0], indexing="card"), [(0, 46124)])
        self.assertEqual(process_evidence(EMPTY, [0], indexing="card"), [])

    def test_unknown_row_fails_closed(self):
        with self.assertRaises(ValueError):
            process_evidence(A5.replace("46124", "???"), [0], indexing="card")

    def test_reclaim_kills_only_selected_container_pid(self):
        commands = []
        outputs = iter([A5, A5, EMPTY])
        def run(host, cmd, **kw):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout=next(outputs) if "npu-smi" in cmd else "")
        with patch("ci_core.transport.run", side_effect=run):
            remote_preflight(self.case, {"a5": self.pool})
        self.assertEqual([x for x in commands if x.startswith("kill")], ["kill -9 -- 46124"])

    def test_blue_does_not_preempt(self):
        self.pool.update(owner="blue", type="ascend-a3", force_reclaim=False)
        self.pool["process_indexing"] = "card"
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=A5)):
            with self.assertRaises(ExternalNpuOccupancy):
                remote_preflight(self.case, {"a5": self.pool})

    def test_failed_kill_fails_closed(self):
        def run(host, cmd, **kw):
            return SimpleNamespace(returncode=1 if cmd.startswith("kill") else 0, stdout=A5)
        with patch("ci_core.transport.run", side_effect=run):
            with self.assertRaises(RuntimeError):
                remote_preflight(self.case, {"a5": self.pool})

    def test_persistent_process_fails_closed(self):
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=A5)):
            with self.assertRaises(RuntimeError):
                remote_preflight(self.case, {"a5": self.pool})

    def test_shared_pid_outside_allocation_never_killed(self):
        shared = A5.replace("99887", "46124")
        with patch("ci_core.transport.run", return_value=SimpleNamespace(returncode=0, stdout=shared)) as run:
            with self.assertRaises(ValueError):
                remote_preflight(self.case, {"a5": self.pool})
        self.assertFalse(any(c.args[1].startswith("kill") for c in run.call_args_list))

    def test_config_rejects_blue_and_a3_force_reclaim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config/workers.json").write_text(json.dumps({"blue": {"enabled": True}, "yellow": {"enabled": True}}))
            for owner, kind in [("blue", "ascend-a5"), ("yellow", "ascend-a3")]:
                (root / "config/pools.json").write_text(json.dumps({"p": {"owner": owner, "type": kind, "force_reclaim": True, "nodes": []}}))
                with self.assertRaises(ValueError):
                    load(root, "yellow")

if __name__ == "__main__":
    unittest.main()
