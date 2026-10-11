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
        self.assertTrue(any(g["host_id"]=="a3-3" and g["devices"]==list(range(8,16))
                            for a in groups for g in a))
        for allocation in groups:
            scheduler.validate_allocation(config["a3-8p"],pools,allocation)
        scheduler.validate_allocation(config["a3-8p"],pools,
            [{"host_id":"a3-3","devices":list(range(8,16))}])
        multi=scheduler.candidates(config["a3-16p"],pools)
        self.assertEqual(len(multi),2)
        self.assertEqual({g["host_id"] for g in multi[0]},{"a3-3","a3-4"})

    def test_experimental_multinode_requires_explicit_opt_in(self):
        pools=json.loads((ROOT/"config/pools.json").read_text())
        case=load_config(ROOT)["a3-16p"]
        verified=pools["a3-shared"]["verified_multinode_allocations"]
        trial=pools["a3-shared"]["experimental_multinode_allocations"][0]
        with patch.dict("os.environ", {"LITE_ACTIONS_ALLOW_EXPERIMENTAL_TOPOLOGY":"0"}):
            self.assertEqual(scheduler.candidates(case,pools),verified)
            with self.assertRaises(ValueError):
                scheduler.validate_allocation(case,pools,trial)
        with patch.dict("os.environ", {"LITE_ACTIONS_ALLOW_EXPERIMENTAL_TOPOLOGY":"1"}):
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
            with patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"0"}), \
                 patch.object(scheduler,"remote_preflight",
                              side_effect=[RuntimeError("occupied"),None]) as preflight, \
                 patch("ci_core.occupancy.reclaim_candidate") as reclaim:
                with scheduler.claim_dynamic(root,config["a3-8p"],
                                              ascend_env="/trusted/set_env.sh",
                                              timeout=1,sleep=0.001) as runtime:
                    self.assertEqual(runtime["allocation"],options[1])
                self.assertEqual(preflight.call_count,2)
                reclaim.assert_not_called()

    def test_two_eight_p_cases_on_separate_hosts_run_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        cfg = load_config(ROOT)["a3-8p"]
        pools = json.loads((ROOT/"config/pools.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps(pools))
            ready = Barrier(2, timeout=3)
            def held_case(owner, host):
                chosen={**cfg, "test_forced_host": host}
                with scheduler.claim_dynamic(root,chosen,ascend_env=None,
                                             owner_key=owner,timeout=2,sleep=.01) as allocated:
                    ready.wait()
                    return allocated["allocation"][0]["host_id"]
            def forced_candidates(case, pools):
                return [[{"host_id": case["test_forced_host"], "devices": list(range(8))}]]
            # All test workers share one deterministic mock and a single lease root.
            with patch.object(scheduler, "candidates", side_effect=forced_candidates), \
                 patch.object(scheduler, "remote_preflight"):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    left = executor.submit(held_case,"201:1:case1", "a3-3")
                    right = executor.submit(held_case,"202:1:case2", "a3-4")
                    self.assertEqual({left.result(),right.result()},{"a3-3","a3-4"})

    def test_candidates_round_robin_hosts_without_state_race(self):
        pool=json.loads((ROOT/"config/pools.json").read_text())
        cfg=load_config(ROOT)["a3-8p"]
        choices=scheduler.candidates(cfg,pool)
        self.assertEqual([x[0]['host_id'] for x in choices[:3]],
                         ['a3-4','a3-3','a3-4'])

    def test_two_8p_prefer_independent_a3_hosts(self):
        from ci_core.job_state import JobState
        cfg=load_config(ROOT)["a3-8p"]
        pools=json.loads((ROOT/"config/pools.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps(pools))
            JobState(root).update("88:1:8p",phase="running",
                                  allocation=[{"host_id":"a3-4","devices":list(range(8))}])
            with patch.object(scheduler,"remote_preflight"):
                with scheduler.claim_dynamic(root,cfg,ascend_env=None,
                                             timeout=1,owner_key="89:1:8p") as chosen:
                    self.assertEqual(chosen['allocation'][0]['host_id'],'a3-3')

    def test_active_8p_blocks_16p_temporarily_without_quarantine(self):
        from ci_core.job_state import JobState
        from concurrent.futures import ThreadPoolExecutor
        import time
        root_case = load_config(ROOT)["a3-16p"]
        pools=json.loads((ROOT/"config/pools.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps(pools))
            old_key="88:1:active-eight-p"
            state=JobState(root)
            state.update(old_key,phase="running",
                         allocation=[{"host_id":"a3-4","devices":list(range(8,16))}])
            def after_release():
                time.sleep(.05)
                state.update(old_key,phase="reported",result="PASS")
            with patch.object(scheduler,"remote_preflight"):
                with ThreadPoolExecutor(max_workers=1) as ex:
                    future=ex.submit(after_release)
                    with scheduler.claim_dynamic(root,root_case,ascend_env=None,
                                                 timeout=1,sleep=.01,
                                                 owner_key="89:1:16p") as selected:
                        self.assertEqual(len(selected['allocation']),2)
                    future.result()
            self.assertFalse(state.read()[old_key].get('quarantined',False))

    def test_two_8p_actions_four_cases_run_on_four_disjoint_eight_p_groups(self):
        """CPU contention gate for two independent Run IDs x two Matrix Cases."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import copy
        cfg = load_config(ROOT)["a3-8p"]
        pools=json.loads((ROOT/"config/pools.json").read_text())
        pools=copy.deepcopy(pools)
        pools["a3-shared"]["max_jobs_per_host"]=2
        pools["a3-shared"]["nodes"][0]["allocation_groups"][1]["verified"]=True
        barrier=Barrier(4,timeout=6)
        def run_one(root,run_id,test_id):
            with scheduler.claim_dynamic(root,cfg,ascend_env=None,timeout=5,sleep=.01,
                                         owner_key=f"{run_id}:1:{test_id}") as task:
                barrier.wait()
                return (run_id,test_id,tuple((x['host_id'],tuple(x['devices'])) for x in task['allocation']),
                        task['master_port'],task['hccl_host_port_range'],task['hccl_npu_port_range'])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'config').mkdir()
            (root/'config/pools.json').write_text(json.dumps(pools))
            with patch.object(scheduler,'remote_preflight'), \
                 patch.object(scheduler,'shared_resource_lock_root',return_value=root/'leases'):
                with ThreadPoolExecutor(max_workers=4) as ex:
                    futures=[ex.submit(run_one,root,r,t) for r in (101,102) for t in ('muon','adamw')]
                    observed=[f.result() for f in futures]
            self.assertEqual(len({x[2] for x in observed}),4)
            # Reusing a numeric port on DIFFERENT hosts is safe; on one host
            # the two concurrent cases must have different ports/ranges.
            for host in ('a3-3','a3-4'):
                local=[x for x in observed if x[2][0][0] == host]
                self.assertEqual(len(local),2)
                self.assertEqual(len({x[3] for x in local}),2)
                self.assertEqual(len({x[4] for x in local}),2)
                self.assertEqual(len({x[5] for x in local}),2)
            self.assertEqual({x[0] for x in observed},{101,102})

    def test_a33_eight_p_coexists_with_a33_a34_sixteen_p(self):
        """N04: same physical node, two tasks, disjoint NPU and port leases."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import copy
        cfg = load_config(ROOT)
        pools=copy.deepcopy(json.loads((ROOT/'config/pools.json').read_text()))
        pools['a3-shared']['max_jobs_per_host']=2
        pools['a3-shared']['nodes'][0]['allocation_groups'][1]['verified']=True
        candidate_8=[{'host_id':'a3-3','devices':list(range(8))}]
        candidate_16=[{'host_id':'a3-3','devices':list(range(8,16))},
                      {'host_id':'a3-4','devices':list(range(8))}]
        barrier=Barrier(2,timeout=4)
        def forced(case,pools):
            return [candidate_8 if case['resources']['nodes']==1 else candidate_16]
        def claim(root,kind,key):
            with scheduler.claim_dynamic(root,cfg[kind],ascend_env=None,
                                         timeout=3,sleep=.01,owner_key=key) as lease:
                barrier.wait()
                return lease
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            (root/'config/pools.json').write_text(json.dumps(pools))
            with patch.object(scheduler,'candidates',side_effect=forced), \
                 patch.object(scheduler,'remote_preflight'), \
                 patch.object(scheduler,'shared_resource_lock_root',return_value=root/'leases'):
                with ThreadPoolExecutor(max_workers=2) as ex:
                    a=ex.submit(claim,root,'a3-8p','501:1:8p')
                    b=ex.submit(claim,root,'a3-16p','502:1:16p')
                    left,right=a.result(),b.result()
        self.assertEqual(left['allocation'],candidate_8)
        self.assertEqual(right['allocation'],candidate_16)
        self.assertNotEqual(left['master_port'],right['master_port'])
        self.assertNotEqual(left['hccl_npu_port_range'],right['hccl_npu_port_range'])

    def test_two_simultaneous_sixteen_p_bundles_are_disjoint(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import copy
        cfg=load_config(ROOT)['a3-16p']
        pools=copy.deepcopy(json.loads((ROOT/'config/pools.json').read_text()))
        a3=pools['a3-shared']
        a3['max_jobs_per_host']=2
        a3['nodes'][0]['allocation_groups'][1]['verified']=True
        # The existing experimental candidate is NOT disjoint from the
        # verified A3-3 0-7 + A3-4 8-15 bundle. Test the genuinely new
        # diagonal: A3-3 8-15 + A3-4 0-7 instead.
        assert [{'host_id':'a3-3','devices':list(range(8,16))},
                {'host_id':'a3-4','devices':list(range(8))}] in a3['verified_multinode_allocations']
        barrier=Barrier(2,timeout=5)
        def task(root,key):
            with scheduler.claim_dynamic(root,cfg,ascend_env=None,timeout=4,sleep=.01,
                                         owner_key=key) as x:
                barrier.wait()
                return (scheduler.resource_keys(x['allocation']),x['master_port'])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            (root/'config/pools.json').write_text(json.dumps(pools))
            with patch.object(scheduler,'remote_preflight'), \
                 patch.object(scheduler,'shared_resource_lock_root',return_value=root/'leases'):
                with ThreadPoolExecutor(max_workers=2) as ex:
                    f1=ex.submit(task,root,'103:1:16p_a')
                    f2=ex.submit(task,root,'104:1:16p_b')
                    a,b=f1.result(),f2.result()
            self.assertFalse(set(a[0])&set(b[0]))
            self.assertNotEqual(a[1],b[1])

    def test_approved_a3_reclaim_rechecks_before_allocation(self):
        case=load_config(ROOT)["a3-16p"]
        pools=json.loads((ROOT/"config/pools.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps(pools))
            with patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"1"}), \
                 patch.object(scheduler,"remote_preflight",
                              side_effect=[RuntimeError("occupied"), None]) as preflight, \
                 patch("ci_core.occupancy.reclaim_candidate",return_value=True) as reclaim:
                with scheduler.claim_dynamic(root,case,ascend_env=None,
                                             timeout=1,sleep=.001) as runtime:
                    self.assertIn(runtime["allocation"], scheduler.candidates(case,pools))
                # With a second certified 16P topology available, the
                # scheduler safely selects it instead of killing anything.
                reclaim.assert_not_called()
                self.assertEqual(preflight.call_count,2)

    def test_reclaim_once_when_only_verified_16p_pair_is_occupied(self):
        case=load_config(ROOT)['a3-16p']
        pools=json.loads((ROOT/'config/pools.json').read_text())
        pools['a3-shared']['verified_multinode_allocations']=[
            pools['a3-shared']['verified_multinode_allocations'][0]]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            (root/'config/pools.json').write_text(json.dumps(pools))
            with patch.dict('os.environ',{'LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES':'1'}), \
                 patch.object(scheduler,'remote_preflight',side_effect=[RuntimeError('occupied'),None]) as preflight, \
                 patch('ci_core.occupancy.reclaim_candidate',return_value=True) as reclaim:
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=.5,sleep=.01) as x:
                    self.assertEqual(x['allocation'],pools['a3-shared']['verified_multinode_allocations'][0])
                reclaim.assert_called_once()
                self.assertEqual(preflight.call_count,2)

    def test_reclaim_requires_per_pool_authorization(self):
        pools = json.loads((ROOT / "config/pools.json").read_text())
        self.assertTrue(pools["a3-shared"]["reclaim_external_processes"])
        self.assertFalse(pools["a5-64p-lab"]["reclaim_external_processes"])
        # An arbitrary pool without explicit approval must remain fail closed.
        self.assertFalse(pools["local-smoke"].get("reclaim_external_processes", False))

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

    def test_high_priority_16p_queued_intent_preserves_its_devices(self):
        from ci_core.job_state import JobState
        pool=json.loads((ROOT/'config/pools.json').read_text())
        case=load_config(ROOT)['a3-8p']
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'config').mkdir()
            (root/'config/pools.json').write_text(json.dumps(pool))
            (root/'config/pipelines.json').write_text((ROOT/'config/pipelines.json').read_text())
            JobState(root).update('new_high:1:16p',phase='queued',case='a3-16p',
                                  requested_devices=16,queued_at=1)
            with patch.object(scheduler,'remote_preflight'), \
                 patch.object(scheduler,'shared_resource_lock_root',return_value=root/'leases'):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=.25,
                                             sleep=.01,owner_key='new_low:1:8p') as low:
                    high=set(scheduler.resource_keys(pool['a3-shared']['verified_multinode_allocations'][0]))
                    self.assertFalse(high & set(scheduler.resource_keys(low['allocation'])))
            # Disabled 64P is never allowed to reserve capacity on A3.
            JobState(root).update('disabled:1:64p',phase='queued',case='a5-64p',
                                  requested_devices=64,queued_at=0)
            with patch.object(scheduler,'remote_preflight'), \
                 patch.object(scheduler,'shared_resource_lock_root',return_value=root/'leases'):
                with scheduler.claim_dynamic(root,case,ascend_env=None,timeout=.25,
                                             sleep=.01,owner_key='new_low2:1:8p') as low:
                    self.assertEqual(len(low['allocation']),1)

    def test_four_training_workers_and_priority_order(self):
        self.assertEqual(unified_agent.MAX_WORKERS, 5)
        self.assertEqual(unified_agent.TRAINING_WORKERS, 4)
        self.assertEqual(unified_agent.LIGHTWEIGHT_WORKERS, 1)
        a=load_config(ROOT)
        options=[{"id":81,"path":".github/workflows/a3_8p_lite_actions.yml"},
                 {"id":80,"path":".github/workflows/a3_16p_lite_actions.yml"},
                 {"id":70,"path":".github/workflows/a5_64p_lite_actions.yml"},
                 {"id":79,"path":".github/workflows/a3_8p_lite_actions.yml"}]
        admitted={case["workflow"]:case for case in a.values() if case["enabled"]}
        sorted_runs=unified_agent.prioritize_runs(options,admitted)
        self.assertEqual([x["id"] for x in sorted_runs],[80,79,81,70])
        self.assertEqual(unified_agent.requested_devices(a["a5-64p"]),64)
        # Disabled 64P must never gain a training priority slot.
        self.assertNotIn(a["a5-64p"]["workflow"],admitted)

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
                yield {**_case,"allocation":[{"host_id":"a3-4","devices":list(range(8))}],
                       "port_slot":0}
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
