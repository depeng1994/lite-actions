"""Broker integration across Prepare, authority, source retention and claims."""

import copy
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_admission import AdmissionError, GitCodeAdmissionBroker
from ci_core.gitcode_contract import request_id
from ci_core.gitcode_state import Authority, Coordinator, EventProof, StateError
from ci_core.gitcode_store import GitCodeRequestStore
from tests.test_gitcode_pipeline import HostingAPI


class AdmissionBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = HostingAPI(self.root)
        self.plan = self.api.produce()
        self.coordinator = Coordinator(self.root / "coordinator.sqlite3")
        self.authority = Authority(copy.deepcopy(self.plan["pr"]), 1)
        self.proof = EventProof("event-1", "snapshot-1", 1, 1)
        self.broker = GitCodeAdmissionBroker(
            reader=self.api.reader,
            coordinator=self.coordinator,
            store=GitCodeRequestStore(self.root / "retained"),
            workspace=self.root / "materializing",
            read_authority=lambda repo, number: self.authority,
            verify_event=lambda intent: self.proof,
            clock=lambda: 1100,
        )

    def count(self, table):
        self.assertIn(table, {"deliveries", "executions", "leases", "requests"})
        with sqlite3.connect(self.coordinator.path) as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def complete(self):
        self.api.job.update(status="completed", conclusion="success")
        return self.broker.admit(100)

    def test_early_intent_only_registers_cancellation_ownership(self):
        self.assertIsNone(self.broker.admit(100))
        self.assertEqual(self.count("deliveries"), 1)
        self.assertEqual(self.count("executions"), 0)
        self.assertEqual(self.count("leases"), 0)
        self.assertEqual(list(self.broker.store.root.iterdir()), [])

    def test_closed_pr_cancels_registered_prepare_without_plan_admission(self):
        self.broker.admit(100)
        self.authority = Authority(self.authority.pr, 2, state="closed")
        result = self.broker.reconcile_registered()
        self.assertEqual(result["unavailable"], [])
        self.assertEqual([x["kind"] for x in self.coordinator.pending_outbox()], ["CANCEL_RUN"])
        self.assertEqual(self.count("executions"), 0)

    def test_duplicate_poll_does_not_add_execution_or_resource_lease(self):
        first = self.complete()
        second = self.broker.admit(100)
        self.assertEqual(first.admission.receipt, second.admission.receipt)
        self.assertEqual(self.count("executions"), 1)
        self.assertEqual(self.count("requests"), 1)
        self.assertEqual(self.count("leases"), 0)
        self.assertEqual(self.broker.startable_source(first).source.tested_merge_sha, self.plan["merge"]["tested_merge_sha"])

    def test_no_authenticated_event_proof_means_no_execution(self):
        self.proof = {"event_order": 1, "version_revision": 1}
        with self.assertRaisesRegex(AdmissionError, "AUTHENTICATED_EVENT_PROOF"):
            self.complete()
        self.assertEqual(self.count("executions"), 0)
        self.assertEqual(self.count("deliveries"), 0)

    def test_observed_aba_history_cannot_reissue_old_snapshot(self):
        accepted = self.complete()
        newer = {**self.authority.pr, "head_sha": "0" * 40}
        self.authority = Authority(newer, 2)
        self.broker.reconcile_registered()
        self.authority = Authority(self.plan["pr"], 3)
        self.broker.reconcile_registered()
        with self.assertRaises(StateError):
            self.broker.startable_source(accepted)
        with self.assertRaises(StateError):
            self.broker.admit(100)
        self.assertEqual(self.count("executions"), 1)

    def test_artifact_deletion_does_not_destroy_outbox_identity(self):
        accepted = self.complete()
        self.api.metadata.clear()
        self.api.zips.clear()
        self.api.calls.clear()
        self.assertEqual(self.broker.resolve_request(request_id(self.plan)).plan, self.plan)
        self.assertEqual(self.api.calls, [])
        self.broker.startable_source(accepted).verify_files()
        self.assertFalse(any("artifacts" in path for _, path in self.api.calls))

    def test_authority_outage_persists_no_new_start_permission(self):
        accepted = self.complete()
        self.broker.read_authority = Mock(side_effect=OSError("authority offline"))
        self.assertTrue(self.broker.reconcile_registered()["unavailable"])
        with self.assertRaises(StateError):
            self.coordinator.assert_startable(accepted.execution_id, now=1100)

    def test_disabled_provider_never_queries_platform(self):
        self.api.reader.policy = replace(self.api.policy, enabled=False)
        self.api.calls.clear()
        with self.assertRaisesRegex(AdmissionError, "PROVIDER_DISABLED"):
            self.broker.admit(100)
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
