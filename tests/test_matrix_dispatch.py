"""CI serial Matrix, verified topology, per-case reporting: CPU-only."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import unified_agent
from ci_core.config import load_config
from ci_core import dispatcher, scheduler, occupancy
from ci_core.dispatch import Execution, task_dir
from ci_core.protocol import plan_digest


ROOT = Path(__file__).resolve().parents[1]
SHA = "a"*40
CASES = [
    {"test_id":"dsv4_flash_a3_8p_example","ngpu":8,"nnodes":1,"label":"Muon"},
    {"test_id":"dsv4_flash_a3_8p_adamw","ngpu":8,"nnodes":1,"label":"AdamW"},
]


def run_plan(cases=CASES):
    plan={"schema":2,"repo":"torchtitan-npu/torchtitan-npu",
          "run_id":1092,"attempt":1,"sha":SHA,"pipeline":"a3-8p","cases":cases}
    plan["plan_digest"]=plan_digest(plan)
    return plan


RUN={"id":1092,"run_attempt":1,"run_number":3,"head_sha":SHA,
     "created_at":"2026-10-10T08:00:00Z","name":"A3-8p-CI-Example",
     "display_title":"A3-8p-CI-Example"}


class MatrixDispatchTests(unittest.TestCase):
    def test_result_marker_is_single_release_format(self):
        from ci_core.dispatcher import case_marker
        self.assertEqual(case_marker({"id": 42, "run_attempt": 1},
                                     "dsv4_flash_a3_8p_example"),
                         "Lite-CI-RESULT 42:1:dsv4_flash_a3_8p_example")

    def test_verified_groups_are_safe_and_no_unverified_a3_3_second_group(self):
        pools=json.loads((ROOT/"config/pools.json").read_text())
        config=load_config(ROOT)
        groups=scheduler.candidates(config["a3-8p"],pools)
        self.assertGreaterEqual(len(groups),2)
        self.assertFalse(any(g["host_id"]=="a3-3" and g["devices"]==list(range(8,16))
                             for a in groups for g in a))
        for allocation in groups:
            scheduler.validate_allocation(config["a3-8p"],pools,allocation)
        with self.assertRaises(ValueError):
            scheduler.validate_allocation(config["a3-8p"],pools,
                [{"host_id":"a3-3","devices":list(range(8,16))}])
        multi=scheduler.candidates(config["a3-16p"],pools)
        self.assertEqual(len(multi),1)
        self.assertEqual({g["host_id"] for g in multi[0]},{"a3-3","a3-4"})

    def test_experimental_multinode_requires_explicit_opt_in(self):
        pools=json.loads((ROOT/"config/pools.json").read_text())
        case=load_config(ROOT)["a3-16p"]
        verified=pools["a3-shared"]["verified_multinode_allocations"]
        trial=pools["a3-shared"]["experimental_multinode_allocations"][0]
        with patch.dict("os.environ", {"A3_CI_ALLOW_EXPERIMENTAL_TOPOLOGY":"0"}):
            self.assertEqual(scheduler.candidates(case,pools),verified)
            with self.assertRaises(ValueError):
                scheduler.validate_allocation(case,pools,trial)
        with patch.dict("os.environ", {"A3_CI_ALLOW_EXPERIMENTAL_TOPOLOGY":"1"}):
            self.assertEqual(scheduler.candidates(case,pools),verified+[trial])
            scheduler.validate_allocation(case,pools,trial)

    def test_dynamic_lock_and_preflight_selects_unoccupied_second_group(self):
        config=load_config(ROOT)
        pools=json.loads((ROOT/"config/pools.json").read_text())
        options=scheduler.candidates(config["a3-8p"],pools)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps(pools))
            with patch.dict("os.environ", {"A3_CI_RECLAIM_EXTERNAL_PROCESSES":"0"}), \
                 patch.object(scheduler,"remote_preflight",
                              side_effect=[RuntimeError("occupied"),None]) as preflight, \
                 patch("ci_core.occupancy.reclaim_candidate") as reclaim:
                with scheduler.claim_dynamic(root,config["a3-8p"],
                                              ascend_env="/trusted/set_env.sh",
                                              timeout=1,sleep=0.001) as runtime:
                    self.assertEqual(runtime["allocation"],options[1])
                self.assertEqual(preflight.call_count,2)
                reclaim.assert_not_called()

    def test_case_execution_resource_busy_not_retried(self):
        from ci_core.resources import ResourceBusy
        case=load_config(ROOT)["a3-8p"]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(
                (ROOT/"config/pools.json").read_text())
            with patch.object(scheduler,"remote_preflight") as preflight:
                with self.assertRaisesRegex(ResourceBusy,"from execution"):
                    with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=0.1):
                        raise ResourceBusy("from execution")
                self.assertEqual(preflight.call_count,1)

    def test_phase1_global_worker_is_serial(self):
        self.assertEqual(unified_agent.MAX_WORKERS,1)

    def test_npu_process_classification_maps_card_die_without_kill(self):
        out=("Process id in container\n"
             "| 3  0 | 2345 | dummy |\n"
             "| 3  1 | 3456 | dummy |\n")
        self.assertEqual(occupancy.selected_pids(out,[6],indexing="card-die"),[2345])
        self.assertEqual(occupancy.selected_pids(out,[7],indexing="card-die"),[3456])
        with self.assertRaises(ValueError):
            occupancy.selected_pids("missing",[6],indexing="card-die")

    def test_two_cases_reported_independently_without_real_ssh(self):
        original=load_config(ROOT)["a3-8p"]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text((ROOT/"config/pools.json").read_text())
            seen=[]
            reports=[]
            @contextmanager
            def claimed(_root,_case,**kwargs):
                yield {**_case,"allocation":[{"host_id":"a3-4","devices":list(range(8))}]}
            def execute_case(case, subrun, **kwargs):
                self.assertTrue(kwargs["pre_reserved"])
                assert len(seen)==0 or seen[-1] != subrun["_ci_test_id"]
                seen.append(subrun["_ci_test_id"])
                # Actual resolver inside execute normally populates this.
                subrun["_ci_expanded_cases"]=[{"test_id":subrun["_ci_test_id"]}]
                log=task_dir(subrun,root)/"pipeline.log"
                log.parent.mkdir(parents=True,exist_ok=True)
                log.write_text("CASE_PLAN "+subrun["_ci_test_id"]+"\n"
                               "CASE_RESULT "+subrun["_ci_test_id"]+" PASS 0\n")
                return Execution(0,["step 5 PASS"],log)
            class FakeGitHub:
                def __init__(self,*args):pass
                def commit_comments(self,sha):return []
                def send_result(self,sha,body):reports.append(json.loads(body.split("\n",1)[1]))
            run={**RUN,"_ci_request":run_plan()}
            with patch.object(dispatcher,"prepare_profile",
                              side_effect=lambda case,run:(run["_ci_test_id"],{"ascend_env":"/trusted"})), \
                 patch.object(dispatcher,"claim_dynamic",side_effect=claimed), \
                 patch.object(dispatcher,"execute",side_effect=execute_case), \
                 patch.object(dispatcher,"GitHubClient",FakeGitHub), \
                 patch.object(dispatcher,"authorization",return_value="fake"):
                dispatcher.run_and_report(original,run,root=root)
            self.assertEqual(seen,[x["test_id"] for x in CASES])
            self.assertEqual([x["status"] for x in reports],["PASS","PASS"])
            self.assertEqual([x["test_id"] for x in reports],seen)
            from ci_core.job_state import JobState
            states=JobState(root).read()
            self.assertEqual([states[dispatcher.task_key(run,id)]["phase"]
                              for id in seen],["reported","reported"])


if __name__=="__main__":
    unittest.main()
