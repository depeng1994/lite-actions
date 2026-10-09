"""Check Example workflow name, Agent acceptance and GitHub Waiter stay aligned."""
import importlib.util
from pathlib import Path
import unittest

import unified_agent
from ci_core import reporting
from ci_core.config import load_config


BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "waiter_contract", BASE / "github/ci_wait_result.py"
)
waiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waiter)


class WorkflowContractTests(unittest.TestCase):
    def test_example_names_match_across_workflow_agent_waiter(self):
        config = load_config(BASE)
        for case_id, expected_name in (
            ("a3-8p", "A3-8p-CI-Example"),
            ("a3-16p", "A3-16p-CI-Example"),
            ("a5-64p", "A5-64p-CI"),
        ):
            with self.subTest(case=case_id):
                case = config[case_id]
                file = BASE / "github/workflows" / case["workflow"]
                data = file.read_text()
                self.assertIn(f"name: {expected_name}\n", data)
                self.assertEqual(unified_agent.STATUSES[case_id], expected_name)
                self.assertEqual(waiter.PIPELINES[case_id],
                                 (expected_name, case["pipeline_id"]))

    def test_dispatch_selection_is_in_workflow_not_pipeline_registry(self):
        config = load_config(BASE)
        for case_id in ("a3-8p", "a3-16p", "a5-64p"):
            with self.subTest(case_id=case_id):
                case = config[case_id]
                self.assertEqual(case["runner"], {"kind": "ssh"})
                yaml = (BASE / "github/workflows" / case["workflow"]).read_text()
                self.assertIn('test_case:', yaml)
                self.assertIn('run-name:', yaml)
                self.assertIn('inputs.test_case', yaml)
                pool = __import__('json').loads((BASE / "config/pools.json").read_text())[case["pool"]]
                self.assertTrue(all(not any(k in n for k in ("ascend_env", "hf_assets_path", "ckpt_init_path"))
                                    for n in pool["nodes"]))

    def test_old_names_can_still_be_received_from_old_master(self):
        config = load_config(BASE)
        for case_id in ("a3-8p", "a3-16p"):
            case = config[case_id]
            old = unified_agent.LEGACY_STATUSES[case_id]
            run = dict(id=124, head_branch="master", event="workflow_dispatch",
                       status="in_progress", head_sha="b"*40,
                       run_number=6, run_attempt=1, actor={"login":"depeng1994"},
                       display_title=old+" | test=dsv4_flash_a3_8p_example",
                       path=".github/workflows/"+case["workflow"], name=old)
            self.assertTrue(unified_agent.eligible(run, case))
            self.assertEqual(reporting.marker(case, run),
                             f"{old}-RESULT 124:1")


if __name__ == "__main__":
    unittest.main()
