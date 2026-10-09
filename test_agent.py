import os
from pathlib import Path
import tempfile
import unittest
import agent

class AgentTests(unittest.TestCase):
    def test_tail_exactly_twenty(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.txt"
            log.write_text("".join(f"line {x}\n" for x in range(35)))
            lines = agent.tail_lines(log)
            self.assertEqual(len(lines), 20)
            self.assertEqual(lines[0], "line 15")
            self.assertEqual(lines[-1], "line 34")

    def test_failure_exit_code_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "fail.sh"
            script.write_text("#!/bin/bash\necho failing\nexit 7\n")
            old = agent.PIPELINES["npu-smi"]
            old_logs = agent.LOG_DIR
            try:
                agent.PIPELINES["npu-smi"] = script
                agent.LOG_DIR = Path(td)
                _, result, path = agent.execute({"id": 1234, "run_attempt": 1, "head_sha": "a" * 40,
                                                   "created_at": "2026-10-08T01:54:29Z"})
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["exit_code"], 7)
                self.assertIn("failing", result["last_20_lines"])
                self.assertEqual(path.parent.name, "2026-10-08_09-54-29_run-1234_attempt-1")
                self.assertEqual(path.name, "pipeline.log")
                agent.record_metrics(result, 1234, 2, 500, path)
                entry = __import__("json").loads(
                    (path.parent / "upload-metrics.json").read_text())
                self.assertEqual(entry["upload_bytes_application_estimate"], 1234)
            finally:
                agent.PIPELINES["npu-smi"] = old
                agent.LOG_DIR = old_logs

    def test_timezone_is_beijing_even_when_runner_uses_utc(self):
        utc = {"id": 101, "run_attempt": 2, "created_at": "2026-10-08T01:51:13Z"}
        offset = {"id": 101, "run_attempt": 2, "created_at": "2026-10-08T09:51:13+08:00"}
        expected = "2026-10-08_09-51-13_run-101_attempt-2"
        self.assertEqual(agent.task_log_dir(utc).name, expected)
        self.assertEqual(agent.task_log_dir(offset).name, expected)

    def test_public_result_has_no_token(self):
        report = dict(run_id=345, attempt=1, sha="b"*40,
                      pipeline="npu-smi", status="PASS", exit_code=0,
                      last_20_lines=["ok"])
        body = agent.comment_body(report)
        self.assertTrue(body.startswith("A3-2p-CI-RESULT 345:1\n"))
        self.assertLess(len(body.encode()), 7000)

if __name__ == "__main__":
    unittest.main()
