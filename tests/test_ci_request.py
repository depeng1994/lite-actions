"""CPU-only bounded Actions artifact handoff and pinned-case validation."""
from __future__ import annotations
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from ci_core.github import GitHubClient
from ci_core.registry import parse_request,resolve_request
from ci_core.protocol import plan_digest
from ci_core.source_repo import REPO as SOURCE_REPO
from tests.test_ssh_runner import TEST_8P

RUN={"id":12345,"run_attempt":1,"head_sha":"a"*40}
CASE={"test_id":TEST_8P}
REQ={"schema":1,"run_id":12345,"attempt":1,"sha":"a"*40,"cases":[CASE]}

class RequestTests(unittest.TestCase):
    def test_binding_and_restricted_id(self):
        self.assertEqual(parse_request(REQ,RUN),[CASE])
        for key,value in (("run_id",99),("attempt",2),("sha","b"*40)):
            with self.subTest(key=key),self.assertRaises(ValueError):
                parse_request({**REQ,key:value},RUN)
        with self.assertRaisesRegex(ValueError,"selector format"):
            parse_request({**REQ,"cases":[{**CASE,"path":"../../bin/sh"}]},RUN)
        with self.assertRaisesRegex(ValueError,"test ID"):
            parse_request({**REQ,"cases":[{**CASE,"test_id":"../../bin/sh"}]},RUN)

    def test_reject_all_parameter_fields_even_when_empty(self):
        for extra in ({'params':{}},{'params':{'STEPS':'5'}},
                      {'STEPS':'5'},{'path':'evil.py'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError,'format'):
                parse_request({**REQ,'cases':[{'test_id':TEST_8P,**extra}]},RUN)

    def test_missing_artifact_remains_pending(self):
        c=GitHubClient("TOKEN")
        with patch.object(c,"request",return_value={"artifacts":[]}):
            self.assertIsNone(c.ci_request(RUN))

    def test_artifact_retrieval_does_not_forward_gh_token_to_signed_url(self):
        c=GitHubClient("SECRET_TOKEN")
        listing={"artifacts":[{"name":"lite-ci-request-1","id":678,
                               "size_in_bytes":300,"expired":False,
                               "workflow_run":{"id":12345}}]}
        case = {"test_id": TEST_8P, "nnodes": 1, "ngpu": 8,
                "label": "Trusted 8P test"}
        raw = {"schema": 2, "repo": SOURCE_REPO, "run_id": RUN["id"],
               "attempt": RUN["run_attempt"], "sha": RUN["head_sha"],
               "pipeline": "a3-8p", "cases": [case]}
        trusted_plan = {**raw, "plan_digest": plan_digest(raw)}
        binary=io.BytesIO()
        with zipfile.ZipFile(binary,"w") as z:
            z.writestr("ci-request.json",json.dumps(trusted_plan))
        class RedirectError(Exception):
            pass
        from urllib.error import HTTPError
        class OpenHandler:
            def open(self,req,**kwargs):
                self.request=req
                raise HTTPError(req.full_url,302,"redirect",{"Location":"https://example.org/artifact.zip"},None)
        holder=OpenHandler()
        class File:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return binary.getvalue()
        def download(req,timeout):
            self.assertNotIn("Authorization",req.headers)
            return File()
        with patch.object(c,"request",return_value=listing), \
             patch("ci_core.github.urllib.request.build_opener",return_value=holder), \
             patch("ci_core.github.urllib.request.urlopen",side_effect=download):
            self.assertEqual(c.ci_request({**RUN, "path": ".github/workflows/a3-8p-lite-actions.yml"}),trusted_plan)
            self.assertIn("SECRET_TOKEN",holder.request.headers.get("Authorization",""))

    def test_commit_comment_lookup_is_bounded_and_paginated(self):
        c=GitHubClient("TOKEN")
        page1=[{"id": i} for i in range(100)]
        page2=[{"id": 100}]
        with patch.object(c,"request",side_effect=[page1,page2]) as request:
            comments=c.commit_comments("a"*40)
        self.assertEqual(len(comments),101)
        self.assertIn("page=1",request.call_args_list[0].args[1])
        self.assertIn("page=2",request.call_args_list[1].args[1])

        c=GitHubClient("TOKEN")
        with patch.object(c,"request",side_effect=[[{}]*100]*4):
            with self.assertRaisesRegex(RuntimeError,"too many commit comments"):
                c.commit_comments("a"*40)

if __name__=="__main__":unittest.main()
