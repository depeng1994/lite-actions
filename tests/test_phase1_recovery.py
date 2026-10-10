"""Phase 1 fail-closed cancellation, quarantine, and reporting tests."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from ci_core import occupancy, scheduler, v2_dispatch
from ci_core.config import load_config
from ci_core.dispatch import Execution
from ci_core.job_state import JobState
from ci_core.protocol_v2 import plan_digest

ROOT = Path(__file__).resolve().parents[1]
TEST_ID = "dsv4_flash_a3_8p_example"
CASE = {"test_id": TEST_ID, "nnodes": 1, "ngpu": 8, "label": "Muon"}


def plan():
    base = {"schema": 2, "repo": "depeng1994/torchtitan-npu",
            "run_id": 1024, "attempt": 1, "sha": "a"*40,
            "pipeline": "a3-8p", "cases": [CASE]}
    return {**base, "plan_digest": plan_digest(base)}


RUN = {"id": 1024, "run_number": 5, "run_attempt": 1, "head_sha": "a"*40,
       "created_at": "2026-10-10T08:00:00Z", "name": "A3-8p-CI-Example",
       "display_title": "A3-8p-CI-Example"}


class FakeGitHub:
    published = []
    def __init__(self, token): pass
    def commit_comments(self, sha): return []
    def send_result(self, sha, body):
        self.published.append(json.loads(body.split("\n", 1)[1]))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        FakeGitHub.published = []

    def test_orphan_quarantine_prevents_new_training(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:orphan", phase="reported",
                                  quarantined=True, allocation=[
                    {"host_id": "a3-4", "devices": list(range(8))}])
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight") as preflight:
                with self.assertRaises(scheduler.UnsafeResourceState):
                    with scheduler.claim_dynamic(root, case, ascend_env=None,timeout=0.1):
                        pass
                preflight.assert_not_called()

    def test_running_previous_case_stops_global_serial_slot(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:orphan", phase="running",
                                  allocation=[{"host_id":"a3-4","devices":list(range(8))}])
            case=load_config(ROOT)["a3-8p"]
            with self.assertRaises(scheduler.UnsafeResourceState):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.1):
                    pass

    def test_agent_crash_never_blindly_relaunches_case(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            run={**RUN, "_ci_request": plan()}
            key=v2_dispatch.task_key(run, TEST_ID)
            JobState(root).update(key, phase="running", sha="a"*40,
                                  allocation=[{"host_id":"a3-4","devices":list(range(8))}])
            with patch.object(v2_dispatch,"GitHubClient",FakeGitHub), \
                 patch.object(v2_dispatch,"authorization",return_value="fake"), \
                 patch.object(v2_dispatch,"execute") as execute:
                v2_dispatch.run_and_report_v2(load_config(ROOT)["a3-8p"],run,root=root)
            execute.assert_not_called()
            states=JobState(root).read()
            self.assertTrue(states[key]["quarantined"])
            self.assertEqual(states[key]["phase"],"blocked_recovery")
            self.assertEqual(states["1024:1"]["phase"],"blocked_recovery")

    def test_cancelled_run_reports_cancelled_without_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            event=Event();event.set()
            run={**RUN, "_ci_request": plan(),"_ci_cancel_event":event}
            with patch.object(v2_dispatch,"GitHubClient",FakeGitHub), \
                 patch.object(v2_dispatch,"authorization",return_value="fake"), \
                 patch.object(v2_dispatch,"execute") as execute:
                v2_dispatch.run_and_report_v2(load_config(ROOT)["a3-8p"],run,root=root)
            execute.assert_not_called()
            self.assertEqual(FakeGitHub.published[0]["status"],"CANCELLED")

    def test_remote_reclamation_script_is_pid_scoped_and_syntax_valid(self):
        ast.parse(occupancy._REMOTE_TERMINATE)
        self.assertIn("SIGTERM",occupancy._REMOTE_TERMINATE)
        self.assertIn("SIGKILL",occupancy._REMOTE_TERMINATE)
        self.assertNotIn("pkill",occupancy._REMOTE_TERMINATE)
        self.assertNotIn("killall",occupancy._REMOTE_TERMINATE)

        # Verify the exact remote classifier without evaluating its signaling
        # entrypoint. An unrelated training data path called "vllm" is NEVER
        # sufficient grounds for termination.
        funcs=[node for node in ast.parse(occupancy._REMOTE_TERMINATE).body
               if isinstance(node,ast.FunctionDef) and node.name=="direct_class"]
        import os
        env={"os":os}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),
                     "<remote-classifier>","exec"),env)
        classify=env["direct_class"]
        self.assertEqual(classify(["python3","-m","vllm.entrypoints.openai.api_server"]),
                         "vllm")
        self.assertEqual(classify(["python3","/opt/auto_occupy.py"]),"auto-occupy")
        self.assertIsNone(classify(["python3","train.py","--data","/data/vllm"]))
        self.assertIsNone(classify(["python3","train.py","--arg","auto-occupy"]))


if __name__ == "__main__":
    unittest.main()
