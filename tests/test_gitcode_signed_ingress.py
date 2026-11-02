"""Synthetic signed ingress integration, never real GitCode Action evidence."""

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_admission import GitCodeAdmissionBroker
from ci_core.gitcode_artifacts import ArtifactError
from ci_core.gitcode_contract import canonical
from ci_core.gitcode_platform import ObservedGitCodeAuthority, PlatformError
from ci_core.gitcode_state import Coordinator, StateError
from ci_core.gitcode_store import GitCodeRequestStore
from tests import test_gitcode_attestation as attestation_fixtures
from tests.test_gitcode_contract import fixture
from tests.test_gitcode_pipeline import HostingAPI


class SignedIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        attestation_fixtures.AttestationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        attestation_fixtures.AttestationTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.signed = attestation_fixtures.AttestationTests()
        self.signed.key, self.signed.public = self.key, self.public
        self.signed.setUp()
        self.signed.intent["bridge"]["rerun_intent_id"] = "initial"
        self.intent = self.signed.intent
        self.envelope = self.signed.sign()
        self.now = 1050
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "coordinator.sqlite3"
        self.coordinator = Coordinator(self.path)
        self.policy = self.signed.policy.control
        self.snapshot = {
            "identity": copy.deepcopy(self.intent["pr"]),
            "state": "open",
            "authorized": True,
        }
        self.client = Mock(repo_id=self.policy.gitcode_repo_id)
        self.client.authority_snapshot.side_effect = lambda *a, **k: copy.deepcopy(
            self.snapshot
        )
        self.client.action_run.return_value = {
            "workflow_run_id": "a" * 32,
            "workflow_id": "b" * 32,
            "file_path": self.signed.policy.workflow_path,
            "attempt": 1,
            "head_sha": "f" * 40,
            "head_branch": "target-control",
            "event": "PULL_REQUEST",
            "status": "RUNNING",
            "start_time": 1010000,
            "exist_in_default_branch": True,
        }
        case = copy.deepcopy(fixture()[1]["catalog"]["vllm_worker_os_blue"])
        case["test_id"] = "worker_os_blue"
        self.catalog = {"worker_os_blue": case}
        self.provider = self.reopen()
        self.run = {
            "id": 100,
            "event": "workflow_dispatch",
            "run_attempt": 1,
            "head_sha": self.policy.control_sha,
            "head_branch": "master",
            "workflow_id": self.policy.workflow_id,
            "repository": {
                "id": 1210038933,
                "full_name": "cann-framework/torchtitan-npu",
            },
            "actor": {"login": "depeng1994"},
            "created_at": "1970-01-01T00:16:50Z",
        }

    def reopen(self):
        return ObservedGitCodeAuthority(
            self.client,
            self.policy,
            self.catalog,
            self.path,
            clock=lambda: self.now,
            coordinator=self.coordinator,
            attestation_policy=self.signed.policy,
            action_client=self.client,
        )

    def enroll(self, intent=None, envelope=None):
        self.provider.enroll_attested(
            self.intent if intent is None else intent,
            self.envelope if envelope is None else envelope,
            github_run=self.run,
        )

    def _exercise_real_signed_chain(self):
        hosted = Path(self.tmp.name) / "hosted"
        hosted.mkdir()
        api = HostingAPI(hosted)
        api.intent["bridge"]["rerun_intent_id"] = "initial"
        api.run["created_at"] = "1970-01-01T00:16:50Z"
        self.policy = api.policy
        self.catalog = api.reader.catalog
        self.signed.policy = replace(self.signed.policy, control=self.policy)
        self.signed.intent = api.intent
        self.signed.original_event = {
            "id": api.intent["bridge"]["bridge_event_id"],
            "created_at": api.intent["bridge"]["event_created_at"],
            "event_kind": "pull_request",
            "source_snapshot_id": api.intent["bridge"]["source_snapshot_id"],
            "source_snapshot": copy.deepcopy(api.intent["pr"]),
        }
        self.signed.arguments.update(
            original_event=self.signed.original_event,
            run_id=api.intent["bridge"]["bridge_action_run"],
        )
        self.client.action_run.return_value["workflow_run_id"] = api.intent["bridge"][
            "bridge_action_run"
        ]
        self.snapshot["identity"] = copy.deepcopy(api.intent["pr"])
        self.now = 1100
        self.provider = self.reopen()
        envelope = self.signed.sign()
        api.reader.read_snapshot = self.provider.read_snapshot
        # Real Producer creates immutable inputs/slots from signed intent only;
        # it neither instantiates a fresh authority ledger nor needs generation.
        plan = api.produce()
        api.add_artifact(
            987, "dispatch-attestation-100-1", "attestation.json", canonical(envelope)
        )

        def enroll(intent, run):
            transported = api.reader.early_attestation(run["id"])
            if transported is None:
                return False
            self.provider.enroll_attested(intent, transported, github_run=run)
            return True

        broker = GitCodeAdmissionBroker(
            reader=api.reader,
            coordinator=self.coordinator,
            store=GitCodeRequestStore(Path(self.tmp.name) / "requests"),
            workspace=Path(self.tmp.name) / "admit",
            read_authority=self.provider.read_authority,
            verify_event=self.provider.verify_event,
            verify_plan=self.provider.verify_plan,
            enroll_intent=enroll,
            clock=lambda: self.now,
        )
        api.job.update(status="in_progress", conclusion=None)
        self.assertIsNone(broker.admit(100))
        self.assertEqual(self.coordinator.execution_snapshots(), [])
        # Authenticated ingress is durable but no execution before Prepare success.
        self.provider.verify_event(api.intent)
        api.job.update(status="completed", conclusion="success")
        accepted = broker.admit(100)
        self.assertIsNotNone(accepted)
        self.assertFalse(accepted.duplicate)
        self.assertEqual(accepted.verified.plan["pr"], plan["pr"])
        broker.startable_source(accepted).verify_files()
        with self.coordinator._connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM source_attestations").fetchone()[0], 1
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

        original_run = copy.deepcopy(self.client.action_run.return_value)
        duplicate_run = {**original_run, "workflow_run_id": "d" * 32}
        self.client.action_run.side_effect = lambda run_id: (
            original_run if run_id == original_run["workflow_run_id"] else duplicate_run
        )
        self.signed.intent = copy.deepcopy(api.intent)
        self.signed.intent["bridge"].update(
            dispatch_intent_id="second-authentic-capture",
            bridge_event_id="second-event",
            source_snapshot_id="second-snapshot",
            bridge_action_run="d" * 32,
            bridge_nonce="second_nonce_1234567890",
        )
        self.signed.original_event = {
            **self.signed.original_event,
            "id": "second-event",
            "source_snapshot_id": "second-snapshot",
        }
        self.signed.arguments.update(
            original_event=self.signed.original_event, run_id="d" * 32
        )
        self.provider.enroll_attested(
            self.signed.intent, self.signed.sign(), github_run={**api.run, "id": 200}
        )
        # Same effective spec from another valid capture does not supersede or
        # cancel the existing canonical execution before duplicate registration.
        self.coordinator.assert_startable(accepted.execution_id, now=self.now)
        self.provider.verify_plan(accepted.verified.plan)
        duplicate_run["status"] = "CANCELED"
        with self.assertRaisesRegex(PlatformError, "REVOKED_BY_ACTION"):
            self.provider.verify_event(self.signed.intent)
        self.coordinator.assert_startable(accepted.execution_id, now=self.now)
        self.provider.verify_plan(accepted.verified.plan)
        return api, accepted, broker

    def test_real_producer_signed_sidecar_broker_and_atomic_coordinator_chain(self):
        self._exercise_real_signed_chain()

    def test_reader_rejection_persists_exact_observed_b_before_api_returns_a(self):
        api, accepted, broker = self._exercise_real_signed_chain()
        original = copy.deepcopy(self.snapshot)
        self.snapshot["identity"]["base_sha"] = "e" * 40
        self.now += 1
        # The reader rejects before broker.refresh. Its one authenticated B read
        # must already have revoked A atomically, not be lost on exception.
        before = self.client.authority_snapshot.call_count
        with self.assertRaisesRegex(ArtifactError, "EARLY_INTENT_VERSION_STALE"):
            broker.admit(100)
        self.assertEqual(self.client.authority_snapshot.call_count, before + 1)
        with self.coordinator._connection() as db:
            row = db.execute(
                "SELECT revision, snapshot FROM source_observations"
            ).fetchone()
            self.assertEqual(row["revision"], 2)
            self.assertIn("e" * 40, row["snapshot"])
        self.snapshot = original
        self.now += 1
        broker.refresh(self.policy.gitcode_repo_id, api.intent["pr"]["number"])
        with self.assertRaises(StateError):
            self.coordinator.assert_startable(accepted.execution_id, now=self.now)
        with self.assertRaises(PlatformError):
            self.provider.verify_plan(accepted.verified.plan)
        with self.coordinator._connection() as db:
            self.assertEqual(
                db.execute("SELECT revision FROM source_observations").fetchone()[0], 3
            )

    def test_valid_original_enrollment_and_restart_keep_same_proof(self):
        self.enroll()
        proof = self.provider.verify_event(self.intent)
        self.provider = self.reopen()
        self.enroll()
        self.assertEqual(self.provider.verify_event(self.intent), proof)

    def test_same_capture_source_with_new_event_or_payload_is_equivocation(self):
        self.enroll()
        original = copy.deepcopy(self.intent)
        self.intent["bridge"]["dispatch_intent_id"] = "new-intent"
        self.intent["bridge"]["bridge_event_id"] = "new-event"
        self.signed.original_event["id"] = "new-event"
        changed = self.signed.sign()
        with self.assertRaisesRegex(PlatformError, "EQUIVOCATION"):
            self.enroll(self.intent, changed)
        self.provider.verify_event(original)

    def test_old_original_event_cannot_be_rejuvenated_after_observed_aba(self):
        pr = self.snapshot["identity"]
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1060
        self.snapshot["identity"]["base_sha"] = "e" * 40
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1070
        self.snapshot["identity"]["base_sha"] = "b" * 40
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1080
        fresh_signature = self.signed.sign(issued_at=1080)
        with self.assertRaisesRegex(PlatformError, "OBSERVED_INVALIDATION"):
            self.enroll(envelope=fresh_signature)

    def test_changed_api_action_identity_never_enrolls(self):
        self.client.action_run.return_value["head_sha"] = "0" * 40
        with self.assertRaisesRegex(PlatformError, "API_IDENTITY"):
            self.enroll()

    def test_cancelled_action_and_key_retirement_block_existing_intent(self):
        self.enroll()
        self.provider.attestation_policy = replace(self.signed.policy, key_id="retired")
        with self.assertRaisesRegex(PlatformError, "KEY_RETIRED"):
            self.provider.verify_event(self.intent)
        self.provider.attestation_policy = self.signed.policy
        self.client.action_run.return_value["status"] = "CANCELED"
        with self.assertRaisesRegex(PlatformError, "REVOKED_BY_ACTION"):
            self.provider.verify_event(self.intent)
        self.client.action_run.return_value["status"] = "RUNNING"
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(self.intent)

    def test_older_or_equal_time_different_spec_cannot_supersede_newer(self):
        original = copy.deepcopy(self.intent)
        original_event = copy.deepcopy(self.signed.original_event)
        extra = copy.deepcopy(fixture()[1]["catalog"]["dispatch_smoke_a3_16p"])
        extra["test_id"] = "dispatch_smoke_16p"
        self.catalog["dispatch_smoke_16p"] = extra
        self.intent["selectors"].append({"test_id": "dispatch_smoke_16p"})
        self.intent["bridge"]["event_created_at"] = 1020
        self.signed.original_event["created_at"] = 1020
        self.envelope = self.signed.sign(issued_at=1030)
        self.enroll()
        newer = copy.deepcopy(self.intent)
        for timestamp in (1000, 1020):
            with self.subTest(timestamp=timestamp):
                self.signed.intent = copy.deepcopy(original)
                self.signed.intent["bridge"].update(
                    dispatch_intent_id="late-" + str(timestamp),
                    bridge_event_id="late-event-" + str(timestamp),
                    bridge_action_run="c" * 32,
                    event_created_at=timestamp,
                )
                self.signed.original_event = {
                    **original_event,
                    "id": "late-event-" + str(timestamp),
                    "created_at": timestamp,
                }
                self.signed.arguments.update(
                    original_event=self.signed.original_event, run_id="c" * 32
                )
                self.client.action_run.return_value["workflow_run_id"] = "c" * 32
                old_envelope = self.signed.sign(issued_at=1030)
                with self.assertRaisesRegex(PlatformError, "NOT_NEWER"):
                    self.enroll(self.signed.intent, old_envelope)
        self.client.action_run.return_value["workflow_run_id"] = "a" * 32
        self.provider.verify_event(newer)

    def test_terminal_capture_before_first_enrollment_never_revives(self):
        self.client.action_run.return_value["status"] = "CANCELED"
        with self.assertRaisesRegex(PlatformError, "TERMINALLY_REVOKED"):
            self.enroll()
        self.client.action_run.return_value["status"] = "RUNNING"
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "ALREADY_REVOKED"):
            self.enroll()
        self.assertEqual(self.coordinator.execution_snapshots(), [])

    def test_replayed_valid_envelope_does_not_need_new_signature_after_age_budget(self):
        self.enroll()
        self.now = 1400
        self.enroll()
        self.provider.verify_event(self.intent)


if __name__ == "__main__":
    unittest.main()
