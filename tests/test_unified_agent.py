"""No-network tests of shared agent filtering and result protocol."""
import unittest
from unittest.mock import patch
from ci_core.config import load_config
from pathlib import Path
import unified_agent as agent


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=load_config(Path(agent.__file__).resolve().parent)

    def base_run(self):
        return {"id":39100291,"run_attempt":1,"head_sha":"a"*40,
                "created_at":"2026-10-09T01:00:00Z","event":"workflow_dispatch",
                "status":"in_progress","head_branch":"master",
                "actor":{"login":"depeng1994"},
                "path":".github/workflows/a3-8p-ci.yml","name":"A3-8p-CI"}

    def test_valid_dispatch(self):
        self.assertTrue(agent.eligible(self.base_run(),self.cases["a3-8p"]))

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
        run["path"]=".github/workflows/a3-16p-ci.yml"
        self.assertFalse(agent.eligible(run,self.cases["a3-8p"]))

    def test_a5_cannot_be_submitted(self):
        run=self.base_run()
        self.assertFalse(agent.eligible(run,self.cases["a5-64p"]))

    def test_result_matches_old_waiter_contract(self):
        run=self.base_run()
        case=self.cases["a3-8p"]
        result=agent.make_report(case,run,0,["PASS"])
        self.assertEqual(result["pipeline"],"dsv4-flash-8p")
        self.assertTrue(agent.report_payload(case,run,result).startswith(
            "A3-8p-CI-RESULT 39100291:1\n"))


if __name__=="__main__":
    unittest.main()
