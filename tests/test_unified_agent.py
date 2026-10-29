"""No-network tests of shared agent filtering and result protocol."""
import unittest
from unittest.mock import patch
from ci_core.config import load_config
from pathlib import Path
import unified_agent as agent
from ci_core import reporting


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=load_config(Path(agent.__file__).resolve().parent)

    def base_run(self):
        return {"id":39100291,"run_number":9,"run_attempt":1,"head_sha":"a"*40,
                "created_at":"2026-10-09T01:00:00Z","event":"workflow_dispatch",
                "status":"in_progress","head_branch":"master",
                "actor":{"login":"depeng1994"},
                "path":".github/workflows/a3_8p_lite_actions.yml","name":"A3-8p-CI-Example",
                "display_title":"A3-8p-CI-Example | test=dsv4_flash_a3_8p_example"}

    def test_valid_dispatch(self):
        self.assertTrue(agent.eligible(self.base_run(),self.cases["a3-8p"]))

    def test_old_name_kept_compatible_until_upstream_updates(self):
        run=self.base_run()
        run["name"]="A3-8p-CI"
        case=self.cases["a3-8p"]
        self.assertTrue(agent.eligible(run,case))
        self.assertTrue(reporting.report_payload(
            case,run,reporting.make_report(case,run,0,["PASS"])
        ).startswith("A3-8p-CI-RESULT 39100291:1\n"))

    def test_missing_or_invalid_run_number_denied(self):
        for number in (None, 0, "6", True):
            run=self.base_run()
            run["run_number"]=number
            self.assertFalse(agent.eligible(run,self.cases["a3-8p"]))

    def test_untrusted_actor_denied(self):
        run=self.base_run()
        run["actor"]["login"]="attacker"
        self.assertFalse(agent.eligible(run,self.cases["a3-8p"]))

    def test_wrong_branch_denied(self):
        run=self.base_run()
        run["head_branch"]="untrusted-pr"
        self.assertFalse(agent.eligible(run,self.cases["a3-8p"]))

    def test_wrong_path_denied(self):
        run=self.base_run()
        run["path"]=".github/workflows/a3_16p_lite_actions.yml"
        self.assertFalse(agent.eligible(run,self.cases["a3-8p"]))

    def test_a5_cannot_be_submitted(self):
        run=self.base_run()
        self.assertFalse(agent.eligible(run,self.cases["a5-64p"]))

    def test_result_matches_old_waiter_contract(self):
        run=self.base_run()
        case=self.cases["a3-8p"]
        result=reporting.make_report(case,run,0,["PASS"])
        self.assertEqual(result["pipeline"],"a3-8p")
        self.assertTrue(reporting.report_payload(case,run,result).startswith(
            "A3-8p-CI-Example-RESULT 39100291:1\n"))


    def test_two_workflows_are_queued_until_executor_starts_them(self):
        """A queued Future must not masquerade as a crashed training Case."""
        from contextlib import nullcontext
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from ci_core.job_state import JobState

        class Future:
            def result(self):
                return None
        class StubExecutor:
            def __init__(self, max_workers):
                self.max_workers=max_workers
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def submit(self, fn, *args, **kwargs):
                return Future()
        class StubGitHub:
            def __init__(self, *_):
                pass
            def running_workflows(self):
                first=self_run()
                second={**first,"id":39100292,"run_number":10}
                return [first,second]
            def ci_request(self, run):
                return {"schema":2}

        self_run=self.base_run
        with TemporaryDirectory() as td:
            root=Path(td)
            with patch.object(agent,"ROOT",root), patch.object(agent,"LOG",root/"logs/system/agent.log"), \
                 patch.object(agent,"load_config",return_value=self.cases), \
                 patch.object(agent,"single_agent",return_value=nullcontext()), \
                 patch.object(agent,"GitHubClient",StubGitHub), \
                 patch.object(agent,"authorization",return_value="stub"), \
                 patch.object(agent,"ThreadPoolExecutor",StubExecutor), \
                 patch("sys.argv",["unified_agent.py","--once"]):
                agent.main()
            state=JobState(root).read()
            self.assertEqual(state["39100291:1"]["phase"],"queued")
            self.assertEqual(state["39100292:1"]["phase"],"queued")


if __name__=="__main__":
    unittest.main()
