"""GitHub result waiter contract and independent GitHub queue config tests."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("a3_ci_waiter", ROOT/"github/ci_wait_result.py")
waiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waiter)


class WaiterTests(unittest.TestCase):
    def test_payload_verified_and_old_protocol_compatible(self):
        data = {"run_id":42,"attempt":1,"sha":"b"*40,
                "pipeline":"a3-8p","status":"PASS",
                "exit_code":0,"last_20_lines":["ok"]}
        import json
        body="A3-8p-CI-Example-RESULT 42:1\n"+json.dumps(data)
        comments=[{"user":{"login":"depeng1994"},"body":body}]
        self.assertEqual(waiter.find_result(comments,"A3-8p-CI-Example-RESULT 42:1",
                        "b"*40,42,1,"a3-8p"),data)
        self.assertIsNone(waiter.find_result(comments,"A3-8p-CI-Example-RESULT 42:1",
                        "b"*40,42,1,"a3-16p"))

    def test_all_workflows_queue_without_cancel(self):
        for file in ("lite-actions-dispatcher-smoke.yml","a3-8p-lite-actions.yml","a3-16p-lite-actions.yml"):
            text=(ROOT/"github/workflows"/file).read_text()
            self.assertIn("queue: max",text)
            self.assertIn("cancel-in-progress: false",text)
            self.assertIn(".github/scripts/lite_actions/wait_result.py",text)


if __name__=="__main__":
    unittest.main()
