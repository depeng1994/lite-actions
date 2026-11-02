"""Release invariants not established by artifact existence or green workflows."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ci_core.gitcode_contract import ContractError, aggregate_results, source_identity, validate_pipeline
from ci_core.gitcode_state import Coordinator, StateError
from tests.test_gitcode_contract import fixture, receipt_for
from tests.test_gitcode_pipeline import HostingAPI


class ResultEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.plan, _ = fixture()
        self.receipt = receipt_for(self.plan)
        self.results = [
            {"case_id": item["case_id"], "receipt": self.receipt,
             "identity": source_identity(self.plan), "status": "PASS", "exit_code": 0,
             "cleanup_verified": True, "summary": []}
            for item in self.plan["cases"]
        ]

    def test_nonzero_exit_can_never_be_a_pass(self):
        for code in (1, 124, 125, 130, -9):
            self.results[0]["exit_code"] = code
            with self.subTest(code=code), self.assertRaisesRegex(ContractError, "PASS_EXIT_CODE"):
                aggregate_results(self.plan, self.receipt, self.results, current=True)

    def test_missing_or_noninteger_exit_code_is_rejected(self):
        for code in (True, False, None, "0", 0.0, 256, -256):
            self.results[0]["exit_code"] = code
            with self.subTest(code=code), self.assertRaises(ContractError):
                aggregate_results(self.plan, self.receipt, self.results, current=True)
        del self.results[0]["exit_code"]
        with self.assertRaises(ContractError):
            aggregate_results(self.plan, self.receipt, self.results, current=True)

    def test_control_characters_cannot_enter_result_diagnostics(self):
        for text in ("line\x00secret", "line\tvalue", "\x1b[31mred"):
            self.results[0]["summary"] = [text]
            with self.subTest(text=repr(text)), self.assertRaisesRegex(ContractError, "DIAGNOSTIC"):
                aggregate_results(self.plan, self.receipt, self.results, current=True)


class PipelineIdentityTests(unittest.TestCase):
    def test_smoke_cannot_impersonate_nightly_or_vice_versa(self):
        smoke = ".github/workflows/lite_actions_dispatcher_smoke.yml"
        nightly = ".github/workflows/nightly_fulltests.yml"
        for pipeline, workflow, cases in (
            ("nightly", smoke, [{"kind": "train"}]),
            ("smoke", nightly, [{"kind": "worker_probe"}]),
            ("smoke", smoke, [{"kind": "train"}]),
            ("nightly", nightly, [{"kind": "npu_smoke"}]),
        ):
            with self.subTest(pipeline=pipeline, workflow=workflow), self.assertRaises(ContractError):
                validate_pipeline(pipeline, workflow, cases)
        validate_pipeline("nightly", nightly, [{"kind": "train"}])

    def test_producer_rejects_wrong_pipeline_before_first_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            api = HostingAPI(Path(temp))
            api.intent["pipeline"] = "nightly"
            with self.assertRaisesRegex(ContractError, "PIPELINE_WORKFLOW"):
                api.produce()
            self.assertEqual(api.uploads, [])


class RecoveryVersionTests(unittest.TestCase):
    def test_result_free_v1_migration_keeps_outbox_and_leases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "coordinator.sqlite3"
            original = Coordinator(path)
            with original._transaction() as db:
                original._outbox(db, "CANCEL_RUN", "kept", {"reason": "not forgotten"})
            before = original.pending_outbox()
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=1")
            restored = Coordinator(path)
            self.assertEqual(restored.pending_outbox(), before)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_v1_results_without_exit_evidence_are_not_assumed_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "coordinator.sqlite3"
            original = Coordinator(path)
            with original._transaction() as db:
                original._outbox(db, "CASE_RESULT", "old-result", {"status": "PASS", "cleanup_verified": True})
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=1")
            with self.assertRaisesRegex(StateError, "MIGRATION_REQUIRES_EXIT_EVIDENCE"):
                Coordinator(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                result = json.loads(db.execute("SELECT payload_json FROM outbox").fetchone()[0])
                self.assertNotIn("exit_code", result)

    def test_cancellation_is_selected_before_a_full_page_of_failed_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Coordinator(Path(temp) / "state.sqlite3")
            with state._transaction() as db:
                for i in range(150):
                    state._outbox(db, "CASE_RESULT", f"case-{i}", {"fixture": i})
                state._outbox(db, "CANCEL_EXECUTION", "urgent", {"execution_id": "urgent"})
            self.assertEqual(state.pending_outbox(limit=1)[0]["kind"], "CANCEL_EXECUTION")


if __name__ == "__main__":
    unittest.main()
