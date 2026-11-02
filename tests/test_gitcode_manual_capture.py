"""Manual API wake integration. Synthetic clients, not a live smoke claim."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from ci_core.gitcode_admission import GitCodeAdmissionBroker
from ci_core.gitcode_assembly import AssembledRuntime, AssemblyError, assemble_blue
from ci_core.gitcode_contract import digest, request_id
from ci_core.gitcode_manual_capture import (
    PLATFORM_STEP_KEYS,
    ManualCaptureError,
    ManualCapturePolicy,
    capture_user_step,
    encode_capture,
    verify_manual_capture,
)
from ci_core.gitcode_platform import ObservedGitCodeAuthority, PlatformError
from ci_core.gitcode_state import Coordinator, StateError
from ci_core.gitcode_store import GitCodeRequestStore
from tests.test_gitcode_pipeline import HostingAPI


class ManualCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = HostingAPI(self.root)
        self.api.intent["bridge"]["rerun_intent_id"] = "initial"
        self.api.intent["selectors"] = [
            {"test_id": "worker_os_blue"},
            {"test_id": "dispatch_smoke_8p"},
            {"test_id": "dispatch_smoke_16p"},
        ]
        self.api.run["created_at"] = "1970-01-01T00:16:50Z"
        self.now = 1100
        self.capture_policy = ManualCapturePolicy(
            control=self.api.policy,
            workflow_id="capture-workflow",
            workflow_path=".gitcode/workflows/manual_capture.yml",
            job_identifier="manual_capture",
            step_identifier="1",
            step_name="Capture manual request",
            script_sha256="a" * 64,
            shell="bash",
            working_directory="",
            enabled=True,
        )
        self.intent = self.api.intent
        self.client = Mock(repo_id=self.api.policy.gitcode_repo_id)
        self.snapshot = {
            "identity": copy.deepcopy(self.intent["pr"]),
            "state": "open",
            "authorized": True,
        }
        self.client.authority_snapshot.side_effect = lambda *a, **k: copy.deepcopy(
            self.snapshot
        )
        self.run = {
            "workflow_run_id": self.intent["bridge"]["bridge_action_run"],
            "workflow_id": "capture-workflow",
            "file_path": self.capture_policy.workflow_path,
            "head_sha": self.api.policy.control_sha,
            "head_branch": "master",
            "event": "Manual",
            "attempt": 1,
            "exist_in_default_branch": True,
            "status": "RUNNING",
            "start_time": 980000,
        }
        self.job = {
            "id": "job-a",
            "identifier": "manual_capture",
            "name": "manual capture",
            "status": "COMPLETED",
            "is_shadow": False,
            "start_time": 990000,
            "end_time": 1010000,
            "exec_id": "execution-a",
            "last_dispatch_id": "dispatch-a",
            "steps": [
                {
                    "id": "step-a",
                    "job_run_id": "job-a",
                    "identifier": "1",
                    "name": "Capture manual request",
                    "status": "COMPLETED",
                    "task": "official_shell",
                    "script_sha256": "a" * 64,
                    "shell": "bash",
                    "working_directory": "",
                    "start_time": 990000,
                    "end_time": 1010000,
                    "last_dispatch_id": "step-dispatch-a",
                    "continue-on-error": False,
                }
            ],
        }
        self.logs = {
            "complete": True,
            "start_offset": 0,
            "end_offset": 123,
            "log": encode_capture(
                self.intent, self.intent["bridge"]["event_created_at"]
            )
            + "\n",
        }
        self.client.action_run.side_effect = lambda *a: copy.deepcopy(self.run)
        self.client.action_jobs.side_effect = lambda *a: [copy.deepcopy(self.job)]
        self.client.action_job.side_effect = lambda *a: copy.deepcopy(self.job)
        self.client.action_capture_snapshot.side_effect = lambda *a: {
            "run": copy.deepcopy(self.run),
            "job": copy.deepcopy(self.job),
        }
        self.client.action_step_logs.side_effect = lambda *a, **k: copy.deepcopy(
            self.logs
        )
        self.db = Coordinator(self.root / "coordinator.sqlite3")
        self.provider = self.reopen()

    def reopen(self):
        return ObservedGitCodeAuthority(
            self.client,
            self.api.policy,
            self.api.reader.catalog,
            self.db.path,
            clock=lambda: self.now,
            coordinator=self.db,
            manual_capture_policy=self.capture_policy,
            action_client=self.client,
        )

    def enroll(self, intent=None, run=None):
        return self.provider.enroll_manual(
            self.intent if intent is None else intent,
            github_run=self.api.run if run is None else run,
        )

    def test_real_producer_broker_manual_registration_waits_for_prepare_completion(
        self,
    ):
        self.api.reader.read_snapshot = self.provider.read_snapshot
        self.api.produce()
        broker = GitCodeAdmissionBroker(
            reader=self.api.reader,
            coordinator=self.db,
            store=GitCodeRequestStore(self.root / "requests"),
            workspace=self.root / "admit",
            read_authority=self.provider.read_authority,
            verify_event=self.provider.verify_event,
            verify_plan=self.provider.verify_plan,
            clock=lambda: self.now,
            enroll_intent=lambda intent, run: (
                self.provider.enroll_manual(intent, github_run=run) is None
            ),
        )
        self.assertIsNone(broker.admit(100))
        self.assertEqual(self.db.execution_snapshots(), [])
        self.api.job.update(status="completed", conclusion="success")
        accepted = broker.admit(100)
        self.assertIsNotNone(accepted)
        broker.startable_source(accepted).verify_files()
        with self.db._connection() as db:
            evidence = json.loads(
                db.execute("SELECT payload FROM source_attestations").fetchone()[0]
            )
            self.assertEqual(evidence["metadata"]["capture_kind"], "manual_api")
            self.assertEqual(evidence["metadata"]["github_delivery"]["run_id"], 100)
            self.assertEqual(
                evidence["metadata"]["github_delivery"]["intent_digest"],
                digest(self.intent),
            )
            self.assertNotIn("workflow_source_sha", evidence["metadata"])
            self.assertNotIn("original_event_at", evidence["metadata"])
            self.assertEqual(db.execute("SELECT count(*) FROM leases").fetchone()[0], 0)

    def test_real_b472_sanitized_fixture_exact_platform_envelope(self):
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures" / "gitcode_manual_b472_steps.json"
            ).read_text()
        )
        steps = []
        for observed in fixture["steps"]:
            step = {
                k: v
                for k, v in observed.items()
                if k not in {"keys", "inputs", "inputs_type", "env_type"}
            }
            step["metadata_keys"] = observed["keys"]
            step["inputs_state"] = (
                "null" if observed["inputs_type"] == "NoneType" else "present"
            )
            step["env_state"] = (
                "null" if observed["env_type"] == "NoneType" else "present"
            )
            step["condition_state"] = (
                "null" if observed["condition"] is None else "present"
            )
            steps.append(step)
        selected = capture_user_step(
            {"id": fixture["job_id"], "steps": steps}, self.capture_policy
        )
        self.assertEqual(selected["id"], "c4ec4fb5f98247e9add0dbfca47a35ae")
        self.assertEqual(selected["task"], "official_shell")
        self.assertEqual(set(fixture["steps"][0]["keys"]), PLATFORM_STEP_KEYS)
        self.assertIsNone(fixture["steps"][0]["inputs"])

    def add_platform_steps(self):
        self.job["steps"][0]["sequence"] = 1

        def platform(index, task, name):
            return {
                "id": "platform-" + str(index),
                "job_run_id": self.job["id"],
                "sequence": index,
                "identifier": None,
                "task": task,
                "name": name,
                "status": "COMPLETED",
                "continue-on-error": None,
                "metadata_keys": sorted(PLATFORM_STEP_KEYS),
                "inputs_state": "null",
                "env_state": "null",
                "condition_state": "null",
                "start_time": 990000 if index == 0 else 1010000,
                "end_time": 990000 if index == 0 else 1010000,
            }

        self.job["steps"] = [
            platform(0, "official_pre_step", "初始化步骤"),
            self.job["steps"][0],
            platform(2, "official_post_step", "后处理步骤"),
        ]

    def test_exact_platform_wrapper_enrolls_and_retains_whole_envelope(self):
        self.add_platform_steps()
        self.enroll()
        self.provider.verify_event(self.intent)
        self.job["steps"][0]["end_time"] += 1
        with self.assertRaisesRegex(PlatformError, "REVOKED_BY_ACTION"):
            self.provider.verify_event(self.intent)

    def test_platform_wrapper_unknown_missing_or_nonnull_metadata_rejected(self):
        self.add_platform_steps()
        original = copy.deepcopy(self.job)
        changes = [
            ("task", "official_shell"),
            ("name", "init"),
            ("identifier", "0"),
            ("sequence", 1),
            ("status", "FAILED"),
            ("job_run_id", "other-job"),
            ("inputs_state", "missing"),
            ("inputs_state", "present"),
            ("env_state", "missing"),
            ("env_state", "present"),
            ("condition_state", "missing"),
            ("condition_state", "present"),
            ("script_sha256", "a" * 64),
            ("metadata_keys", sorted(PLATFORM_STEP_KEYS | {"command"})),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.job = copy.deepcopy(original)
                self.job["steps"][0][key] = value
                with self.assertRaises(ManualCaptureError):
                    self.enroll()
        self.job = copy.deepcopy(original)
        self.job["steps"].append(copy.deepcopy(self.job["steps"][1]))
        with self.assertRaises(ManualCaptureError):
            self.enroll()
        self.job = copy.deepcopy(original)
        self.job["steps"][1]["sequence"] = 2
        with self.assertRaises(ManualCaptureError):
            self.enroll()

    def runtime_with_admitted_plan(self):
        self.api.reader.read_snapshot = self.provider.read_snapshot
        self.api.produce()
        broker = GitCodeAdmissionBroker(
            reader=self.api.reader,
            coordinator=self.db,
            store=GitCodeRequestStore(self.root / "requests"),
            workspace=self.root / "admit",
            read_authority=self.provider.read_authority,
            verify_event=self.provider.verify_event,
            verify_plan=self.provider.verify_plan,
            clock=lambda: self.now,
            enroll_intent=lambda intent, run: (
                self.provider.enroll_manual(intent, github_run=run) is None
            ),
        )
        self.api.job.update(status="completed", conclusion="success")
        accepted = broker.admit(100)
        self.api.reader.channel.publish_receipt(
            accepted.verified.plan, accepted.admission.receipt
        )
        runtime = AssembledRuntime(
            self.api.policy,
            None,
            self.api.reader,
            None,
            self.provider,
            self.root,
            lambda: self.now,
            "blue",
            coordinator=self.db,
        )
        return runtime, accepted.verified.plan

    def factory_blue_with_admitted_plan(self):
        runtime, plan = self.runtime_with_admitted_plan()
        config = {
            "policy": self.api.policy,
            "personal_repo": "depeng1994/torchtitan-npu",
        }
        with (
            patch("ci_core.gitcode_assembly.load_release", return_value=config),
            patch("ci_core.gitcode_assembly._assemble", return_value=runtime),
        ):
            adapter = assemble_blue(
                [self.root / "synthetic-release.json"],
                environment={},
                state_directory=self.root,
                clock=lambda: self.now,
            )
        adapter.clock = lambda: self.now
        verified = adapter.resolve_request(request_id(plan))
        receipt = adapter.coordinator.request_reference(request_id(plan))["receipt"]
        return adapter, verified, receipt

    def test_actual_blue_factory_callback_one_observation_with_synthetic_network_budget(
        self,
    ):
        adapter, verified, receipt = self.factory_blue_with_admitted_plan()

        def source(*a, **kw):
            self.now += 6  # Six sequential PR reads, synthetic one-second costs.
            return copy.deepcopy(self.snapshot)

        def origin(*a):
            self.now += 3  # Three retained run/job reads, not measured latency.
            return {"run": copy.deepcopy(self.run), "job": copy.deepcopy(self.job)}

        self.client.authority_snapshot.side_effect = source
        self.client.action_capture_snapshot.side_effect = origin
        self.client.reset_mock()
        actual = self.api.reader._actual_run

        def github(*a, **kw):
            self.now += 1
            return actual(*a, **kw)

        start = self.now
        with patch.object(self.api.reader, "_actual_run", side_effect=github):
            self.assertTrue(adapter.current_authority(verified, receipt))
        self.client.authority_snapshot.assert_called_once()
        self.client.action_capture_snapshot.assert_called_once()
        self.assertEqual(self.now - start, 10)  # Old actual entrypoint needed 16.

    def test_actual_blue_factory_callback_rechecks_revocation_after_github_io(self):
        adapter, verified, receipt = self.factory_blue_with_admitted_plan()
        actual = self.api.reader._actual_run

        def race(*a, **kw):
            run = actual(*a, **kw)
            self.snapshot["identity"]["head_sha"] = "f" * 40
            self.provider.read_authority(
                verified.plan["pr"]["repo_id"], verified.plan["pr"]["number"]
            )
            return run

        with patch.object(self.api.reader, "_actual_run", side_effect=race):
            self.assertFalse(adapter.current_authority(verified, receipt))

    def test_actual_blue_factory_callback_generic_proof_retains_explicit_refresh(self):
        adapter, verified, receipt = self.factory_blue_with_admitted_plan()
        broker = adapter.broker(verified.plan)
        proof = self.provider.verify_plan(verified.plan)
        broker.verify_plan = lambda plan: proof
        self.client.authority_snapshot.reset_mock()
        self.assertTrue(adapter.current_authority(verified, receipt))
        self.client.authority_snapshot.assert_called_once()

    def test_current_uses_one_fresh_pr_read_and_retained_capture_no_logs(self):
        runtime, plan = self.runtime_with_admitted_plan()
        self.client.reset_mock()
        self.assertTrue(runtime.current(plan))
        self.client.authority_snapshot.assert_called_once()
        self.client.action_capture_snapshot.assert_called_once()
        self.client.action_step_logs.assert_not_called()

    def test_current_observed_change_rejects_and_cannot_revive_after_aba(self):
        runtime, plan = self.runtime_with_admitted_plan()
        old = copy.deepcopy(self.snapshot)
        self.snapshot["identity"]["head_sha"] = "f" * 40
        with self.assertRaises(PlatformError):
            runtime.current(plan)
        self.snapshot = old
        with self.assertRaises(PlatformError):
            runtime.current(plan)

    def test_current_receipt_network_race_rechecks_committed_generation(self):
        runtime, plan = self.runtime_with_admitted_plan()
        read = self.api.reader.channel.read_receipt

        def raced(plan):
            receipt = read(plan)
            self.snapshot["identity"]["head_sha"] = "f" * 40
            self.provider.read_authority(plan["pr"]["repo_id"], plan["pr"]["number"])
            return receipt

        with patch.object(self.api.reader.channel, "read_receipt", side_effect=raced):
            with self.assertRaises(StateError):
                runtime.current(plan)

    def test_current_rejects_other_coordinator_or_generic_provider(self):
        runtime, plan = self.runtime_with_admitted_plan()
        runtime.coordinator = Coordinator(self.root / "other.sqlite3")
        with self.assertRaisesRegex(AssemblyError, "SHARED_COORDINATOR"):
            runtime.current(plan)
        runtime.coordinator = self.db
        runtime.authority = Mock()
        with self.assertRaisesRegex(AssemblyError, "SHARED_COORDINATOR"):
            runtime.current(plan)

    def test_retained_capture_checks_job_changes_without_reading_new_logs(self):
        self.enroll()
        self.logs["log"] = "unread changes are not claimed as detected"
        self.client.action_step_logs.reset_mock()
        self.provider.verify_event(self.intent)
        self.client.action_step_logs.assert_not_called()
        self.job["exec_id"] = "different-execution"
        with self.assertRaisesRegex(PlatformError, "REVOKED_BY_ACTION"):
            self.provider.verify_event(self.intent)
        self.job["exec_id"] = "execution-a"
        with self.assertRaisesRegex(PlatformError, "TRUSTED_INTENT_REVOKED"):
            self.provider.verify_event(self.intent)

    def test_retained_capture_api_failure_and_late_pr_read_fail_closed(self):
        self.enroll()
        self.client.action_capture_snapshot.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.provider.verify_event(self.intent)
        self.client.action_capture_snapshot.side_effect = lambda *a: {
            "run": copy.deepcopy(self.run),
            "job": copy.deepcopy(self.job),
        }

        def late(*a, **kw):
            self.now = self.intent["deadline"]
            return copy.deepcopy(self.snapshot)

        self.client.authority_snapshot.side_effect = late
        with self.assertRaisesRegex(PlatformError, "EXPIRED"):
            self.provider.verify_event(self.intent)

    def test_exact_capture_recovery_no_new_rights_and_changed_gh_owner_rejected(self):
        self.enroll()
        before = self.provider.verify_event(self.intent)
        self.provider = self.reopen()
        self.enroll()
        self.assertEqual(self.provider.verify_event(self.intent), before)
        with self.assertRaisesRegex(PlatformError, "DELIVERY_CHANGED"):
            self.enroll(run={**self.api.run, "id": 200})
        with self.db._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_intents").fetchone()[0], 1
            )

    def test_mr_or_old_probe_missing_capture_never_enrolls(self):
        self.run["event"] = "MR"
        with self.assertRaisesRegex(ManualCaptureError, "RUN_IDENTITY"):
            self.enroll()
        self.run["event"] = "Manual"
        self.logs["log"] = '{"schema":"old-context-probe"}\n'
        with self.assertRaisesRegex(ManualCaptureError, "LOG_MISSING"):
            self.enroll()

    def test_script_or_full_step_set_mismatch_rejected(self):
        self.job["steps"][0]["script_sha256"] = "b" * 64
        with self.assertRaisesRegex(ManualCaptureError, "COMMAND_MISMATCH"):
            self.enroll()
        self.job["steps"][0]["script_sha256"] = "a" * 64
        self.job["steps"].append(copy.deepcopy(self.job["steps"][0]))
        with self.assertRaisesRegex(ManualCaptureError, "JOB_NOT_FIXED"):
            self.enroll()

    def test_log_intent_and_actual_gh_actor_cannot_be_substituted(self):
        altered = copy.deepcopy(self.intent)
        altered["selectors"] = altered["selectors"][:1]
        with self.assertRaisesRegex(ManualCaptureError, "INTENT_MISMATCH"):
            self.enroll(altered)
        with self.assertRaisesRegex(PlatformError, "OPERATOR_OR_NONCE"):
            self.enroll(run={**self.api.run, "actor": {"login": "not-authorized"}})

    def test_duplicate_log_and_changed_execution_identity_rejected(self):
        self.logs["log"] *= 2
        with self.assertRaisesRegex(ManualCaptureError, "LOG_MISSING_OR_DUPLICATE"):
            self.enroll()
        self.logs["log"] = self.logs["log"].splitlines()[0] + "\n"
        changed = copy.deepcopy(self.job)
        changed["exec_id"] = "another-execution"
        self.client.action_job.side_effect = [copy.deepcopy(self.job), changed]
        with self.assertRaisesRegex(ManualCaptureError, "EVIDENCE_CHANGED"):
            self.enroll()

    def test_observed_aba_rejects_unknown_late_manual_request(self):
        pr = self.intent["pr"]
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1050
        self.snapshot["identity"]["base_sha"] = "e" * 40
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1060
        self.snapshot["identity"] = copy.deepcopy(pr)
        self.provider.read_authority(pr["repo_id"], pr["number"])
        self.now = 1100
        with self.assertRaisesRegex(PlatformError, "OBSERVED_INVALIDATION"):
            self.enroll()

    def test_cancel_is_durable_across_restart_and_status_restore(self):
        self.enroll()
        self.run["status"] = "CANCELED"
        with self.assertRaisesRegex(PlatformError, "REVOKED_BY_ACTION"):
            self.provider.verify_event(self.intent)
        self.run["status"] = "RUNNING"
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(self.intent)

    def test_terminal_before_first_registration_tombstones_valid_capture(self):
        self.run["status"] = "FAILED"
        with self.assertRaisesRegex(PlatformError, "TERMINALLY_REVOKED"):
            self.enroll()
        self.run["status"] = "RUNNING"
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "REPLAY_OR_DELIVERY"):
            self.enroll()
        self.assertEqual(self.db.execution_snapshots(), [])

    def test_replayed_capture_cannot_change_selector_with_new_intent_id(self):
        self.enroll()
        changed = copy.deepcopy(self.intent)
        changed["bridge"]["dispatch_intent_id"] = "new-id"
        self.logs["log"] = (
            encode_capture(changed, changed["bridge"]["event_created_at"]) + "\n"
        )
        with self.assertRaisesRegex(PlatformError, "SOURCE_EQUIVOCATION"):
            self.enroll(changed)
        with self.assertRaisesRegex(PlatformError, "TRUSTED_INTENT_REVOKED"):
            self.provider.verify_event(self.intent)

    def test_unavailable_api_never_creates_ledger_intent(self):
        self.client.action_jobs.side_effect = OSError("unavailable")
        with self.assertRaises(OSError):
            self.enroll()
        with self.db._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_intents").fetchone()[0], 0
            )

    def test_older_duplicate_three_case_capture_does_not_supersede_current_owner(self):
        self.enroll()
        older = copy.deepcopy(self.intent)
        older["bridge"].update(
            bridge_event_id="manual:older-run",
            bridge_action_run="older-run",
            bridge_nonce="o" * 32,
            source_snapshot_id="manual-capture:" + "o" * 32,
            dispatch_intent_id="manual-intent:" + "o" * 32,
            event_created_at=985,
        )
        self.run["workflow_run_id"] = "older-run"
        self.run["start_time"] = 970000
        self.job["steps"][0].update(start_time=980000, end_time=990000)
        self.logs["log"] = encode_capture(older, 985) + "\n"
        older_gh_run = {
            **self.api.run,
            "display_title": "lite-gitcode:" + older["bridge"]["bridge_nonce"],
        }
        self.enroll(older, run=older_gh_run)
        with self.db._connection() as db:
            rows = db.execute(
                "SELECT id,revoked FROM source_intents ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["revoked"] == 0 for row in rows))

    def test_run_attempt_change_after_log_read_rejected(self):
        self.client.action_run.side_effect = [
            copy.deepcopy(self.run),
            {**self.run, "attempt": 2},
        ]
        with self.assertRaisesRegex(ManualCaptureError, "RUN_IDENTITY_CHANGED"):
            self.enroll()

    def test_manual_capture_allows_legacy_os_only_or_exact_three_case_mode(self):
        from ci_core.gitcode_manual_capture import verify_manual_capture

        legacy = copy.deepcopy(self.intent)
        legacy["selectors"] = [{"test_id": "worker_os_blue"}]
        self.logs["log"] = encode_capture(legacy, legacy["bridge"]["event_created_at"]) + "\n"
        proof = verify_manual_capture(
            legacy, self.capture_policy, self.client, now=self.now
        )
        self.assertEqual(proof.intent["selectors"], [{"test_id": "worker_os_blue"}])

        for selectors in (
            [{"test_id": "worker_os_blue"}, {"test_id": "dispatch_smoke_8p"}],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dispatch_smoke_8p"},
            ],
            [
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_16p"},
            ],
        ):
            changed = copy.deepcopy(self.intent)
            changed["selectors"] = selectors
            self.logs["log"] = encode_capture(changed, changed["bridge"]["event_created_at"]) + "\n"
            with self.subTest(selectors=selectors), self.assertRaisesRegex(
                ManualCaptureError, "BOUNDED_SMOKE_REQUEST_REQUIRED"
            ):
                verify_manual_capture(
                    changed, self.capture_policy, self.client, now=self.now
                )

    def test_manual_never_enables_training_or_native_attempt(self):
        for mutation in ("training", "attempt"):
            changed = copy.deepcopy(self.intent)
            if mutation == "training":
                changed["selectors"] = [{"test_id": "dsv4_flash_a3_8p_example"}]
            else:
                changed["bridge"]["bridge_attempt"] = 2
            with self.assertRaisesRegex(ManualCaptureError, "BOUNDED_SMOKE"):
                self.enroll(changed)

    def test_slow_evidence_or_final_authority_read_cannot_extend_first_enrollment_age(
        self,
    ):
        def slow_authority(*args, **kwargs):
            self.now = 1400
            return copy.deepcopy(self.snapshot)

        self.client.authority_snapshot.side_effect = slow_authority
        with self.assertRaisesRegex(PlatformError, "EXPIRED_DURING_VERIFICATION"):
            self.enroll()
        with self.db._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_intents").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_observations").fetchone()[0], 1
            )

    def test_final_authority_read_crossing_intent_deadline_rejects_registration(self):
        self.intent["deadline"] = 1110
        self.logs["log"] = (
            encode_capture(self.intent, self.intent["bridge"]["event_created_at"])
            + "\n"
        )

        def slow_authority(*args, **kwargs):
            self.now = 1120
            return copy.deepcopy(self.snapshot)

        self.client.authority_snapshot.side_effect = slow_authority
        with self.assertRaisesRegex(PlatformError, "EXPIRED_DURING_VERIFICATION"):
            self.enroll()
        with self.db._connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM source_intents").fetchone()[0], 0
            )

    def test_state_v6_blocks_old_coordinator_and_direct_authority_reads(self):
        self.enroll()
        with self.db._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        # Model the old binary's supported version gate, including direct
        # provider reads through an already supplied Coordinator object.
        with patch("ci_core.gitcode_state.STATE_VERSION", 5):
            with self.assertRaisesRegex(
                StateError, "UNSUPPORTED_COORDINATOR_STATE_VERSION"
            ):
                Coordinator(self.db.path)
            old_provider = self.reopen()
            with self.assertRaisesRegex(
                StateError, "UNSUPPORTED_COORDINATOR_STATE_VERSION"
            ):
                old_provider.read_authority(
                    self.api.policy.gitcode_repo_id, self.intent["pr"]["number"]
                )

    def test_v5_upgrade_preserves_every_table_and_failure_rolls_back(self):
        self.enroll()
        with self.db._connection() as db:
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            before = {
                name: [tuple(row) for row in db.execute("SELECT * FROM " + name)]
                for name in tables
            }
            db.execute("PRAGMA user_version=5")

        @contextmanager
        def denied_connection(coordinator):
            connection = sqlite3.connect(coordinator.path)
            connection.row_factory = sqlite3.Row
            connection.set_authorizer(
                lambda action, arg1, arg2, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_PRAGMA
                    and arg1 == "user_version"
                    and arg2 == "6"
                    else sqlite3.SQLITE_OK
                )
            )
            try:
                yield connection
            finally:
                connection.close()

        with patch.object(Coordinator, "_connection", denied_connection):
            with self.assertRaises(sqlite3.DatabaseError):
                Coordinator(self.db.path)
        with sqlite3.connect(self.db.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                {
                    name: db.execute("SELECT * FROM " + name).fetchall()
                    for name in tables
                },
                before,
            )
        upgraded = Coordinator(self.db.path)
        with upgraded._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(
                {
                    name: [tuple(row) for row in db.execute("SELECT * FROM " + name)]
                    for name in tables
                },
                before,
            )

    def test_live_timestamp_framing_is_stripped_once_not_arbitrary_prefixes(self):
        raw = self.logs["log"]
        stamp = "[2026-11-02 16:17:34.000+08:00] "
        self.logs["log"] = stamp + raw
        self.enroll()
        self.provider.verify_event(self.intent)
        self.logs["log"] = stamp + raw + stamp + raw
        with self.assertRaisesRegex(ManualCaptureError, "LOG_MISSING_OR_DUPLICATE"):
            verify_manual_capture(
                self.intent, self.capture_policy, self.client, now=self.now
            )
        self.logs["log"] = stamp + stamp + raw
        with self.assertRaisesRegex(ManualCaptureError, "LOG_MISSING_OR_DUPLICATE"):
            verify_manual_capture(
                self.intent, self.capture_policy, self.client, now=self.now
            )
        self.logs["log"] = "untrusted-prefix " + raw
        with self.assertRaisesRegex(ManualCaptureError, "LOG_MISSING_OR_DUPLICATE"):
            verify_manual_capture(
                self.intent, self.capture_policy, self.client, now=self.now
            )

    def test_request_age_uses_platform_time_not_fresh_payload_timestamp(self):
        self.now = 2000
        with self.assertRaisesRegex(ManualCaptureError, "TOO_OLD"):
            verify_manual_capture(
                self.intent, self.capture_policy, self.client, now=self.now
            )
