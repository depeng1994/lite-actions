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
                self.assertEqual(case["runner"]["kind"], "ssh")
                yaml = (BASE / "github/workflows" / case["workflow"]).read_text()
                self.assertIn('test_cases:', yaml)
                self.assertIn('run-name:', yaml)
                self.assertIn('inputs.test_cases', yaml)
                self.assertIn('actions/upload-artifact@v4', yaml)
                self.assertIn('name: Prepare CI request', yaml)
                pool = __import__('json').loads((BASE / "config/pools.json").read_text())[case["pool"]]
                self.assertTrue(all(not any(k in n for k in ("ascend_env", "hf_assets_path", "ckpt_init_path"))
                                    for n in pool["nodes"]))

    def test_waiter_budget_and_admission_are_finite_and_fail_closed(self):
        import re
        config=load_config(BASE)
        for name in ("a3-8p","a3-16p","a5-64p"):
            with self.subTest(name=name):
                yaml=(BASE/"github/workflows"/config[name]["workflow"]).read_text()
                max_cases=1 if name=="a5-64p" else 2
                self.assertIn(f'CI_MAX_CASES: "{max_cases}"',yaml)
                self.assertIn('name: Validate CI admission',yaml)
                self.assertIn('CI_REQUEST_REF:',yaml)
                self.assertIn('timeout-minutes: 300',yaml)
                match=re.search(r'CI_WAIT_TIMEOUT_SECONDS: "([0-9]+)"',yaml)
                self.assertIsNotNone(match)
                budget=int(match.group(1))
                self.assertGreaterEqual(budget,config[name]["timeout_seconds"]*max_cases+1000)
                self.assertLess(budget,300*60)
        a5=(BASE/"github/workflows/a5-64p-lite-actions.yml").read_text()
        self.assertIn('CI_CHANNEL_ENABLED: "false"',a5)
        for name in ("a3-8p-lite-actions.yml","a3-16p-lite-actions.yml"):
            self.assertIn('CI_CHANNEL_ENABLED: "true"',(BASE/"github/workflows"/name).read_text())

    def test_github_custom_run_name_matches_case_and_marker(self):
        case=load_config(BASE)["a3-8p"]
        text="A3-8p-CI-Example | test=dsv4_flash_a3_8p_example"
        run=dict(id=37939445270,head_branch="refactor/unified-a3-a5-ci",
                 event="workflow_dispatch",status="in_progress",head_sha="a"*40,
                 run_number=10,run_attempt=1,actor={"login":"depeng1994"},
                 display_title=text,name=text,path=".github/workflows/a3-8p-lite-actions.yml")
        from unittest.mock import patch
        with patch.object(unified_agent, "BRANCHES", {"refactor/unified-a3-a5-ci"}):
            self.assertTrue(unified_agent.eligible(run,case))
        self.assertEqual(reporting.marker(case,run),
                         "A3-8p-CI-Example-RESULT 37939445270:1")

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
