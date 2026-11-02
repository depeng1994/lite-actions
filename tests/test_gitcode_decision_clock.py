"""Real SQLite lock tests, synthetic clock; never touches production state."""

import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from ci_core.gitcode_contract import verify_request
from ci_core.gitcode_state import Coordinator, StateError
from tests import test_gitcode_state as fixtures


class DecisionClockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.co = self.fixture.coordinator
        self.now = 1102
        self.clock_calls = []

    def clock(self):
        self.clock_calls.append(self.now)
        return self.now

    def test_real_lock_wait_uses_clock_only_after_acquisition(self):
        admitted = self.fixture.admitted()
        eid = admitted.receipt["canonical_execution_id"]
        self.co.decision_clock = self.clock
        writer = sqlite3.connect(self.fixture.path, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        ready, done = threading.Event(), threading.Event()
        errors = []

        def worker():
            ready.set()
            try:
                self.co.assert_startable(eid, now=1100)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(ready.wait(2))
        time.sleep(0.05)
        self.assertFalse(done.is_set())
        self.assertEqual(self.clock_calls, [])
        writer.execute("UPDATE pr_versions SET observed_at=1102")
        writer.commit()
        writer.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.clock_calls, [1102])
        # The same rows and explicit historical pre-lock time still fail. No
        # max(now, observed_at), timestamp rewrite or future tolerance exists.
        historical = Coordinator(self.fixture.path)
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(StateError):
                historical.assert_startable(eid, now=1100)
        self.assertIn("AUTHORITY_FROM_FUTURE", "".join(logs.output))
        with sqlite3.connect(self.fixture.path) as db:
            self.assertEqual(
                db.execute("SELECT observed_at FROM pr_versions").fetchone()[0], 1102
            )

    def test_all_seven_authorization_paths_sample_locked_clock(self):
        self.fixture.register()
        self.co.decision_clock = self.clock
        verified = verify_request(self.fixture.plan, **self.fixture.ctx)
        admitted = self.co.claim(verified, now=1)  # new claim
        self.co.claim(verified, now=1)  # existing claim
        eid = admitted.receipt["canonical_execution_id"]
        cid = self.fixture.plan["cases"][0]["case_id"]
        self.co.assert_startable(eid, now=1)
        self.assertTrue(self.co.claim_case(eid, cid, now=1))
        self.co.retry_unlaunched_case(eid, cid, now=1)
        self.assertTrue(self.co.claim_case(eid, cid, now=1))
        cleanup = {
            "nodes": [
                {
                    "host_id": "host",
                    "container_id": "container",
                    "image_id": "image",
                    "task_marker": "marker",
                    "device_ids": [0],
                }
            ]
        }
        fence = self.co.bind_lease(eid, cid, "lease", cleanup, now=1)
        self.co.assert_lease_active(eid, cid, "lease", fence, now=1)
        self.assertEqual(self.clock_calls, [1102] * 8)

    def test_claim_deadline_uses_locked_clock_not_supplied_time(self):
        self.fixture.register()
        self.now = self.fixture.plan["deadline"]
        self.co.decision_clock = self.clock
        with sqlite3.connect(self.fixture.path) as db:
            db.execute("UPDATE pr_versions SET observed_at=?", (self.now,))
        with self.assertRaisesRegex(StateError, "REQUEST_GENERATION_NOT_CURRENT"):
            self.co.claim(
                verify_request(self.fixture.plan, **self.fixture.ctx), now=1101
            )

    def test_future_stale_unavailable_cancel_and_deadline_remain_rejected(self):
        admitted = self.fixture.admitted()
        eid = admitted.receipt["canonical_execution_id"]
        self.co.decision_clock = self.clock
        changes = [
            ("UPDATE pr_versions SET observed_at=1103", "AUTHORITY_FROM_FUTURE"),
            ("UPDATE pr_versions SET observed_at=1071", "AUTHORITY_STALE"),
            ("UPDATE pr_versions SET available=0", "AUTHORITY_UNAVAILABLE"),
            ("UPDATE executions SET cancel_requested=1", "EXECUTION_CANCELLED"),
            ("UPDATE executions SET deadline=1102", "EXECUTION_DEADLINE_EXPIRED"),
            (
                "UPDATE pipeline_heads SET publication_revision=99",
                "PUBLICATION_REVISION_CHANGED",
            ),
        ]
        for sql, reason in changes:
            with self.subTest(reason=reason):
                with sqlite3.connect(self.fixture.path) as db:
                    db.execute("UPDATE pr_versions SET observed_at=1100,available=1")
                    db.execute("UPDATE executions SET cancel_requested=0,deadline=5000")
                    db.execute("UPDATE pipeline_heads SET publication_revision=1")
                    db.execute(sql)
                with self.assertLogs(level="WARNING") as logs:
                    with self.assertRaises(StateError):
                        self.co.assert_startable(eid, now=1101)
                self.assertIn(reason, "".join(logs.output))
                self.assertNotIn("intent", "".join(logs.output))

    def test_bound_lease_cannot_start_past_locked_execution_deadline(self):
        admitted = self.fixture.admitted()
        eid = admitted.receipt["canonical_execution_id"]
        cid = self.fixture.plan["cases"][0]["case_id"]
        self.co.claim_case(eid, cid, now=1101)
        cleanup = {
            "nodes": [
                {
                    "host_id": "host",
                    "container_id": "container",
                    "image_id": "image",
                    "task_marker": "marker",
                    "device_ids": [0],
                }
            ]
        }
        fence = self.co.bind_lease(eid, cid, "lease", cleanup, now=1101)
        self.now = self.fixture.plan["deadline"]
        self.co.decision_clock = self.clock
        with sqlite3.connect(self.fixture.path) as db:
            db.execute("UPDATE pr_versions SET observed_at=?", (self.now,))
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaisesRegex(
                StateError, "LEASE_OWNERSHIP_OR_GENERATION_REVOKED"
            ):
                self.co.assert_lease_active(eid, cid, "lease", fence, now=1101)
        self.assertIn("EXECUTION_DEADLINE_EXPIRED", "".join(logs.output))

    def test_invalid_trusted_clock_is_not_replaced_by_supplied_now(self):
        admitted = self.fixture.admitted()
        for invalid in (True, -1, 1102.0, "1102"):
            self.co.decision_clock = lambda: invalid
            with self.assertRaisesRegex(StateError, "INVALID_DECISION_CLOCK"):
                self.co.assert_startable(
                    admitted.receipt["canonical_execution_id"], now=1101
                )

    def test_clock_callback_runs_inside_actual_transaction(self):
        admitted = self.fixture.admitted()
        self.co.decision_clock = self.clock
        original = self.co._locked_decision_now
        locked = []

        def capture(connection, now):
            locked.append(connection.in_transaction)
            return original(connection, now)

        with patch.object(self.co, "_locked_decision_now", side_effect=capture):
            self.co.assert_startable(
                admitted.receipt["canonical_execution_id"], now=1101
            )
        self.assertEqual(locked, [True])
