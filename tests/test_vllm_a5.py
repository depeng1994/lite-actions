"""Regression coverage for vLLM A5 PR normalization and trusted case selection."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VllmAdapterTests(unittest.TestCase):
    def test_vllm_profile_contract_and_target_pr(self):
        program = """
import os
from ci_core.contract import REPO, configured_repos, create_plan, normalize, parse_plan
from ci_core.vllm_ascend import definition
from ci_core.github import GitHubClient
from ci_core.ssh_runner import MODEL_REPO, VLLM
from types import SimpleNamespace
assert REPO == 'cann-framework/vllm-ascend'
assert configured_repos() == (REPO,) and VLLM and MODEL_REPO == REPO
defaults = dict(worker='yellow',target_pool='a5_shared_yellow',docker='vllm-kimi-wangjinyin')
cases = []
for name in ('dispatch_smoke_a5_8p','dispatch_smoke_a5_32p'):
    row=definition(name)
    test=SimpleNamespace(test_name=name,test_descr=row['label'],nnodes=row['nnodes'],
        ngpu=row['ngpu'],ci_kind=row['kind'],ci_overrides={},timeout=row['timeout'])
    cases.append(normalize(test,defaults,len(cases)))
sha='a'*40
plan=create_plan(cases,run_id=20,attempt=1,sha=sha,workflow='lite_actions_dispatcher_smoke.yml',defaults=defaults)
assert len(parse_plan(plan,dict(id=20,run_attempt=1,head_sha=sha),'lite_actions_dispatcher_smoke.yml'))==2
class Fake(GitHubClient):
    def request(self,method,path,data=None):
        assert method == 'GET' and path == '/pulls/7'
        return dict(state='open',draft=False,user=dict(login='depeng1994'),
            base=dict(ref='main',repo=dict(full_name=REPO)),
            head=dict(sha=sha,repo=dict(id=42,full_name='any-fork/vllm-ascend')))
workflow_sha='b'*40
for title in ('lite-a5-pr-7-'+sha, 'lite-vllm-pr-7-'+sha+'-'+workflow_sha):
    run=dict(display_title=title,
        path='.github/workflows/lite_actions_dispatcher_smoke.yml',
        actor=dict(login='depeng1994'),head_sha=sha)
    result=Fake('fake').normalize_target_pr_run(run)
    assert result['head_sha']==sha and result['_ci_pr_number']==7
    assert Fake('fake').normalize_target_pr_run({**run,'actor':dict(login='other')})['_ci_source_repo_id'] == 42
    assert Fake('fake').normalize_target_pr_run({**run,'display_title':title.replace(sha,'c'*40)}) is None
    assert Fake('fake').normalize_target_pr_run({**run,'path':'wrong.yml'}) is None
assert Fake('fake').normalize_target_pr_run({**run,'display_title':title+'bad'}) is None
os.environ['LITE_ACTIONS_VLLM_ALLOWED_BRANCHES'] = 'main,ci/test'
recheck = dict(display_title='lite-vllm-recheck-7-'+workflow_sha+'-'+'f'*32,
    path='.github/workflows/lite_actions_dispatcher_smoke.yml', event='workflow_dispatch',
    actor=dict(login='depeng1994'),head_sha=workflow_sha,head_branch='ci/test')
normalized = Fake('fake').normalize_native_recheck(recheck)
assert normalized['head_sha'] == sha and normalized['_ci_control_sha'] == workflow_sha
assert normalized['_ci_source_repo_id'] == 42
assert Fake('fake').normalize_native_recheck({**recheck,'head_branch':'unapproved'}) is None
"""
        for repo in ("cann-framework/vllm-ascend", "torchtitan-npu/vllm-ascend"):
            with self.subTest(repo=repo):
                env = {**os.environ, "LITE_ACTIONS_MODEL_REPO": repo}
                env.pop("LITE_ACTIONS_MODEL_REPOS", None)
                proc = subprocess.run(
                    [sys.executable, "-c", program],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
