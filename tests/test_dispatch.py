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

    def test_result_log_selection(self):
        with tempfile.TemporaryDirectory() as td:
            log=Path(td)/"pipeline.log"
            log.write_text("first\nRESULT_LAST_20_LINES_BEGIN\nmetric1\nmetric2\n"
                           "RESULT_LAST_20_LINES_END\nafter\n")
            self.assertEqual(dispatch.summarize(log,0),["metric1","metric2"])

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
            "2026-10-09_17-43-56_run_A3-16p-CI_6",
        )
        # Dispatcher keeps globally unique IDs for state, audit and GitHub POST.
        self.assertEqual(
            dispatch.task_dir(run, Path("/tmp")).name,
            "2026-10-09_17-43-56_run-37913151439_attempt-1",
        )
        run["run_attempt"] = 2
        self.assertEqual(
            dispatch.remote_task_name(run),
            "2026-10-09_17-43-56_run_A3-16p-CI_6_attempt-2",
        )

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
