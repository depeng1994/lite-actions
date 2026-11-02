"""Atomic SQLite source/coordinator regressions; no platform/hardware claims."""

import copy
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_contract import request_id, verify_request
from ci_core.gitcode_platform import ObservedGitCodeAuthority, PlatformError
from ci_core.gitcode_state import STATE_VERSION, Coordinator, EventProof, StateError
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
