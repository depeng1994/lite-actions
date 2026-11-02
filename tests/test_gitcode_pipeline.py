"""Cross-stage CPU integration, explicitly not live GitCode/GitHub/NPU evidence."""

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_admission import GitCodeAdmissionBroker
from ci_core.gitcode_artifacts import ArtifactError, GitCodeArtifactReader
from ci_core.gitcode_checks import CheckRunChannel, CreationJournal
from ci_core.gitcode_contract import (
    canonical,
    source_identity,
)
from ci_core.gitcode_delivery import DeliveryError, GitCodeDelivery
from ci_core.gitcode_producer import GitCodeProducer, ProducerError
from ci_core.gitcode_source import SourceHandle
from ci_core.gitcode_state import Authority, Coordinator, EventProof
from ci_core.gitcode_store import GitCodeRequestStore
from ci_core.gitcode_waiter import GitCodeWaiter, WaiterError
from tests.test_gitcode_artifacts import ArtifactAPI, tar_bytes


class HostingAPI(ArtifactAPI):
    def __init__(self, root, *, torchtitan=True):
        self.root = root
        self.source_bytes = (
            tar_bytes(
                {
                    ".github/scripts/lite_actions/gitcode/smoke_payload.py": b"raise RuntimeError('must never execute on Prepare or Worker control')\nPAYLOAD_API_VERSION = 1\ndef all_reduce(tensor): pass\n"
                }
            )
            if torchtitan
            else tar_bytes()
        )
        self.runner_bytes = tar_bytes({"entry.py": b"# trusted runner\n"})
        super().__init__(source=self.source_bytes, runner=self.runner_bytes)
        if torchtitan:
            old_catalog = self.reader.catalog
            self.policy = replace(
                self.policy,
                github_repo_id=1210038933,
                upstream_default_ref="master",
                allowed_base_refs=("master",),
            )
            self.repo, self.repo_id = "cann-framework/torchtitan-npu", 1210038933
            self.api = "https://api.github.com/repos/" + self.repo
            self.intent = copy.deepcopy(self.intent)
            self.intent["pr"]["base_ref"] = "master"
            self.intent["control_repo_id"] = self.repo_id
            mapping = {
                "vllm_worker_os_blue": "worker_os_blue",
                "dispatch_smoke_a3_8p": "dispatch_smoke_8p",
                "dispatch_smoke_a3_16p": "dispatch_smoke_16p",
            }
            catalog = {}
            for old, name in mapping.items():
                row = copy.deepcopy(old_catalog[old])
                row["test_id"] = name
                if row["kind"] != "worker_probe":
                    row["docker"] = "cann_master_torchtitan_master_xyj"
                catalog[name] = row
            self.intent["selectors"] = [{"test_id": name} for name in mapping.values()]
            self.run.update(
                repository_id=self.repo_id,
                repository={"id": self.repo_id, "full_name": self.repo},
                head_branch="master",
                _ci_repo=self.repo,
            )
            self.personal.open_pull_snapshot.return_value = {
                "identity": copy.deepcopy(self.intent["pr"]),
                "state": "open",
                "authorized": True,
            }
            self.channel = CheckRunChannel(
                self, repo_id=self.repo_id, app_id=self.policy.app_id
            )
            self.reader = GitCodeArtifactReader(
                policy=self.policy,
                github=self,
                personal=self.personal,
                channel=self.channel,
                catalog=catalog,
            )
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

    def add_artifact(self, artifact_id, name, member, content):
        value = super().add_artifact(artifact_id, name, member, content)
        value["workflow_run"]["head_branch"] = self.run["head_branch"]
        return value

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
            return {
                "id": self.repo_id,
                "full_name": self.repo,
                "default_branch": self.run["head_branch"],
            }
        if method == "GET" and path == "/branches/" + self.run["head_branch"]:
            return {
                "name": self.run["head_branch"],
                "commit": {"sha": self.policy.control_sha},
            }
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
        self.coordinator = Coordinator(self.root / "coordinator.sqlite3")
        self.authority = Authority(copy.deepcopy(plan["pr"]), 1)
        self.broker = GitCodeAdmissionBroker(
            reader=self.api.reader,
            coordinator=self.coordinator,
            store=GitCodeRequestStore(self.root / "retained"),
            workspace=self.root / "admissions",
            read_authority=lambda repo, number: self.authority,
            verify_event=lambda intent: EventProof("event-1", "snapshot-1", 1, 1),
            clock=lambda: 1100,
        )
        accepted = self.broker.admit(100)
        self.verified = accepted.verified
        self.receipt = accepted.admission.receipt
        self.results = []
        self.delivery = GitCodeDelivery(
            self.coordinator,
            resolve_request=self.broker.resolve_request,
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
                    {
                        "nodes": [
                            {
                                "host_id": "test-host",
                                "container_id": "a" * 64,
                                "image_id": "sha256:" + "b" * 64,
                                "task_marker": eid + case_id,
                                "device_ids": list(range(8)),
                            }
                        ]
                    },
                    now=1100,
                )
                self.coordinator.complete_lease(
                    eid, case_id, "lease-" + case_id, token, cleanup_verified=True
                )
            result = {
                "case_id": case_id,
                "receipt": self.receipt,
                "identity": source_identity(self.api.plan),
                "status": status,
                "exit_code": 0 if status == "PASS" else 1,
                "cleanup_verified": True,
                "summary": ["CPU integration fixture; no hardware executed"],
            }
            self.coordinator.record_case_result(eid, case_id, result)
            self.results.append(result)

    def waiter(self, *, current=True):
        return GitCodeWaiter(
            self.verified, self.api.channel, current=lambda plan: current
        )

    def test_request_is_last_and_successful_prepare_is_independent(self):
        plan = self.api.produce()
        self.assertEqual(
            self.api.uploads,
            [
                "dispatch-intent-100-1",
                "gitcode-merge-source-100-1",
                "trusted-runner-100-1",
                "source-manifest-100-1",
                "lite-ci-request-1",
            ],
        )
        manifest_meta = next(
            x
            for x in self.api.metadata.values()
            if x["name"] == "source-manifest-100-1"
        )
        manifest, _ = self.api.reader._read(manifest_meta, "manifest")
        self.assertNotIn("plan_digest", manifest)
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
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
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
                self.api.personal.open_pull_snapshot.return_value["identity"][
                    "base_sha"
                ] = "0" * 40

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
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
        self.assertEqual(
            self.waiter(current=False).poll(now=1100)["test_result"], "STALE"
        )
        self.assertEqual(
            self.delivery.publish_complete(
                self.verified, self.receipt, self.results, current=lambda: False
            )["test_result"],
            "STALE",
        )

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
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
        case_id = self.api.plan["cases"][-1]["case_id"]
        self.api.checks[self.api.plan["result_slots"][case_id]["id"]]["status"] = (
            "in_progress"
        )
        with self.assertRaisesRegex(WaiterError, "INCOMPLETE_CASE_SET"):
            self.waiter().poll(now=1100)

    def test_failure_is_not_confused_with_missing_atomic_merge_gate(self):
        self.admitted()
        self.record_results(status="FAIL")
        self.delivery.flush()
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
        result = self.waiter().poll(now=1100)
        self.assertEqual(result["test_result"], "FAIL")
        self.assertEqual(result["merge_gate"], "NOT_PROVIDED")

    def test_network_error_cannot_return_cached_green(self):
        self.admitted()
        self.record_results()
        self.delivery.flush()
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
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
        self.assertFalse(
            self.coordinator.claim_case(
                self.receipt["canonical_execution_id"],
                self.api.plan["cases"][0]["case_id"],
                now=1100,
            )
        )

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
            self.delivery.publish_complete(
                self.verified, self.receipt, self.results[:-1], current=lambda: True
            )
        slot = self.api.plan["result_slots"]["aggregate"]["id"]
        del self.api.checks[slot]
        count = len(self.api.checks)
        self.assertTrue(self.delivery.flush()["pending"])
        self.assertEqual(len(self.api.checks), count)

    def test_in_progress_aggregate_can_finish_after_last_case_arrives(self):
        self.admitted()
        self.delivery.flush()
        self.api.channel.publish_aggregate(
            self.api.plan, self.receipt, [], current=True
        )
        self.record_results()
        self.delivery.flush()
        self.assertEqual(self.waiter().poll(now=1100)["test_result"], "PENDING")
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
        self.assertEqual(self.waiter().poll(now=1100)["test_result"], "PASS")

    def test_late_admission_ack_does_not_regress_completed_aggregate(self):
        self.admitted()
        self.record_results()
        acknowledge = self.coordinator.acknowledge
        self.coordinator.acknowledge = Mock(
            side_effect=OSError("simulated acknowledgement loss")
        )
        self.delivery.flush()
        self.coordinator.acknowledge = acknowledge
        self.delivery.publish_complete(
            self.verified, self.receipt, self.results, current=lambda: True
        )
        self.assertEqual(self.delivery.flush()["pending"], [])
        self.assertEqual(self.waiter().poll(now=1100)["test_result"], "PASS")

    def test_waiter_deadline_prevents_future_green(self):
        self.admitted()
        with self.assertRaisesRegex(WaiterError, "DEADLINE"):
            self.waiter().poll(now=self.api.plan["deadline"])


if __name__ == "__main__":
    unittest.main()
