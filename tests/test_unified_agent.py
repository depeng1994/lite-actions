"""No-network tests of shared agent filtering and result protocol."""
import unittest
from unittest.mock import patch
from ci_core.config import load_config
from pathlib import Path
import unified_agent as agent
from ci_core import reporting
from ci_core.protocol import plan_digest
from ci_core.source_repo import REPO


def trusted_plan(run, cases=None):
    cases = cases or [{'test_id':'dsv4_flash_a3_8p_example',
                      'nnodes':1,'ngpu':8,'label':'8P Muon'}]
    payload = {'schema':2, 'repo':REPO, 'run_id':run['id'],
               'attempt':run.get('run_attempt',1), 'sha':run['head_sha'],
               'pipeline':'a3-8p', 'cases':cases}
    return {**payload, 'plan_digest':plan_digest(payload)}


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=load_config(Path(agent.__file__).resolve().parent)
        cls._allowed_backup = agent.ALLOWED_ACTORS
        agent.ALLOWED_ACTORS = frozenset({"ci-maintainer"})

    @classmethod
    def tearDownClass(cls):
        agent.ALLOWED_ACTORS = cls._allowed_backup

    def base_run(self):
        return {"id":39100291,"run_number":9,"run_attempt":1,"head_sha":"a"*40,
                "created_at":"2026-10-09T01:00:00Z","event":"workflow_dispatch",
                "status":"in_progress","head_branch":"master",
                "actor":{"login":"ci-maintainer"},
                "path":".github/workflows/a3_8p_lite_actions.yml","name":"A3-8p-CI-Example",
                "display_title":"A3-8p-CI-Example | test=dsv4_flash_a3_8p_example"}

    def test_single_dispatcher_entrypoint_routes_by_authorized_runner_kind(self):
        from ci_core import dispatcher
        self.assertIs(agent.run_and_report, dispatcher.run_and_report)
        with patch.object(dispatcher, "_run_local_and_report") as local, \
             patch.object(dispatcher, "_run_matrix_and_report") as matrix:
            run = self.base_run()
            dispatcher.run_and_report(self.cases["dispatcher-smoke"], run,
                                      root=Path("/tmp/not-used"))
            local.assert_called_once()
            matrix.assert_not_called()
            local.reset_mock()
            with self.assertRaisesRegex(ValueError, "schema-2"):
                dispatcher.run_and_report(self.cases["a3-8p"], run,
                                          root=Path("/tmp/not-used"))
            local.assert_not_called()
            dispatcher.run_and_report(self.cases["a3-8p"],
                                      {**run, "_ci_request": {"schema": 2}},
                                      root=Path("/tmp/not-used"))
            matrix.assert_called_once()

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

    def test_progress_comment_is_short_and_idempotent(self):
        from ci_core.dispatcher import post_progress_running
        client = unittest.mock.MagicMock()
        run = self.base_run()
        client.commit_comments.return_value = []
        post_progress_running(client, run, pipeline="a3-8p",
                              test_id="dsv4_flash_a3_8p_example")
        client.send_result.assert_called_once()
        sha, body = client.send_result.call_args.args
        self.assertEqual(sha, run["head_sha"])
        self.assertEqual(body, "LCI-R 39100291:1:dsv4_flash_a3_8p_example")
        self.assertLess(len(body.encode()), 60)
        client.commit_comments.return_value = [
            {"user":{"login":"ci-maintainer"},"body":body}]
        client.send_result.reset_mock()
        with patch.dict("os.environ",{"LITE_ACTIONS_RESULT_AUTHOR":"ci-maintainer"}):
            post_progress_running(client, run, pipeline="a3-8p",
                                  test_id="dsv4_flash_a3_8p_example")
        client.send_result.assert_not_called()

    def test_pull_request_allowed_without_actor_or_branch_allowlist(self):
        run = {**self.base_run(), "event": "pull_request", "head_branch": "feature",
               "actor": {"login": "external-contributor"}, "_ci_pr_number": 6}
        self.assertTrue(agent.eligible(run, self.cases["a3-8p"]))
        self.assertFalse(agent.eligible({**run, "_ci_pr_number": None}, self.cases["a3-8p"]))

    def test_github_normalizes_pr_merge_sha_to_current_head(self):
        from ci_core.github import GitHubClient
        source_sha = "b" * 40
        run = {**self.base_run(), "event": "pull_request",
               "head_sha": source_sha, "head_repository": {"id": 44},
               "pull_requests": [{"number": 6, "head": {"sha": source_sha}}]}
        pr = {"state": "open", "head": {"sha": source_sha, "repo": {"id": 44}},
              "base": {"ref": "master", "repo": {"full_name": "torchtitan-npu/torchtitan-npu"}}}
        client = GitHubClient("stub")
        with patch.object(client, "request", return_value=pr):
            normalized = client.normalize_pr_run(run)
            self.assertEqual(normalized["head_sha"], source_sha)
            self.assertEqual(normalized["_ci_pr_number"], 6)
            self.assertIsNone(client.normalize_pr_run({**run, "pull_requests": [
                {"number": 6, "head": {"sha": "c" * 40}}]}))
            self.assertIsNone(client.normalize_pr_run({**run, "pull_requests": [
                {"number": 6, "head": {"sha": source_sha}},
                {"number": 7, "head": {"sha": source_sha}}]}))

        # GitHub may expose an empty pull_requests array for a fork PR run.
        def response(method, path):
            return [dict(pr, number=6)] if path.startswith("/pulls?") else pr
        with patch.object(client, "request", side_effect=response):
            normalized = client.normalize_pr_run({**run, "pull_requests": []})
            self.assertEqual(normalized["_ci_pr_number"], 6)
            self.assertEqual(normalized["head_sha"], source_sha)
            self.assertIsNone(client.normalize_pr_run({
                **run, "head_repository": {"id": 999}, "pull_requests": []}))
            self.assertIsNone(client.normalize_pr_run({
                **run, "head_sha": "c" * 40, "pull_requests": []}))

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


    def test_smoke_completes_while_training_worker_is_busy(self):
        """Real thread pools: smoke must run before the first train finishes."""
        from contextlib import nullcontext
        from tempfile import TemporaryDirectory
        from threading import Event, Lock

        smoke_done = Event()
        train_started = Event()
        observed = []
        guard = Lock()
        running_train = 0
        peak_train = 0

        class StubGitHub:
            def __init__(self, *_):
                pass
            def running_workflows(self):
                first = self_run()
                second = {**first, "id": first["id"]+1, "run_number": 10}
                smoke = {**first, "id": first["id"]+2, "run_number": 11,
                         "name": "Dispatcher-Smoke",
                         "path": ".github/workflows/lite_actions_dispatcher_smoke.yml"}
                return [first, second, smoke]
            def ci_request(self, run):
                return trusted_plan(run)

        def stub_execute(case, run, *, root):
            nonlocal running_train, peak_train
            if case["runner"]["kind"] == "local":
                self.assertTrue(train_started.wait(2), "training never began")
                with guard:
                    observed.append("smoke")
                smoke_done.set()
                return
            with guard:
                running_train += 1
                peak_train = max(peak_train, running_train)
                observed.append("train_start")
            train_started.set()
            self.assertTrue(smoke_done.wait(2), "smoke blocked behind training")
            with guard:
                observed.append("train_end")
                running_train -= 1

        self_run = self.base_run
        with TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(agent, "ROOT", root), \
                 patch.object(agent, "LOG", root/"logs/system/agent.log"), \
                 patch.object(agent, "load_config", return_value=self.cases), \
                 patch.object(agent, "single_agent", return_value=nullcontext()), \
                 patch.object(agent, "GitHubClient", StubGitHub), \
                 patch.object(agent, "authorization", return_value="stub"), \
                 patch.object(agent, "run_and_report", side_effect=stub_execute), \
                 patch("sys.argv", ["unified_agent.py", "--once"]):
                agent.main()
        self.assertEqual(peak_train, 2)
        self.assertEqual(observed.count("train_end"), 2)
        self.assertLess(observed.index("smoke"), observed.index("train_end"))

    def test_completed_unadmitted_higher_priority_does_not_block_new_cases(self):
        from tempfile import TemporaryDirectory
        from ci_core.job_state import JobState
        class StubAPI:
            def workflow_status(self, rid):
                self_id = 5001
                if rid != self_id:raise RuntimeError('unexpected Run')
                return {'status':'completed','conclusion':'cancelled'}
        with TemporaryDirectory() as td:
            state=JobState(Path(td))
            key='5001:1:dsv4_flash_a3_16p_example'
            state.update(key,phase='queued',case='a3-16p',requested_devices=16,queued_at=1)
            agent.prune_completed_unadmitted(state, StubAPI(), {}, [])
            self.assertEqual(state.read()[key]['phase'],'reported')
            self.assertEqual(state.read()[key]['result'],'ABANDONED')

    def test_matrix_cases_get_independent_futures_and_state(self):
        """Two jobs in the same GitHub run must not be serialized in one future."""
        from contextlib import nullcontext
        from tempfile import TemporaryDirectory
        from ci_core.job_state import JobState
        cases = [{'test_id':f'dsv4_flash_a3_8p_{i}', 'nnodes':1,
                  'ngpu':8, 'label':f'Case {i}'} for i in (1,2)]
        seen = []
        class Future:
            def result(self): return None
        class Executor:
            def __init__(self, max_workers): self.max_workers=max_workers
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def submit(self,fn,case,run,*,root):
                seen.append(run.get('_ci_case_id'))
                return Future()
        class API:
            def __init__(self, *_): pass
            def running_workflows(self): return [self_run()]
            def ci_request(self, run): return trusted_plan(run,cases)
        self_run = self.base_run
        with TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(agent,'ROOT',root), patch.object(agent,'LOG',root/'ci.log'), \
                 patch.object(agent,'load_config',return_value=self.cases), \
                 patch.object(agent,'single_agent',return_value=nullcontext()), \
                 patch.object(agent,'GitHubClient',API), \
                 patch.object(agent,'authorization',return_value='stub'), \
                 patch.object(agent,'ThreadPoolExecutor',Executor), \
                 patch('sys.argv',['unified_agent.py','--once']):
                agent.main()
            state=JobState(root).read()
            self.assertEqual(set(seen),{c['test_id'] for c in cases})
            for c in cases:
                self.assertEqual(state[f"39100291:1:{c['test_id']}"]['phase'],'queued')

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
                return trusted_plan(run)

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
