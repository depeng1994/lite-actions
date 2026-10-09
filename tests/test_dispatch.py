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
