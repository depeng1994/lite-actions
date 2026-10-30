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
from tests.support import load_config
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
                    {"host_id": "113.46.17.205:154", "devices": list(range(8))}])
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight") as preflight:
                with scheduler.claim_dynamic(root, case, ascend_env=None,timeout=0.1) as chosen:
                    self.assertNotEqual(chosen["allocation"],
                                        [{"host_id":"113.46.17.205:154","devices":list(range(8))}])
                self.assertEqual(preflight.call_count,1)

    def test_quarantined_a3_4_first_group_allows_clean_16p_combination(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(
                (ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:adamw",phase="reported",
                                  quarantined=True,
                                  allocation=[{"host_id":"113.46.17.205:154",
                                               "devices":list(range(8))}])
            case=load_config(ROOT)["a3-16p"]
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.1) as chosen:
                    self.assertEqual([(n["host_id"],n["devices"][0])
                                      for n in chosen["allocation"]],
                                     [("113.46.17.205:153",0),("113.46.17.205:154",8)])

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

    def test_running_previous_case_allows_disjoint_devices(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("oldrun:1:orphan", phase="running",
                                  allocation=[{"host_id":"113.46.17.205:154","devices":list(range(8))}])
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.2) as allocated:
                    self.assertFalse(set(allocated['allocation'][0]['devices']) == set(range(8))
                                     and allocated['allocation'][0]['host_id'] == '113.46.17.205:154')

    def test_running_previous_run_without_allocation_does_not_block(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            JobState(root).update("999:1", phase="running",
                                  case="a3-8p", sha="a"*40)
            case=load_config(ROOT)["a3-8p"]
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,case,ascend_env=None,
                                             timeout=0.2,owner_key="1000:1") as chosen:
                    self.assertEqual(len(chosen['allocation']), 1)

    def test_current_owner_state_does_not_self_block(self):
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
                                  allocation=[{"host_id":"113.46.17.205:154","devices":list(range(8))}])
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

    def test_failed_preflight_before_launch_does_not_quarantine(self):
        from ci_core.dispatch import ExternalNpuOccupancy
        case=load_config(ROOT)["a3-8p"]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            @contextmanager
            def claimed(_root,_case,**kwargs):
                yield {**_case,"allocation":[{"host_id":"113.46.17.205:154","devices":list(range(8))}],
                       "port_slot":0}
            run={**RUN,"_ci_request":plan()}
            with patch.object(dispatcher,"prepare_profile",return_value=(TEST_ID,{"ascend_env":"/trusted"})), \
                 patch.object(dispatcher,"claim_dynamic",side_effect=claimed), \
                 patch.object(dispatcher,"execute",side_effect=ExternalNpuOccupancy("external vllm")), \
                 patch.object(dispatcher,"GitHubClient",FakeGitHub), \
                 patch.object(dispatcher,"authorization",return_value="fake"):
                dispatcher.run_and_report(case,run,root=root)
            report=FakeGitHub.published[-1]
            state=JobState(root).read()[f"{run['id']}:1:{TEST_ID}"]
            self.assertEqual(report["status"],"RESOURCE_TIMEOUT")
            self.assertFalse(state.get("quarantined",False))
            self.assertEqual(state["phase"],"reported")


    def test_external_host_pid_cleanup_never_uses_docker_namespace(self):
        from types import SimpleNamespace
        pool=json.loads((ROOT/"config/pools.json").read_text())
        case={**load_config(ROOT)["a3-8p"],
              "allocation":[{"host_id":"113.46.17.205:153","devices":[6]}]}
        calls=[]
        def fake_ssh(host, command, **kwargs):
            calls.append((host,command))
            self.assertEqual(host,"113.46.17.205:153")
            if command=="npu-smi info":
                return SimpleNamespace(returncode=0,
                    stdout="Process id in container\n| 3  0 | 2345 | dummy |\n")
            return SimpleNamespace(returncode=1,stdout="REFUSED not approved class")
        with patch.object(occupancy,"ssh",side_effect=fake_ssh):
            self.assertFalse(occupancy.reclaim_candidate(case,pool,ascend_env=None))
        self.assertEqual(len(calls),2)
        self.assertTrue(all(host=="113.46.17.205:153" for host,_ in calls))
        self.assertIn("2345",calls[1][1])
        self.assertNotIn("113.46.17.205:153-docker-relay",str(calls))

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
        import re
        env={"os":os,"re":re}
        exec(compile(ast.Module(body=funcs,type_ignores=[]),
                     "<remote-classifier>","exec"),env)
        classify=env["direct_class"]
        self.assertEqual(classify(["python3","-m","vllm.entrypoints.openai.api_server"]),
                         "vllm")
        self.assertEqual(classify(["python3","/opt/auto_occupy.py"]),"auto-occupy")
        self.assertEqual(classify(["python3","/data/example/auto-occupy/auto_occupy_worker.py"]),"auto-occupy")
        self.assertIsNone(classify(["python3","/data/example/other/auto_occupy_worker.py"]))
        self.assertEqual(classify(["VLLM::Worker_TP0"]), "vllm")
        self.assertEqual(classify(["VLLM::Worker_TP15"]), "vllm")
        self.assertIsNone(classify(["ray::VLLMEngine"]))
        self.assertIsNone(classify(["rayTorchTitan"]))
        self.assertIsNone(classify(["VLLM::EngineCore"]))
        self.assertIsNone(classify(["python3", "train.py", "--role", "VLLM::Worker_TP0"]))
        self.assertIsNone(classify(["python3","train.py","--data","/data/vllm"]))
        self.assertIsNone(classify(["python3","train.py","--arg","auto-occupy"]))


    @unittest.skipUnless(__import__("sys").platform == "linux", "Linux /proc regression")
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

    @unittest.skipUnless(__import__("sys").platform == "linux", "Linux /proc regression")
    def test_authorized_vllm_worker_pid_termination_is_scoped(self):
        """Actual process signals on disposable CPU-only vLLM titled worker."""
        import subprocess
        import sys
        import time
        owned=subprocess.Popen(["bash","-c","exec -a VLLM::Worker_TP0 /bin/sleep 60"],
                               start_new_session=True)
        foreign=subprocess.Popen(["sleep","60"],start_new_session=True)
        try:
            time.sleep(.15)
            result=subprocess.run([sys.executable,"-c",occupancy._REMOTE_TERMINATE,
                                   str(owned.pid)],
                                  capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            owned.wait(timeout=3)
            self.assertIsNone(foreign.poll())
        finally:
            for proc in (owned,foreign):
                if proc.poll() is None: proc.kill()
                proc.wait(timeout=5)

    @unittest.skipUnless(__import__("sys").platform == "linux", "Linux /proc regression")
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
