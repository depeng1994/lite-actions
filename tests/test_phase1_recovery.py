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

from ci_core import occupancy, scheduler, dispatcher
from ci_core.config import load_config
from ci_core.dispatch import Execution
from ci_core.job_state import JobState
from ci_core.protocol import plan_digest

ROOT = Path(__file__).resolve().parents[1]
TEST_ID = "dsv4_flash_a3_8p_example"
CASE = {"test_id": TEST_ID, "nnodes": 1, "ngpu": 8, "label": "Muon"}


def plan():
    base = {"schema": 2, "repo": "torchtitan-npu/torchtitan-npu",
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
                with scheduler.claim_dynamic(root, case, ascend_env=None,timeout=0.1) as chosen:
                    self.assertNotEqual(chosen["allocation"],
                                        [{"host_id":"a3-4","devices":list(range(8))}])
                self.assertEqual(preflight.call_count,1)

    def test_quarantined_a3_4_first_group_allows_clean_16p_combination(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(
                (ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:adamw",phase="reported",
                                  quarantined=True,
                                  allocation=[{"host_id":"a3-4",
                                               "devices":list(range(8))}])
            case=load_config(ROOT)["a3-16p"]
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.1) as chosen:
                    self.assertEqual([(n["host_id"],n["devices"][0])
                                      for n in chosen["allocation"]],
                                     [("a3-3",0),("a3-4",8)])

    def test_unscoped_quarantine_denies_16p(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(
                (ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:unknown",phase="reported",quarantined=True)
            case=load_config(ROOT)["a3-16p"]
            with self.assertRaises(scheduler.UnsafeResourceState):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.1):
                    pass

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

    def test_running_previous_v1_run_stops_global_serial_slot(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("999:1", phase="running",
                                  case="a3-8p", sha="a"*40)
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight"):
                with self.assertRaises(scheduler.UnsafeResourceState):
                    with scheduler.claim_dynamic(root,case,ascend_env=None,
                                                 timeout=0.1,owner_key="1000:1"):
                        pass

    def test_current_v1_owner_state_does_not_self_block(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("1024:1", phase="running",
                                  case="a3-8p", sha="a"*40)
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,case,ascend_env=None,
                                             timeout=0.1,owner_key="1024:1") as chosen:
                    self.assertEqual(len(chosen["allocation"]),1)

    def test_agent_crash_never_blindly_relaunches_case(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            run={**RUN, "_ci_request": plan()}
            key=dispatcher.task_key(run, TEST_ID)
            JobState(root).update(key, phase="running", sha="a"*40,
                                  allocation=[{"host_id":"a3-4","devices":list(range(8))}])
            with patch.object(dispatcher,"GitHubClient",FakeGitHub), \
                 patch.object(dispatcher,"authorization",return_value="fake"), \
                 patch.object(dispatcher,"execute") as execute:
                dispatcher.run_and_report(load_config(ROOT)["a3-8p"],run,root=root)
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
            with patch.object(dispatcher,"GitHubClient",FakeGitHub), \
                 patch.object(dispatcher,"authorization",return_value="fake"), \
                 patch.object(dispatcher,"execute") as execute:
                dispatcher.run_and_report(load_config(ROOT)["a3-8p"],run,root=root)
            execute.assert_not_called()
            self.assertEqual(FakeGitHub.published[0]["status"],"CANCELLED")

    def test_external_host_pid_cleanup_never_uses_docker_namespace(self):
        from types import SimpleNamespace
        pool=json.loads((ROOT/"config/pools.json").read_text())
        case={**load_config(ROOT)["a3-8p"],
              "allocation":[{"host_id":"a3-3","devices":[6]}]}
        calls=[]
        def fake_ssh(host, command, **kwargs):
            calls.append((host,command))
            self.assertEqual(host,"a3-3-relay")
            if command=="npu-smi info":
                return SimpleNamespace(returncode=0,
                    stdout="Process id in container\n| 3  0 | 2345 | dummy |\n")
            return SimpleNamespace(returncode=1,stdout="REFUSED not approved class")
        with patch.object(occupancy,"ssh",side_effect=fake_ssh):
            self.assertFalse(occupancy.reclaim_candidate(case,pool,ascend_env=None))
        self.assertEqual(len(calls),2)
        self.assertTrue(all(host=="a3-3-relay" for host,_ in calls))
        self.assertIn("2345",calls[1][1])
        self.assertNotIn("a3-3-docker-relay",str(calls))

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
        self.assertEqual(classify(["python3","/data/example/auto-occupy/auto_occupy_worker.py"]),"auto-occupy")
        self.assertIsNone(classify(["python3","/data/example/other/auto_occupy_worker.py"]))
        self.assertIsNone(classify(["python3","train.py","--data","/data/vllm"]))
        self.assertIsNone(classify(["python3","train.py","--arg","auto-occupy"]))


    def test_cancel_terminates_detached_job_ranks_without_touching_foreign_pid(self):
        """Real /proc cancellation: detached children are not in launcher PGID."""
        import os
        import subprocess
        import time
        from ci_core import ssh_runner
        marker = "phase1-unit-unique-marker"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def spawn(job):
                env = {**os.environ, "LITE_CI_JOB_ID": job}
                return subprocess.Popen(["sleep", "60"], env=env,
                                        start_new_session=True)
            launcher = spawn(marker)
            rank = spawn(marker)
            foreign = spawn("other-run-never-touch")
            (folder / "job.pgid").write_text(str(launcher.pid) + "\n")
            try:
                time.sleep(0.2)
                def local_ssh(host, cmd, **kwargs):
                    result = subprocess.run(["bash", "-lc", cmd],
                                            capture_output=True, text=True,
                                            timeout=kwargs.get("timeout", 35))
                    if result.returncode:
                        raise RuntimeError(result.stderr + result.stdout)
                    return result
                with patch.object(ssh_runner, "ssh", side_effect=local_ssh):
                    ssh_runner.cancel({"host_id": "mock", "ssh": "mock"}, str(folder), marker)
                self.assertIsNotNone(launcher.poll())
                self.assertIsNotNone(rank.poll())
                self.assertIsNone(foreign.poll())
            finally:
                for proc in (launcher, rank, foreign):
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)

    def test_authorized_auto_occupy_pid_termination_is_scoped(self):
        """Exercise actual classifier/signals on disposable CPU-only processes."""
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "auto-occupy"
            fake.symlink_to("/bin/sleep")
            owned = subprocess.Popen([str(fake), "60"], start_new_session=True)
            foreign = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                time.sleep(0.2)
                result = subprocess.run(
                    [sys.executable, "-c", occupancy._REMOTE_TERMINATE,
                     str(owned.pid)],
                    capture_output=True, text=True, timeout=12)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertIsNotNone(owned.poll())
                self.assertIsNone(foreign.poll())
            finally:
                for proc in (owned, foreign):
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
