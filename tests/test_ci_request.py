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
from ci_core.registry import parse_request,load_profile
from tests.test_ssh_runner import archive_with,DEFAULT,TEST_8P

RUN={"id":12345,"run_attempt":1,"head_sha":"a"*40}
CASE={"test_id":TEST_8P, "params":{"STEPS":"3"}}
REQ={"schema":1,"run_id":12345,"attempt":1,"sha":"a"*40,"cases":[CASE]}

class RequestTests(unittest.TestCase):
    def test_binding_and_restricted_id(self):
        self.assertEqual(parse_request(REQ,RUN),[CASE])
        for key,value in (("run_id",99),("attempt",2),("sha","b"*40)):
            with self.subTest(key=key),self.assertRaises(ValueError):
                parse_request({**REQ,key:value},RUN)
        with self.assertRaisesRegex(ValueError,"case format"):
            parse_request({**REQ,"cases":[{**CASE,"path":"../../bin/sh"}]},RUN)
        with self.assertRaisesRegex(ValueError,"test ID"):
            parse_request({**REQ,"cases":[{**CASE,"test_id":"../../bin/sh"}]},RUN)

    def test_profile_derives_module_from_registry_and_checks_params(self):
        with tempfile.TemporaryDirectory() as td:
            archive=archive_with({TEST_8P:DEFAULT},Path(td)/"model.tar.gz")
            profile=load_profile(archive,TEST_8P,nodes=1,ngpu=8,
                                 params=CASE["params"])
            self.assertEqual(profile["env_vars"]["STEPS"],"3")
            with self.assertRaisesRegex(ValueError,"not allowed"):
                load_profile(archive,TEST_8P,nodes=1,ngpu=8,params={"NODE_IPS":"foo"})

    def test_missing_artifact_remains_pending(self):
        c=GitHubClient("TOKEN")
        with patch.object(c,"request",return_value={"artifacts":[]}):
            self.assertIsNone(c.ci_request(RUN))

    def test_artifact_retrieval_does_not_forward_gh_token_to_signed_url(self):
        c=GitHubClient("SECRET_TOKEN")
        listing={"artifacts":[{"name":"lite-ci-request-1","id":678,
                               "size_in_bytes":300,"expired":False,
                               "workflow_run":{"id":12345}}]}
        binary=io.BytesIO()
        with zipfile.ZipFile(binary,"w") as z:
            z.writestr("ci-request.json",json.dumps(REQ))
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
            self.assertEqual(c.ci_request(RUN),REQ)
            self.assertIn("SECRET_TOKEN",holder.request.headers.get("Authorization",""))

if __name__=="__main__":unittest.main()
