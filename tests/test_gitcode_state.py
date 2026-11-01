"""CPU fault injection of the transactional ledger; no platform revision claims."""

import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ci_core.gitcode_contract import digest, external_id, request_id, seal_plan, verify_request
from ci_core.gitcode_state import Authority, Coordinator, EventProof, StateError
from tests.test_gitcode_contract import fixture


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "coordinator.sqlite3"
        self.coordinator = Coordinator(self.path)
        self.plan, self.ctx = fixture()
        self.authority = Authority(copy.deepcopy(self.plan["pr"]), 1)
        self.coordinator.reconcile("42", 1, lambda: self.authority, now=1100)
        self.proof = EventProof("event-1", "snapshot-1", 1, 1)

    def delivery(self, plan=None):
        plan = plan or self.plan
        gh = plan["github"]
        return {"request_id": request_id(plan), "repo_id": gh["repo_id"], "run_id": gh["run_id"],
                "run_attempt": gh["run_attempt"], "control_sha": plan["control"]["control_sha"]}

    def register(self, plan=None, ctx=None, proof=None):
        plan, ctx, proof = plan or self.plan, ctx or self.ctx, proof or self.proof
        return self.coordinator.register_intent(ctx["intent"], self.delivery(plan), proof)

    def claim(self, plan=None, ctx=None):
        return self.coordinator.claim(verify_request(plan or self.plan, **(ctx or self.ctx)), now=1101)

    def admitted(self):
        self.register()
        return self.claim()

    def new_delivery(self, run_id=200):
        plan, ctx = copy.deepcopy(self.plan), copy.deepcopy(self.ctx)
        plan["github"]["run_id"] = run_id
        plan["github"]["prepare_job_id"] = run_id + 1
        ctx["run"]["id"] = run_id
        ctx["prepare_job"].update(id=run_id + 1, run_id=run_id)
        ctx["manifest"]["github_run_id"] = run_id
        for role, ref in plan["artifacts"].items():
            ref["name"] = ref["name"].replace("-100-1", f"-{run_id}-1")
            ref["id"] += run_id
            if role == "source":
                ctx["manifest"]["source_artifact_id"] = ref["id"]
        plan["artifacts"]["manifest"]["content_sha256"] = digest(ctx["manifest"])
        for role, ref in plan["artifacts"].items():
            ctx["artifacts"][role] = {**ref, "repo_id": plan["github"]["repo_id"], "run_id": run_id, "run_attempt": 1, "expired": False}
        plan = seal_plan(plan)
        for case_id, slot in plan["result_slots"].items():
            slot.update(id=slot["id"] + run_id, run_id=run_id, external_id=external_id(plan, case_id))
        ctx["slots"] = copy.deepcopy(plan["result_slots"])
        return seal_plan(plan), ctx

    def test_duplicate_delivery_claims_entire_canonical_execution(self):
        first = self.admitted()
        plan, ctx = self.new_delivery()
        self.register(plan, ctx)
        second = self.claim(plan, ctx)
        self.assertTrue(second.receipt["duplicate"])
        self.assertEqual(first.receipt["canonical_execution_id"], second.receipt["canonical_execution_id"])
        self.assertEqual(second.canonical["request_id"], request_id(self.plan))
        self.assertEqual(second.canonical["result_slots"], self.plan["result_slots"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM executions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM cases").fetchone()[0], 2)

    def test_concurrent_repeated_claim_is_idempotent(self):
        self.register()
        verified = verify_request(self.plan, **self.ctx)
        with ThreadPoolExecutor(max_workers=8) as workers:
            receipts = list(workers.map(lambda _: self.coordinator.claim(verified, now=1101).receipt, range(16)))
        self.assertEqual(len({r["canonical_execution_id"] for r in receipts}), 1)
        self.assertTrue(all(r == receipts[0] for r in receipts))
        self.assertEqual(len(self.coordinator.pending_outbox()), 1)

    def test_unregistered_request_cannot_start(self):
        with self.assertRaisesRegex(StateError, "EARLY_INTENT_NOT_REGISTERED"):
            self.claim()

    def test_head_base_retarget_close_and_auth_changes_cancel_early_and_running(self):
        for change in ("head_sha", "base_sha", "base_ref", "closed", "authorization"):
            with self.subTest(change=change):
                path = Path(self.temp.name) / (change + ".sqlite3")
                coordinator = Coordinator(path)
                coordinator.reconcile("42", 1, lambda: self.authority, now=1100)
                coordinator.register_intent(self.ctx["intent"], self.delivery(), self.proof)
                admission = coordinator.claim(verify_request(self.plan, **self.ctx), now=1101)
                pr = copy.deepcopy(self.authority.pr)
                if change in pr:
                    pr[change] = "release" if change == "base_ref" else "0" * 40
                newer = Authority(pr, 2, state="closed" if change == "closed" else "open", authorized=change != "authorization")
                self.assertEqual(coordinator.reconcile("42", 1, lambda: newer, now=1102), 2)
                with self.assertRaisesRegex(StateError, "REVOKED"):
                    coordinator.assert_startable(admission.receipt["canonical_execution_id"], now=1103)
                self.assertEqual({x["kind"] for x in coordinator.pending_outbox()}, {"ADMISSION_RECEIPT", "CANCEL_RUN", "CANCEL_EXECUTION"})

    def test_aba_never_revives_registered_intent(self):
        admission = self.admitted()
        b = copy.deepcopy(self.authority.pr)
        b["base_sha"] = "0" * 40
        self.coordinator.reconcile("42", 1, lambda: Authority(b, 2), now=1200)
        self.coordinator.reconcile("42", 1, lambda: Authority(self.authority.pr, 3), now=1300)
        with self.assertRaisesRegex(StateError, "REVOKED_INTENT_CANNOT_REVIVE"):
            self.register()
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(admission.receipt["canonical_execution_id"], now=1301)

    def test_first_late_intent_needs_current_revision_proof(self):
        b = copy.deepcopy(self.authority.pr)
        b["base_sha"] = "0" * 40
        self.coordinator.reconcile("42", 1, lambda: Authority(b, 2), now=1200)
        self.coordinator.reconcile("42", 1, lambda: Authority(self.authority.pr, 3), now=1300)
        with self.assertRaisesRegex(StateError, "LATE_INTENT_HAS_NO_CURRENT_REVISION_PROOF"):
            self.register()

    def test_reordered_authority_and_revision_collision_fail_closed(self):
        self.coordinator.reconcile("42", 1, lambda: Authority(self.authority.pr, 3), now=1200)
        with self.assertRaisesRegex(StateError, "REGRESSION"):
            self.coordinator.reconcile("42", 1, lambda: self.authority, now=1300)
        b = copy.deepcopy(self.authority.pr)
        b["head_sha"] = "0" * 40
        with self.assertRaisesRegex(StateError, "COLLISION"):
            self.coordinator.reconcile("42", 1, lambda: Authority(b, 3), now=1300)

    def test_authority_network_failure_rolls_back(self):
        self.register()
        def unavailable():
            raise OSError("network unavailable")
        with self.assertRaises(OSError):
            self.coordinator.reconcile("42", 1, unavailable, now=1200)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT generation,revision FROM pr_versions").fetchone(), (1, 1))

    def test_new_spec_revokes_prior_pipeline_before_prepare_finishes(self):
        admission = self.admitted()
        intent = copy.deepcopy(self.ctx["intent"])
        intent["selectors"] = [{"test_id": "dispatch_smoke_a3_16p"}]
        intent["bridge"].update(dispatch_intent_id="intent-2", source_snapshot_id="snapshot-2", bridge_event_id="event-2")
        delivery = {**self.delivery(), "request_id": "1438625957/200/1", "run_id": 200}
        self.coordinator.register_intent(intent, delivery, EventProof("event-2", "snapshot-2", 2, 1))
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(admission.receipt["canonical_execution_id"], now=1102)
        old_late = copy.deepcopy(self.ctx["intent"])
        old_late["bridge"]["dispatch_intent_id"] = "late-unseen"
        with self.assertRaisesRegex(StateError, "OUT_OF_ORDER"):
            self.coordinator.register_intent(old_late, {**delivery, "request_id": "1438625957/201/1", "run_id": 201}, self.proof)

    def test_rerun_requires_authorization_and_allocates_new_generation(self):
        self.admitted()
        intent = copy.deepcopy(self.ctx["intent"])
        intent["bridge"].update(dispatch_intent_id="rerun-2", source_snapshot_id="snapshot-2", bridge_event_id="event-2", rerun_intent_id="rerun-user-2")
        delivery = {**self.delivery(), "request_id": "1438625957/200/1", "run_id": 200}
        with self.assertRaisesRegex(StateError, "RERUN_NOT_AUTHORIZED"):
            self.coordinator.register_intent(intent, delivery, EventProof("event-2", "snapshot-2", 2, 1))
        self.coordinator.register_intent(intent, delivery, EventProof("event-2", "snapshot-2", 2, 1, True))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT execution_generation,publication_revision FROM pipeline_heads").fetchone(), (2, 2))

    def bind(self, admission, lease="lease-1"):
        execution = admission.receipt["canonical_execution_id"]
        case_id = self.plan["cases"][1]["case_id"]
        self.assertTrue(self.coordinator.claim_case(execution, case_id, now=1102))
        cleanup = {"nodes": [{"host_id": "fixture-host", "container_id": "a" * 64, "image_id": "sha256:" + "b" * 64,
                              "task_marker": "marker-1", "device_ids": list(range(8))}]}
        token = self.coordinator.bind_lease(execution, case_id, lease, cleanup, now=1103)
        return execution, case_id, token

    def test_fencing_old_cleanup_cannot_release_new_or_other_lease(self):
        admission = self.admitted()
        execution, case_id, token = self.bind(admission)
        self.assertFalse(self.coordinator.complete_lease(execution, case_id, "different-lease", token, cleanup_verified=True))
        self.assertFalse(self.coordinator.complete_lease(execution, case_id, "lease-1", token + 1, cleanup_verified=True))
        self.coordinator.cancel_execution(execution)
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(execution, now=1104)
        # Its own cleanup remains permitted after publication/start revocation.
        self.assertTrue(self.coordinator.complete_lease(execution, case_id, "lease-1", token, cleanup_verified=True))
        self.assertEqual(self.coordinator.recoverable_leases(), [])

    def test_unknown_cleanup_quarantines_and_recovery_needs_no_artifact(self):
        admission = self.admitted()
        execution, case_id, token = self.bind(admission)
        self.assertFalse(self.coordinator.complete_lease(execution, case_id, "lease-1", token, cleanup_verified=False))
        restored = Coordinator(self.path)
        leases = restored.recoverable_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0]["state"], "QUARANTINED")
        self.assertEqual(leases[0]["cleanup"]["nodes"][0]["container_id"], "a" * 64)
        self.assertFalse(restored.complete_lease(execution, case_id, "lease-1", token, cleanup_verified=True))

    def test_case_claim_exactly_once_and_unclean_pass_is_rejected(self):
        admission = self.admitted()
        execution, case_id, token = self.bind(admission)
        self.assertFalse(self.coordinator.claim_case(execution, case_id, now=1104))
        with self.assertRaisesRegex(StateError, "UNCLEAN_CASE"):
            self.coordinator.record_case_result(execution, case_id, {"status": "PASS", "cleanup_verified": False})
        with self.assertRaisesRegex(StateError, "LEASE_CLEANUP_NOT_COMPLETE"):
            self.coordinator.record_case_result(execution, case_id, {"status": "PASS", "cleanup_verified": True})
        self.coordinator.complete_lease(execution, case_id, "lease-1", token, cleanup_verified=True)
        self.coordinator.record_case_result(execution, case_id, {"status": "PASS", "cleanup_verified": True})
        with self.assertRaisesRegex(StateError, "RESULT_IMMUTABLE"):
            self.coordinator.record_case_result(execution, case_id, {"status": "FAIL", "cleanup_verified": True})

    def test_outbox_restart_and_ack_are_identity_bound(self):
        self.admitted()
        record = Coordinator(self.path).pending_outbox()[0]
        with self.assertRaisesRegex(StateError, "ACK_IDENTITY_MISMATCH"):
            self.coordinator.acknowledge(record["id"], "0" * 64)
        self.coordinator.acknowledge(record["id"], record["payload_digest"])
        self.assertEqual(Coordinator(self.path).pending_outbox(), [])

    def test_unknown_state_version_refuses_start(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(StateError, "UNSUPPORTED_COORDINATOR_STATE_VERSION"):
            Coordinator(self.path)

    def test_one_pr_failure_does_not_prevent_other_pr_reconciliation(self):
        other = copy.deepcopy(self.authority.pr)
        other["repo_id"] = other["head_repo_id"] = other["base_repo_id"] = "99"
        self.assertEqual(self.coordinator.reconcile("99", 1, lambda: Authority(other, 1), now=1200), 1)
        with self.assertRaisesRegex(StateError, "SCOPE_MISMATCH"):
            self.coordinator.reconcile("42", 1, lambda: Authority(other, 2), now=1201)
        other["base_sha"] = "0" * 40
        self.assertEqual(self.coordinator.reconcile("99", 1, lambda: Authority(other, 2), now=1202), 2)


if __name__ == "__main__":
    unittest.main()
