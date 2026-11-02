"""CPU regression: durable poisoned-outbox isolation, not platform evidence."""

import tempfile
import unittest
from pathlib import Path

from ci_core.gitcode_delivery import GitCodeDelivery
from ci_core.gitcode_state import STATE_VERSION, Coordinator, StateError


class OutboxFairnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = Coordinator(self.path)
        self.calls = []

    def seed(self, poison_kind, final_kind):
        with self.state._transaction() as db:
            for i in range(101):
                self.state._outbox(db, poison_kind, f"poison-{i}", {"poison": i})
            self.state._outbox(db, final_kind, "healthy", {"healthy": True})

    def delivery(self):
        def callback(payload):
            if "poison" in payload:
                raise RuntimeError("simulated permanent remote outage")
            self.calls.append(payload)
            return True

        return GitCodeDelivery(
            self.state,
            resolve_request=lambda *args: None,
            channel_for=lambda *args: None,
            cancel_run=callback,
            cancel_execution=callback,
            cleanup_ack=callback,
        )

    def test_poison_cancellations_do_not_starve_local_cleanup_after_restart(self):
        for kind in ("CANCEL_EXECUTION", "LEASE_CLEANUP"):
            with self.subTest(kind=kind):
                self.setUp()
                self.seed("CANCEL_RUN", kind)
                delivery = self.delivery()
                with self.assertLogs(level="WARNING"):
                    first = delivery.flush()
                self.assertEqual(len(first["pending"]), 100)
                self.assertFalse(self.calls)
                # Recreate both objects: fairness must not be only an in-memory cursor.
                self.state = Coordinator(self.path)
                with self.assertLogs(level="WARNING"):
                    second = self.delivery().flush()
                self.assertEqual(len(second["delivered"]), 1)
                self.assertEqual(self.calls, [{"healthy": True}])
                with self.state._connection() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT COUNT(*) FROM outbox WHERE acknowledged=0"
                        ).fetchone()[0],
                        101,
                    )

    def test_poison_fixed_slots_do_not_hide_later_valid_result(self):
        self.seed("CASE_RESULT", "CASE_RESULT")
        delivery = self.delivery()

        def deliver(record):
            if "poison" in record["payload"]:
                raise RuntimeError("fixed slot unavailable")
            self.calls.append(record["id"])

        delivery._deliver = deliver
        with self.assertLogs(level="WARNING"):
            delivery.flush()
        with self.assertLogs(level="WARNING"):
            result = delivery.flush()
        self.assertEqual(result["delivered"], [102])
        self.assertEqual(self.calls, [102])

    def test_attempt_is_not_ack_and_is_identity_bound(self):
        self.seed("CANCEL_RUN", "LEASE_CLEANUP")
        row = self.state.pending_outbox(1)[0]
        with self.assertRaisesRegex(StateError, "ATTEMPT_IDENTITY"):
            self.state.attempted(row["id"], "wrong")
        self.state.attempted(row["id"], row["payload_digest"])
        self.assertNotEqual(self.state.pending_outbox(1)[0]["id"], row["id"])
        with self.state._connection() as db:
            self.assertEqual(
                db.execute(
                    "SELECT acknowledged FROM outbox WHERE id=?", (row["id"],)
                ).fetchone()[0],
                0,
            )

    def test_scope_pages_exceed_lifetime_limit_and_capture_upper_bound(self):
        with self.state._transaction() as db:
            for number in range(1, 1003):
                db.execute(
                    "INSERT INTO pr_versions VALUES(?,?,1,'{}',1,1,1)",
                    ("42", number),
                )
        scopes = self.state.iter_registered_scopes(page_size=37)
        self.assertEqual(next(scopes), ("42", 1))
        with self.state._transaction() as db:
            db.execute("INSERT INTO pr_versions VALUES('42',1003,1,'{}',1,1,1)")
        remaining = list(scopes)
        self.assertEqual(len(remaining), 1001)
        self.assertEqual(remaining[-1], ("42", 1002))
        self.assertEqual(list(self.state.iter_registered_scopes())[-1], ("42", 1003))

    def test_v3_migration_preserves_pending_rows_and_adds_retry_ledger(self):
        self.seed("CANCEL_RUN", "LEASE_CLEANUP")
        before = self.state.pending_outbox()
        with self.state._connection() as db:
            db.execute("DROP TABLE outbox_attempts")
            db.execute("PRAGMA user_version=3")
        self.state = Coordinator(self.path)
        self.assertEqual(self.state.pending_outbox(), before)
        with self.state._connection() as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], STATE_VERSION
            )
        row = before[0]
        self.state.attempted(row["id"], row["payload_digest"])
        self.assertNotEqual(
            Coordinator(self.path).pending_outbox(1)[0]["id"], row["id"]
        )


if __name__ == "__main__":
    unittest.main()
