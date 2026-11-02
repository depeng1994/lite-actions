"""Cross-stage CPU integration, explicitly not live GitCode/GitHub/NPU evidence."""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_artifacts import ArtifactError
from ci_core.gitcode_checks import ChannelError, CreationJournal
from ci_core.gitcode_contract import VerifiedRequest, canonical, request_id, source_identity
from ci_core.gitcode_delivery import DeliveryError, GitCodeDelivery
from ci_core.gitcode_producer import GitCodeProducer, ProducerError
from ci_core.gitcode_source import SourceHandle
from ci_core.gitcode_state import Authority, Coordinator, EventProof
from ci_core.gitcode_waiter import GitCodeWaiter, WaiterError
from tests.test_gitcode_artifacts import ArtifactAPI, tar_bytes


class HostingAPI(ArtifactAPI):
    def __init__(self, root):
        self.root = root
        self.source_bytes = tar_bytes()
        self.runner_bytes = tar_bytes({"entry.py": b"# trusted runner\n"})
        super().__init__(source=self.source_bytes, runner=self.runner_bytes)
        self.metadata.clear()
        self.zips.clear()
        self.checks.clear()
        self.job.update(status="in_progress", conclusion=None)
        self.uploads = []
        self.lost_after = self.lost_before = None
        self.after_upload = lambda name: None
        self.runner_path = root / "runner.tar.gz"
        self.runner_path.write_bytes(self.runner_bytes)
        self.channel.journal = CreationJournal(root / "checks.sqlite3")
        self.upload_journal = CreationJournal(root / "artifacts.sqlite3")
        self.upstream = Mock(repo_id=self.policy.upstream_repo_id)
        self.upstream.control_tip.return_value = {
            "repo_id": self.policy.upstream_repo_id,
            "default_ref": self.policy.upstream_default_ref,
            "sha": self.policy.control_sha,
        }
        self.producer = GitCodeProducer(
            policy=self.policy,
            catalog=self.reader.catalog,
            github=self,
            personal=self.personal,
            upstream=self.upstream,
            channel=self.channel,
            artifact_journal=self.upload_journal,
            upload=self.upload,
            clock=lambda: 1100,
            source_builder=self.build_source,
        )

    def build_source(self, client, number, destination, **kwargs):
        self.assert_source_identity = kwargs["expected_identity"]
        destination.mkdir()
        bundle = destination / "source.tar.gz"
        bundle.write_bytes(self.source_bytes)
        return SourceHandle(
            provider="gitcode",
            head_sha="a" * 40,
            base_sha="b" * 40,
            tested_merge_sha="e" * 40,
            tested_tree_sha="f" * 40,
            bundle_sha256=hashlib.sha256(self.source_bytes).hexdigest(),
            bundle_path=str(bundle),
            merge_policy="git-ort-v1",
            git_version="2.43.0",
            dependencies=(),
        ), copy.deepcopy(self.intent["pr"])

    def upload(self, name, path):
        self.uploads.append(name)
        if self.lost_before == name:
            raise TimeoutError("unknown before upload")
        ident = 2000 + len(self.metadata)
        self.add_artifact(ident, name, path.name, path.read_bytes())
        self.after_upload(name)
        if self.lost_after == name:
            raise TimeoutError("response lost after upload")
        return ident

    def request(self, method, path, data=None):
        if method == "GET" and path == "":
            return {"id": self.repo_id, "full_name": self.repo, "default_branch": "main"}
        if method == "GET" and path == "/branches/main":
            return {"name": "main", "commit": {"sha": self.policy.control_sha}}
        if method == "GET" and path == "/actions/workflows/91":
            return {"id": 91, "path": self.policy.workflow_path, "state": "active"}
        if method == "GET" and path.startswith("/actions/runs/100/attempts/1/jobs"):
            return {"total_count": 1, "jobs": [copy.deepcopy(self.job)]}
        if method == "POST" and path == "/check-runs":
            ident = 3000 + len(self.checks)
            check = {
                **copy.deepcopy(data),
                "id": ident,
                "app": {"id": self.policy.app_id},
                "url": self.api + f"/check-runs/{ident}",
                "conclusion": None,
            }
            self.checks[ident] = check
            return copy.deepcopy(check)
        return super().request(method, path, data)

    def produce(self, directory="prepare"):
        self.plan = self.producer.prepare(
            run_id=100,
            intent=self.intent,
            runner_path=self.runner_path,
            workspace=self.root / directory,
        )
        return self.plan


class GitCodePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = HostingAPI(self.root)

    def admitted(self):
        plan = self.api.produce()
        self.api.job.update(status="completed", conclusion="success")
        loaded = self.api.reader.load(100, self.root / "admitted", now=1100)
        self.coordinator = Coordinator(self.root / "coordinator.sqlite3")
        self.authority = Authority(copy.deepcopy(plan["pr"]), 1)
        self.coordinator.reconcile("42", 1, lambda: self.authority, now=1100)
        self.coordinator.register_intent(
            loaded.intent,
            {
                "request_id": request_id(plan),
                "repo_id": plan["github"]["repo_id"],
                "run_id": 100,
                "run_attempt": 1,
                "control_sha": self.api.policy.control_sha,
            },
            EventProof("event-1", "snapshot-1", 1, 1),
        )
        self.verified = loaded.verified
        self.receipt = self.coordinator.claim(loaded.verified, now=1100).receipt
        self.results = []
        self.delivery = GitCodeDelivery(
            self.coordinator,
            resolve_request=lambda rid: self.verified,
            channel_for=lambda plan: self.api.channel,
            cancel_run=Mock(return_value=True),
            cancel_execution=Mock(return_value=True),
            cleanup_ack=Mock(return_value=True),
        )
        return plan

    def record_results(self, *, status="PASS"):
        eid = self.receipt["canonical_execution_id"]
        for item in self.api.plan["cases"]:
            case_id = item["case_id"]
            self.coordinator.claim_case(eid, case_id, now=1100)
            if item["kind"] != "worker_probe":
                token = self.coordinator.bind_lease(
                    eid,
                    case_id,
                    "lease-" + case_id,
                    {"nodes": [{"host_id": "test-host", "container_id": "a" * 64,
                                "image_id": "sha256:" + "b" * 64,
                                "task_marker": eid + case_id, "device_ids": list(range(8))}]},
                    now=1100,
                )
                self.coordinator.complete_lease(eid, case_id, "lease-" + case_id, token, cleanup_verified=True)
            result = {
                "case_id": case_id,
                "receipt": self.receipt,
                "identity": source_identity(self.api.plan),
                "status": status,
                "cleanup_verified": True,
                "summary": ["CPU integration fixture; no hardware executed"],
            }
            self.coordinator.record_case_result(eid, case_id, result)
            self.results.append(result)

    def waiter(self, *, current=True):
        return GitCodeWaiter(self.verified, self.api.channel, current=lambda plan: current)

    def test_request_is_last_and_successful_prepare_is_independent(self):
        plan = self.api.produce()
        self.assertEqual(self.api.uploads, ["dispatch-intent-100-1", "gitcode-merge-source-100-1", "trusted-runner-100-1", "source-manifest-100-1", "lite-ci-request-1"])
        self.assertNotIn("plan_digest", self.api.manifest)
        self.assertEqual(plan["merge"]["tested_merge_sha"], "e" * 40)
        self.assertNotEqual(plan["pr"]["head_sha"], plan["merge"]["tested_merge_sha"])
        self.assertIsNone(self.api.reader.load(100, self.root / "early", now=1100))
        self.api.job.update(status="completed", conclusion="failure")
        with self.assertRaisesRegex(ArtifactError, "NOT_SUCCESSFUL"):
            self.api.reader.load(100, self.root / "failed", now=1100)
        self.assertFalse((self.root / "failed").exists())

    def test_complete_producer_to_ledger_delivery_waiter(self):
        self.admitted()
        waiter = self.waiter()
        self.assertEqual(waiter.poll(now=1100)["test_result"], "PENDING")
        self.delivery.flush()
        self.record_results()
        self.delivery.flush()
        self.assertEqual(waiter.poll(now=1100)["test_result"], "PENDING")
        self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: True)
        outcome = waiter.poll(now=1100)
        self.assertEqual(outcome["test_result"], "PASS")
        self.assertEqual(outcome["merge_gate"], "NOT_PROVIDED")
        self.assertFalse(any("comments" in path for method, path in self.api.calls))
        self.assertEqual(self.coordinator.pending_outbox(), [])

    def test_upload_response_loss_recovers_exact_id_once(self):
        self.api.lost_after = "gitcode-merge-source-100-1"
        self.api.produce()
        self.assertEqual(self.api.uploads.count("gitcode-merge-source-100-1"), 1)

    def test_unknown_upload_is_not_blindly_retried(self):
        self.api.lost_before = "dispatch-intent-100-1"
        for directory in ("first", "second"):
            with self.assertRaisesRegex(ProducerError, "PUBLICATION_UNKNOWN"):
                self.api.produce(directory)
        self.assertEqual(self.api.uploads, ["dispatch-intent-100-1"])
        self.assertEqual(self.api.metadata, {})

    def test_changed_pr_during_upload_publishes_no_request(self):
        def changed(name):
            if name == "source-manifest-100-1":
                self.api.personal.open_pull_snapshot.return_value["identity"]["base_sha"] = "0" * 40
        self.api.after_upload = changed
        with self.assertRaisesRegex(ValueError, "STALE"):
            self.api.produce()
        self.assertNotIn("lite-ci-request-1", self.api.uploads)

    def test_wrong_runner_rejected_before_any_upload(self):
        self.api.runner_path.write_bytes(b"not the pinned runner")
        with self.assertRaisesRegex(ProducerError, "UNTRUSTED_RUNNER"):
            self.api.produce()
        self.assertEqual(self.api.uploads, [])

    def test_native_rerun_refused_before_any_upload(self):
        self.api.run["run_attempt"] = 2
        with self.assertRaisesRegex(ArtifactError, "ATTEMPT_MISMATCH"):
            self.api.produce()
        self.assertEqual(self.api.uploads, [])

    def test_base_update_invalidates_previously_published_pass(self):
        self.admitted()
        self.record_results()
        self.delivery.flush()
        self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: True)
        self.assertEqual(self.waiter(current=False).poll(now=1100)["test_result"], "STALE")
        self.assertEqual(self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: False)["test_result"], "STALE")

    def test_receipt_cannot_be_swapped_after_first_read(self):
        self.admitted()
        self.delivery.flush()
        waiter = self.waiter()
        waiter.poll(now=1100)
        slot = self.api.checks[self.api.plan["result_slots"]["aggregate"]["id"]]
        envelope = self.api.channel._payload(slot)
        envelope["receipt"]["canonical_execution_id"] = "replacement"
        slot["output"]["summary"] = canonical(envelope).decode()
        with self.assertRaisesRegex(WaiterError, "RECEIPT_CHANGED"):
            waiter.poll(now=1100)

    def test_dropped_case_and_false_aggregate_cannot_green(self):
        self.admitted()
        self.record_results()
        self.delivery.flush()
        self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: True)
        case_id = self.api.plan["cases"][-1]["case_id"]
        self.api.checks[self.api.plan["result_slots"][case_id]["id"]]["status"] = "in_progress"
        with self.assertRaisesRegex(WaiterError, "INCOMPLETE_CASE_SET"):
            self.waiter().poll(now=1100)

    def test_failure_is_not_confused_with_missing_atomic_merge_gate(self):
        self.admitted()
        self.record_results(status="FAIL")
        self.delivery.flush()
        self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: True)
        result = self.waiter().poll(now=1100)
        self.assertEqual(result["test_result"], "FAIL")
        self.assertEqual(result["merge_gate"], "NOT_PROVIDED")

    def test_network_error_cannot_return_cached_green(self):
        self.admitted()
        self.record_results()
        self.delivery.flush()
        self.delivery.publish_complete(self.verified, self.receipt, self.results, current=lambda: True)
        waiter = self.waiter()
        waiter.poll(now=1100)
        waiter.current = Mock(side_effect=OSError("authority offline"))
        with self.assertRaises(OSError):
            waiter.poll(now=1100)

    def test_lost_outbox_ack_retries_original_slot_without_execution(self):
        self.admitted()
        self.record_results()
        acknowledge = self.coordinator.acknowledge
        self.coordinator.acknowledge = Mock(side_effect=OSError("crash after PATCH"))
        self.assertTrue(self.delivery.flush()["pending"])
        self.coordinator.acknowledge = acknowledge
        self.coordinator = Coordinator(self.coordinator.path)
        self.delivery.coordinator = self.coordinator
        count = len(self.api.checks)
        self.assertEqual(self.delivery.flush()["pending"], [])
        self.assertEqual(len(self.api.checks), count)
        self.assertFalse(self.coordinator.claim_case(self.receipt["canonical_execution_id"], self.api.plan["cases"][0]["case_id"], now=1100))

    def test_cancellation_drains_even_when_all_check_writes_fail(self):
        self.admitted()
        self.coordinator.cancel_execution(self.receipt["canonical_execution_id"])
        self.delivery.channel_for = Mock(side_effect=OSError("Check API offline"))
        result = self.delivery.flush()
        self.delivery.cancel_execution.assert_called_once()
        self.assertTrue(result["pending"])

    def test_callback_without_durable_ack_stays_pending(self):
        self.admitted()
        self.coordinator.cancel_execution(self.receipt["canonical_execution_id"])
        self.delivery.cancel_execution = Mock(return_value=None)
        result = self.delivery.flush()
        self.assertIn("CANCEL_EXECUTION", {row["kind"] for row in result["pending"]})

    def test_missing_result_and_deleted_slot_are_not_recreated(self):
        self.admitted()
        self.record_results()
        with self.assertRaisesRegex(DeliveryError, "COMPLETE_PERSISTED"):
            self.delivery.publish_complete(self.verified, self.receipt, self.results[:-1], current=lambda: True)
        slot = self.api.plan["result_slots"]["aggregate"]["id"]
        del self.api.checks[slot]
        count = len(self.api.checks)
        self.assertTrue(self.delivery.flush()["pending"])
        self.assertEqual(len(self.api.checks), count)

    def test_waiter_deadline_prevents_future_green(self):
        self.admitted()
        with self.assertRaisesRegex(WaiterError, "DEADLINE"):
            self.waiter().poll(now=self.api.plan["deadline"])


if __name__ == "__main__":
    unittest.main()
