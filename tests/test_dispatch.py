import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci_core import dispatch
from ci_core.config import load_config


class DispatchTests(unittest.TestCase):
    def test_physical_die_mapping(self):
        out = """Process id in container
| 3       0                 | 2345      | dummy       |
| 3       1                 | 3456      | dummy       |
| 4       0                 | 4567      | dummy       |"""
        self.assertEqual(dispatch.selected_processes(out,[6,7]),
                         ["chip 6: pid 2345", "chip 7: pid 3456"])
        self.assertEqual(dispatch.selected_processes(out,[8]),
                         ["chip 8: pid 4567"])
        self.assertEqual(dispatch.selected_processes(out,[0,1]), [])
        self.assertEqual(dispatch.selected_processes(out,[3],indexing="card"),
                         ["chip 3: pid 2345", "chip 3: pid 3456"])
        with self.assertRaises(ValueError):
            dispatch.selected_processes(out,[0],indexing="unknown")
        with self.assertRaises(ValueError):
            dispatch.selected_processes("failed", [0])

    def test_remote_preflight_uses_host_namespace_and_detects_selected_group(self):
        root=Path(__file__).resolve().parents[1]
        pools=json.loads((root/"config/pools.json").read_text())
        case={"pool":"a3-shared","allocation":[{
            "host_id":"a3-4","devices":list(range(8,16))}]}
        output=("Process id in container\n"
                "| 4       0                 | 4567      | external     |\n")
        completed=dispatch.subprocess.CompletedProcess([],0,stdout=output,stderr="")
        with patch.object(dispatch.subprocess,"run",return_value=completed) as run:
            with self.assertRaisesRegex(RuntimeError,"chip 8: pid 4567"):
                dispatch.remote_preflight(case,pools,ascend_env="/container/cann/set_env.sh")
        argv=run.call_args.args[0]
        self.assertIn("a3-4-relay",argv)
        self.assertNotIn("a3-4-docker-relay",argv)
        self.assertNotIn("source",argv[-1])

    def test_late_external_occupancy_has_single_scoped_reclaim(self):
        from ci_core import occupancy
        root=Path(__file__).resolve().parents[1]
        pools=json.loads((root/"config/pools.json").read_text())
        case={**load_config(root)["a3-8p"],
              "allocation":[{"host_id":"a3-4","devices":list(range(8))}]}
        occupied=dispatch.ExternalNpuOccupancy("External NPU activity on a3-4")
        with patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"1"}), \
             patch.object(dispatch,"remote_preflight",side_effect=[occupied,None]) as preflight, \
             patch.object(occupancy,"reclaim_candidate",return_value=True) as reclaim:
            dispatch.preflight_before_launch(case,pools,ascend_env=None,pre_reserved=True)
        self.assertEqual(preflight.call_count,2)
        reclaim.assert_called_once()
        for allowed in (False, True):
            with self.subTest(approved=allowed), \
                 patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"1"}), \
                 patch.object(dispatch,"remote_preflight",side_effect=occupied), \
                 patch.object(occupancy,"reclaim_candidate") as reclaim:
                other={**case,"pool":"a5-64p-lab"} if not allowed else case
                with self.assertRaises(dispatch.ExternalNpuOccupancy):
                    dispatch.preflight_before_launch(other,pools,ascend_env=None,
                                                     pre_reserved=not allowed)
                reclaim.assert_not_called()
        with patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"0"}), \
             patch.object(dispatch,"remote_preflight",side_effect=occupied), \
             patch.object(occupancy,"reclaim_candidate") as reclaim:
            with self.assertRaises(dispatch.ExternalNpuOccupancy):
                dispatch.preflight_before_launch(case,pools,ascend_env=None,pre_reserved=True)
            reclaim.assert_not_called()

    def test_reclaim_retry_still_fails_closed_if_occupied(self):
        from ci_core import occupancy
        root=Path(__file__).resolve().parents[1]
        pools=json.loads((root/"config/pools.json").read_text())
        case={**load_config(root)["a3-8p"],
              "allocation":[{"host_id":"a3-4","devices":list(range(8))}]}
        with patch.dict("os.environ", {"LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES":"1"}), \
             patch.object(dispatch,"remote_preflight",
                          side_effect=dispatch.ExternalNpuOccupancy("occupied")) as preflight, \
             patch.object(occupancy,"reclaim_candidate",return_value=True) as reclaim:
            with self.assertRaises(dispatch.ExternalNpuOccupancy):
                dispatch.preflight_before_launch(case,pools,ascend_env=None,pre_reserved=True)
            self.assertEqual(preflight.call_count,2)
            reclaim.assert_called_once()

    def test_result_log_selection(self):
        with tempfile.TemporaryDirectory() as td:
            log=Path(td)/"pipeline.log"
            log.write_text("first\nRESULT_LAST_20_LINES_BEGIN\nmetric1\nmetric2\n"
                           "RESULT_LAST_20_LINES_END\nafter\n")
            self.assertEqual(dispatch.summarize(log,0),["metric1","metric2"])

    def test_preflight_environment_is_derived_from_resolved_case_not_channel_config(self):
        from ci_core import ssh_runner
        case=load_config(Path(__file__).resolve().parents[1])['a3-8p']
        self.assertEqual(case['runner'], {'kind':'ssh'})
        run={'id':101,'run_attempt':1,'head_sha':'a'*40,
             '_ci_request':{'schema':1,'run_id':101,'attempt':1,
                            'sha':'a'*40,'cases':[{'suite':'a3_8p_tests'}]}}
        resolved=[{'test_id':'dsv4_flash_a3_8p_example',
                   'env_vars':{'ASCEND_SET_ENV_PATH':'/model/versioned/cann/set_env.sh',
                               'HF_ASSETS_PATH':'/model/weights',
                               'CKPT_INIT_LOAD_PATH':'/model/init'}},
                  {'test_id':'dsv4_flash_a3_8p_adamw',
                   'env_vars':{'ASCEND_SET_ENV_PATH':'/model/versioned/cann/set_env.sh',
                               'HF_ASSETS_PATH':'/model/weights',
                               'CKPT_INIT_LOAD_PATH':'/model/init',
                               'OPTIMIZER_OVERRIDES':''}}]
        with patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
             patch.object(dispatch,'resolve_request',return_value=resolved):
            test_id,profile=dispatch.prepare_profile(case,run)
        self.assertEqual(test_id,'dsv4_flash_a3_8p_example')
        self.assertEqual(profile,{'ascend_env':'/model/versioned/cann/set_env.sh'})
        self.assertEqual(run['_ci_expanded_cases'],resolved)

    def test_disabled_a5_refused_before_hardware(self):
        root=Path(__file__).resolve().parents[1]
        case=load_config(root)["a5-64p"]
        with self.assertRaisesRegex(ValueError,"Disabled"):
            dispatch.execute(case, {"id": 1, "created_at":"2026-10-09T00:00:00Z"},root=root)

    def test_execution_node_dir_matches_workflow_and_run_number(self):
        run = {"id": 37913151439, "run_number": 6, "run_attempt": 1,
               "name": "A3-16p-CI-Example", "created_at": "2026-10-09T09:43:56Z"}
        self.assertEqual(
            dispatch.remote_task_name(run),
            "2026-10-09_17-43-56_run_A3-16p-CI-Example_6",
        )
        # Dispatcher keeps globally unique IDs for state, audit and GitHub POST.
        self.assertEqual(
            dispatch.task_dir(run, Path("/tmp")).name,
            "2026-10-09_17-43-56_run-37913151439_attempt-1",
        )
        run["run_attempt"] = 2
        self.assertEqual(
            dispatch.remote_task_name(run),
            "2026-10-09_17-43-56_run_A3-16p-CI-Example_6_attempt-2",
        )

    def test_custom_github_run_name_preserves_static_workflow_directory(self):
        run = {"id":37939445270,"run_number":10,"run_attempt":1,
               "name":"A3-8p-CI-Example | test=dsv4_flash_a3_8p_example",
               "display_title":"A3-8p-CI-Example | test=dsv4_flash_a3_8p_example",
               "created_at":"2026-10-09T13:48:04Z"}
        self.assertEqual(dispatch.remote_task_name(run),
                         "2026-10-09_21-48-04_run_A3-8p-CI-Example_10")

    def test_execution_node_dir_handles_other_trusted_workflow_names(self):
        run = {"id": 123456, "run_number": 27, "run_attempt": 1,
               "name": "A5-64p-CI", "created_at": "2026-10-09T08:00:00Z"}
        self.assertEqual(dispatch.remote_task_name(run),
                         "2026-10-09_16-00-00_run_A5-64p-CI_27")
        run["name"] = "A3-8p-CI"
        self.assertEqual(dispatch.remote_task_name(run),
                         "2026-10-09_16-00-00_run_A3-8p-CI_27")

    def test_execution_node_dir_rejects_untrusted_metadata(self):
        run = {"id": 123, "run_number": 6, "run_attempt": 1,
               "name": "A3-16p-CI", "created_at": "2026-10-09T09:43:56Z"}
        for value in (None, 0, -5, "6", True):
            with self.subTest(number=value):
                with self.assertRaises(ValueError):
                    dispatch.remote_task_name({**run, "run_number": value})
        for value in (None, 0, -1, "2", True):
            with self.subTest(attempt=value):
                with self.assertRaises(ValueError):
                    dispatch.remote_task_name({**run, "run_attempt": value})
        for value in ("../other", "A3 16p CI", "CI_foo", "A3-16p-CI\n"):
            with self.subTest(workflow=value):
                with self.assertRaises(ValueError):
                    dispatch.remote_task_name({**run, "name": value})

    def test_real_local_pipeline_runs_without_npu(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"config").mkdir()
            (root/"pipeline.sh").write_text("echo local-test\n")
            (root/"config/pools.json").write_text(json.dumps({"local-smoke":{"type":"local","nodes":[]}}))
            case={"runner":{"kind":"local"},"allocation":[],"pool":"local-smoke",
                  "timeout_seconds":5,"enabled":True}
            run={"id":100,"created_at":"2026-10-09T00:00:00Z",
                 "head_sha":"a"*40,"run_attempt":1}
            with patch.object(dispatch,"remote_preflight") as preflight:
                result=dispatch.execute(case,run,root=root)
                preflight.assert_called_once()
            self.assertEqual(result.exit_code,0)
            self.assertEqual(result.tail,["local-test"])


if __name__ == "__main__":
    unittest.main()
