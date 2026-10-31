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
from ci_core.contract import REPO, create_plan, normalize, parse_plan
from ci_core.vllm_ascend import definition
from ci_core.github import GitHubClient
from types import SimpleNamespace
assert REPO == 'torchtitan-npu/vllm-ascend'
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
        return dict(state='open',draft=False,user=dict(login='depeng1994'),
            base=dict(ref='main',repo=dict(full_name=REPO)),
            head=dict(sha=sha,repo=dict(full_name='depeng1994/vllm-ascend')))
run=dict(display_title='lite-a5-pr-7-'+sha,
    path='.github/workflows/lite_actions_dispatcher_smoke.yml',
    actor=dict(login='depeng1994'),head_sha='b'*40)
result=Fake('fake').normalize_target_pr_run(run)
assert result['head_sha']==sha and result['_ci_pr_number']==7
run['actor']['login']='other'
assert Fake('fake').normalize_target_pr_run(run) is None
"""
        env = {**os.environ, "LITE_ACTIONS_MODEL_REPO": "torchtitan-npu/vllm-ascend"}
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
