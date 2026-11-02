"""Atomic SQLite source/coordinator regressions; no platform/hardware claims."""

import copy
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from ci_core.gitcode_admission import AcceptedRequest, GitCodeAdmissionBroker
from ci_core.gitcode_contract import canonical, request_id, verify_request
from ci_core.gitcode_platform import ObservedGitCodeAuthority, PlatformError
from ci_core.gitcode_state import (
    STATE_VERSION,
    Authority,
    Coordinator,
    EventProof,
    StateError,
)
from tests.test_gitcode_contract import fixture


class SharedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shared.sqlite3"
        self.coordinator = Coordinator(self.path)
        self.plan, self.ctx = fixture()
        self.policy = self.ctx["policy"]
        self.snapshot = {
            "identity": copy.deepcopy(self.plan["pr"]),
            "state": "open",
            "authorized": True,
        }
        self.client = Mock(repo_id="42")
        self.client.authenticated_actor.return_value = self.policy.allowed_actors[0]
        self.client.authority_snapshot.side_effect = lambda *a, **k: copy.deepcopy(
            self.snapshot
        )
        self.provider = ObservedGitCodeAuthority(
            self.client,
            self.policy,
            self.ctx["catalog"],
            self.path,
            clock=lambda: 1100,
            coordinator=self.coordinator,
        )
        self.provider.refresh_coordinator("42", 1)

    def admit_old(self):
        gh = self.plan["github"]
        self.coordinator.register_intent(
            self.ctx["intent"],
            {
                "request_id": request_id(self.plan),
                "repo_id": gh["repo_id"],
                "run_id": gh["run_id"],
                "run_attempt": 1,
                "control_sha": self.policy.control_sha,
            },
            EventProof("event-1", "snapshot-1", 1, 1),
        )
        admission = self.coordinator.claim(
            verify_request(self.plan, **self.ctx), now=1101
        )
        return admission.receipt["canonical_execution_id"]

    def rows(self, table):
        with self.coordinator._connection() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM " + table)]

    def _insert_fixture_intent(self, intent):
        with self.coordinator._connection() as db:
            db.execute(
                "INSERT INTO source_intents(repo_id,id,semantic,number,revision,intent,revoked,trigger_kind,actor,authorized_rerun) VALUES(?,?,?,?,?,?,0,?,?,0)",
                (
                    "42",
                    intent["bridge"]["dispatch_intent_id"],
                    "fixture-semantic",
                    1,
                    1,
                    canonical(intent).decode(),
                    "periodic",
                    self.policy.allowed_actors[0],
                ),
            )

    def _broker(self, provider, *, verify_event=None, verify_plan=None, reader_policy=None):
        return GitCodeAdmissionBroker(
            reader=Mock(policy=reader_policy or self.policy),
            coordinator=self.coordinator,
            store=Mock(),
            workspace=self.path.parent / "broker-workspace",
            read_authority=provider.read_authority,
            verify_event=verify_event or provider.verify_event,
            verify_plan=verify_plan or provider.verify_plan,
            clock=lambda: 1100,
        )

    def test_real_broker_concrete_same_db_policy_observes_once_per_guard(self):
        policy = replace(self.policy, github_repo_id=1210038933)
        plan = copy.deepcopy(self.plan)
        intent = copy.deepcopy(self.ctx["intent"])
        plan["control"]["github_repo_id"] = policy.github_repo_id
        intent["control_repo_id"] = policy.github_repo_id
        provider = ObservedGitCodeAuthority(
            self.client, policy, self.ctx["catalog"], self.path,
            clock=lambda: 1100, coordinator=self.coordinator,
        )
        self._insert_fixture_intent(intent)
        self.client.authority_snapshot.reset_mock()
        broker = GitCodeAdmissionBroker(
            reader=Mock(policy=policy), coordinator=self.coordinator,
            store=Mock(), workspace=self.path.parent / "broker-workspace",
            read_authority=provider.read_authority, verify_event=provider.verify_event,
            verify_plan=provider.verify_plan, clock=lambda: 1100,
        )
        proof = broker.observe_event_current(intent)
        self.assertIsInstance(proof, EventProof)
        self.assertEqual(self.client.authority_snapshot.call_count, 1)
        self.assertTrue(broker.assert_plan_current(plan))
        self.assertEqual(self.client.authority_snapshot.call_count, 2)
        # A second guard performs a new observation: no cross-stage boolean cache.
        self.assertTrue(broker.assert_plan_current(plan))
        self.assertEqual(self.client.authority_snapshot.call_count, 3)

    def test_generic_and_cross_scope_providers_keep_explicit_refresh(self):
        policy = replace(self.policy, github_repo_id=1210038933)
        generic_read = Mock(return_value=Authority(copy.deepcopy(self.plan["pr"]), 1))
        generic_event = Mock(return_value=EventProof("e", "s", 1, 1))
        generic_plan = Mock(return_value=EventProof("e", "s", 1, 1))
        generic = GitCodeAdmissionBroker(
            reader=Mock(policy=policy), coordinator=self.coordinator,
            store=Mock(), workspace=self.path.parent / "generic-workspace",
            read_authority=generic_read, verify_event=generic_event,
            verify_plan=generic_plan, clock=lambda: 1100,
        )
        self.assertFalse(generic._plan_check_refreshes_authority(generic.verify_event))
        generic.observe_event_current(self.ctx["intent"])
        generic_read.assert_called_once_with("42", 1)
        generic_event.assert_called_once()

        other_path = self.path.parent / "other.sqlite3"
        other_coordinator = Coordinator(other_path)
        other_provider = ObservedGitCodeAuthority(
            self.client, policy, self.ctx["catalog"], other_path,
            clock=lambda: 1100, coordinator=other_coordinator,
        )
        cross_db = GitCodeAdmissionBroker(
            reader=Mock(policy=policy), coordinator=self.coordinator,
            store=Mock(), workspace=self.path.parent / "cross-db-workspace",
            read_authority=other_provider.read_authority,
            verify_event=other_provider.verify_event,
            verify_plan=other_provider.verify_plan, clock=lambda: 1100,
        )
        self.assertFalse(cross_db._plan_check_refreshes_authority(cross_db.verify_event))

        other_policy = replace(policy, environment_version="different-policy")
        other_policy_provider = ObservedGitCodeAuthority(
            self.client, other_policy, self.ctx["catalog"], self.path,
            clock=lambda: 1100, coordinator=self.coordinator,
        )
        cross_policy = GitCodeAdmissionBroker(
            reader=Mock(policy=policy), coordinator=self.coordinator,
            store=Mock(), workspace=self.path.parent / "cross-policy-workspace",
            read_authority=other_policy_provider.read_authority,
            verify_event=other_policy_provider.verify_event,
            verify_plan=other_policy_provider.verify_plan, clock=lambda: 1100,
        )
        self.assertFalse(cross_policy._plan_check_refreshes_authority(cross_policy.verify_event))
        wrong_reader = GitCodeAdmissionBroker(
            reader=Mock(policy=other_policy), coordinator=self.coordinator,
            store=Mock(), workspace=self.path.parent / "wrong-reader-workspace",
            read_authority=self.provider.read_authority,
            verify_event=self.provider.verify_event,
            verify_plan=self.provider.verify_plan, clock=lambda: 1100,
        )
        self.assertFalse(wrong_reader._plan_check_refreshes_authority(wrong_reader.verify_event))

    def test_real_broker_startable_source_keeps_gh_read_and_skips_only_same_guard_refresh(self):
        self._insert_fixture_intent(self.ctx["intent"])
        gh = self.plan["github"]
        delivery = {
            "request_id": request_id(self.plan),
            "repo_id": gh["repo_id"],
            "run_id": gh["run_id"],
            "run_attempt": 1,
            "control_sha": self.policy.control_sha,
        }
        self.coordinator.register_intent(
            self.ctx["intent"], delivery, EventProof("event-1", "snapshot-1", 1, 1)
        )
        verified = verify_request(self.plan, **self.ctx)
        admission = self.coordinator.claim(verified, now=1101)
        run_reader = Mock(policy=self.policy)
        stored = object()
        store = Mock()
        store.load_execution.return_value = stored
        with patch("ci_core.gitcode_admission.TORCHTITAN_REPOSITORY_IDS", frozenset({self.policy.github_repo_id})):
            broker = GitCodeAdmissionBroker(
                reader=run_reader, coordinator=self.coordinator, store=store,
                workspace=self.path.parent / "startable-workspace",
                read_authority=self.provider.read_authority,
                verify_event=self.provider.verify_event,
                verify_plan=self.provider.verify_plan,
                clock=lambda: 1101,
            )
        broker.refresh = Mock(side_effect=AssertionError("same-guard duplicate refresh"))
        self.client.authority_snapshot.reset_mock()
        accepted = AcceptedRequest(verified, admission)
        self.assertIs(broker.startable_source(accepted), stored)
        self.assertEqual(self.client.authority_snapshot.call_count, 1)
        run_reader.actual_run.assert_called_once_with(100)
        broker.refresh.assert_not_called()
        # Re-entering the guard must make another fresh observation.
        self.assertIs(broker.startable_source(accepted), stored)
        self.assertEqual(self.client.authority_snapshot.call_count, 2)
        self.assertEqual(run_reader.actual_run.call_count, 2)

    def test_observe_aba_revokes_existing_grant_without_second_reconcile(self):
        execution = self.admit_old()
        self.coordinator.assert_startable(execution, now=1101)
        self.snapshot["identity"]["base_sha"] = "e" * 40
        self.provider.read_authority("42", 1)
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(execution, now=1101)
        self.snapshot["identity"]["base_sha"] = "b" * 40
        self.provider.read_authority("42", 1)
        with self.assertRaisesRegex(StateError, "REVOKED"):
            Coordinator(self.path).assert_startable(execution, now=1101)
        with self.coordinator._connection() as db:
            source = db.execute("SELECT revision FROM source_observations").fetchone()[
                0
            ]
            grant = db.execute("SELECT revision,generation FROM pr_versions").fetchone()
            self.assertEqual((source, *grant), (3, 3, 3))
        self.assertIn(
            "CANCEL_EXECUTION",
            {row["kind"] for row in self.coordinator.pending_outbox()},
        )

    def test_failure_after_observation_revoke_before_intent_insert_rolls_back_all(self):
        execution = self.admit_old()
        tables = (
            "source_observations",
            "source_invalidations",
            "source_intents",
            "pr_versions",
            "intents",
            "executions",
            "outbox",
        )
        before = {table: self.rows(table) for table in tables}
        with self.coordinator._connection() as db:
            db.execute(
                "CREATE TRIGGER fail_source_intent BEFORE INSERT ON source_intents BEGIN SELECT RAISE(ABORT,'injected_crash'); END"
            )
        self.snapshot["identity"]["base_sha"] = "e" * 40
        with self.assertRaises(sqlite3.IntegrityError):
            self.provider.create_intent(
                1, [{"test_id": "vllm_worker_os_blue"}], pipeline="smoke"
            )
        self.assertEqual({table: self.rows(table) for table in tables}, before)
        # The next actual launch must refresh. No partly committed new grant,
        # invalidation or cancellation receipt exists from the aborted operation.
        self.provider.refresh_coordinator("42", 1)
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(execution, now=1101)

    def test_new_spec_atomically_cancels_old_coordinator_execution(self):
        execution = self.admit_old()
        self.provider.create_intent(
            1, [{"test_id": "vllm_worker_os_blue"}], pipeline="smoke"
        )
        with self.assertRaisesRegex(StateError, "REVOKED"):
            self.coordinator.assert_startable(execution, now=1101)
        self.assertEqual(len(self.rows("source_intents")), 1)
        self.assertIn(
            "CANCEL_RUN", {row["kind"] for row in self.coordinator.pending_outbox()}
        )

    def test_network_failure_persists_only_refusal_and_does_not_grant(self):
        execution = self.admit_old()
        before = self.rows("source_observations")
        self.client.authority_snapshot.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.provider.refresh_coordinator("42", 1)
        self.assertEqual(self.rows("source_observations"), before)
        with self.coordinator._connection() as db:
            self.assertEqual(
                db.execute("SELECT available FROM pr_versions").fetchone()[0], 0
            )
        with self.assertRaises(StateError):
            self.coordinator.assert_startable(execution, now=1101)

    def test_repo_scopes_share_one_db_without_pr_number_collision(self):
        other = copy.deepcopy(self.snapshot)
        other["identity"].update(repo_id="43", head_repo_id="43", base_repo_id="43")
        client = Mock(repo_id="43")
        client.authority_snapshot.return_value = other
        provider = ObservedGitCodeAuthority(
            client,
            replace(self.policy, gitcode_repo_id="43"),
            self.ctx["catalog"],
            self.path,
            clock=lambda: 1100,
            coordinator=self.coordinator,
        )
        provider.refresh_coordinator("43", 1)
        self.assertEqual(
            {row[0] for row in self.rows("source_observations")}, {"42", "43"}
        )
        self.assertEqual({row[0] for row in self.rows("pr_versions")}, {"42", "43"})

    def test_v4_migration_preserves_execution_and_lease_and_adds_barrier(self):
        execution = self.admit_old()
        case = self.plan["cases"][1]["case_id"]
        self.coordinator.claim_case(execution, case, now=1102)
        self.coordinator.bind_lease(
            execution,
            case,
            "lease-1",
            {
                "nodes": [
                    {
                        "host_id": "fixture",
                        "container_id": "a" * 64,
                        "image_id": "sha256:" + "b" * 64,
                        "task_marker": "marker",
                        "device_ids": list(range(8)),
                    }
                ]
            },
            now=1103,
        )
        tables = (
            "pr_versions",
            "intents",
            "deliveries",
            "executions",
            "requests",
            "cases",
            "leases",
            "outbox",
        )
        before = {table: self.rows(table) for table in tables}
        with self.coordinator._connection() as db:
            for table in (
                "source_attestations",
                "source_intents",
                "source_invalidations",
                "source_observations",
            ):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=4")
        self.coordinator = Coordinator(self.path)
        self.assertEqual({table: self.rows(table) for table in tables}, before)
        with self.coordinator._connection() as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], STATE_VERSION
            )
            self.assertGreater(
                db.execute("SELECT observed_at FROM source_invalidations").fetchone()[
                    0
                ],
                1100,
            )
        self.assertEqual(self.rows("source_observations")[0][:3], ("42", 1, 1))

    def test_signed_mode_requires_explicit_shared_coordinator(self):
        with self.assertRaisesRegex(PlatformError, "REQUIRES_SHARED"):
            ObservedGitCodeAuthority(
                self.client,
                self.policy,
                self.ctx["catalog"],
                Path(self.temp.name) / "separate.sqlite3",
                attestation_policy=Mock(enabled=True),
            )


if __name__ == "__main__":
    unittest.main()
