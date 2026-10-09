"""CPU-only verification: dispatcher owns log selection and GitHub reporting."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci_core import logs, reporting, dispatch


class TestDispatcherReporting(unittest.TestCase):
    def setUp(self):
        self.case = {"name":"a3-8p","pipeline_id":"a3-8p"}
        self.run = {"id":981,"run_attempt":1,"head_sha":"a"*40,
                    "name":"A3-8p-CI-Example"}

    def test_metrics_prefer_rank_zero_and_limit_twenty(self):
        data = "\n".join([f"[rank0] step: {x} loss: 1.0" for x in range(1,26)] +
                         ["[rank7] step: 25 loss: 1.0"])
        result = logs.select_lines(data, 0)
        self.assertEqual(len(result),20)
        self.assertTrue(all("rank0" in x for x in result))
        self.assertIn("step: 25", result[-1])

    def test_failure_prioritizes_error_context(self):
        data = "begin\nTraceback (most recent call last)\n"
        data += "RuntimeError: collective failure\n" + "\n".join(["other"]*20)
        lines=logs.select_lines(data, 1)
        self.assertLessEqual(len(lines),20)
        self.assertTrue(any("RuntimeError" in item for item in lines))

    def test_bounded_remote_and_no_secret_ssh_commands(self):
        seen=[]
        def ssh(args, **kw):
            seen.append(args)
            return subprocess.CompletedProcess(args,0,b"[rank0] step: 5 loss: 12.1\n",b"")
        result=logs.select_remote("a3-4-docker-relay",
            "/mnt/share/ci_tests/2026-10-09_run-1_attempt-1/run.log",0,
            approved_hosts={"a3-4-docker-relay"},runner=ssh)
        self.assertEqual(len(result),1)
        self.assertIn("tail -c 131072",seen[0][-1])
        self.assertNotIn("grep",seen[0][-1])
        for host,path in (("other-host","/mnt/share/ci_tests/a/run.log"),
                          ("a3-4-docker-relay","/etc/passwd"),
                          ("a3-4-docker-relay","/mnt/share/ci_tests/a/../run.log")):
            with self.assertRaises(ValueError):
                logs.remote_excerpt(host,path,approved_hosts={"a3-4-docker-relay"},runner=ssh)

    def test_dispatcher_refuses_false_pass_without_remote_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            p=base/"pipeline.log"
            p.write_text("RESULT_HOST: a3-4-docker-relay\n"
                         "RESULT_LOG: /mnt/share/ci_tests/valid/run.log\n")
            self.assertEqual(logs.metadata(p),{
                "host":"a3-4-docker-relay",
                "path":"/mnt/share/ci_tests/valid/run.log",
            })

    def test_python_reporter_posts_without_agent_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            log=base/"pipeline.log"
            log.write_text("dispatcher log\n")
            state=reporting.JobState(base)
            calls=[]
            class FakeClient:
                sent_bytes=480
                requests=2
                def __init__(self,token): pass
                def commit_comments(self,sha): return []
                def send_result(self,sha,body): calls.append((sha,body))
            report=reporting.make_report(self.case,self.run,0,["rank0: step 5"])
            with patch.object(reporting,"GitHubClient",FakeClient), \
                 patch.object(reporting,"authorization",return_value="fake"):
                reporting.post_result(self.case,self.run,report,log,state)
            self.assertEqual(len(calls),1)
            self.assertIn("A3-8p-CI-Example-RESULT 981:1",calls[0][1])
            self.assertEqual(state.read()["981:1"]["phase"],"reported")
            self.assertTrue((base/"upload-metrics.json").exists())


if __name__=="__main__":
    unittest.main()
