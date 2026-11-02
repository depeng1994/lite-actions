"""Mock compensation tests, explicitly not live event-history proof."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ci_core.gitcode_reconcile import GitCodeCompensator
from ci_core.gitcode_state import EventProof


class CompensationTests(unittest.TestCase):
    def setUp(self):
        self.policy = SimpleNamespace(
            gitcode_repo_id="42",
            allowed_base_refs=("master",),
            allowed_actors=("owner",),
        )
        self.client = Mock(repo_id="42")
        self.client.open_pull_numbers.return_value = [1, 2]
        self.client.open_pull_snapshot.side_effect = lambda number, **kw: {
            "identity": {"repo_id": "42", "number": number}
        }
        self.broker = Mock(reader=SimpleNamespace(policy=self.policy))
        self.broker.reconcile_registered.return_value = {
            "refreshed": [],
            "unavailable": [],
        }
        self.broker.verify_event.return_value = EventProof("event", "snapshot", 1, 1)
        self.bridge = Mock(personal=SimpleNamespace(repo_id="42"), policy=self.policy)
        self.bridge.dispatch.return_value = {"run_id": 7}
        self.intent_for = Mock(
            side_effect=lambda snapshot: {
                "pr": snapshot["identity"],
                "bridge": {
                    "bridge_event_id": "event",
                    "source_snapshot_id": "snapshot",
                },
            }
        )
        self.compensator = GitCodeCompensator(
            client=self.client,
            broker=self.broker,
            bridge=self.bridge,
            intent_for=self.intent_for,
            clock=lambda: 100,
        )

    def test_discovery_outage_does_not_prevent_registered_revocation(self):
        self.client.open_pull_numbers.side_effect = OSError("do-not-print-secret")
        result = self.compensator.tick()
        self.broker.reconcile_registered.assert_called_once()
        self.bridge.dispatch.assert_not_called()
        self.assertEqual(
            result["pending"], [{"stage": "discovery", "error_type": "OSError"}]
        )
        self.assertNotIn("secret", str(result))

    def test_one_pr_failure_does_not_block_other_pr(self):
        self.broker.refresh.side_effect = [OSError("offline"), 1]
        result = self.compensator.tick()
        self.assertEqual(result["pending"], [{"number": 1, "error_type": "OSError"}])
        self.assertEqual(
            result["dispatched"], [{"number": 2, "delivery": {"run_id": 7}}]
        )
        self.bridge.dispatch.assert_called_once()

    def test_authority_is_refreshed_before_dispatch_and_covered_is_noop(self):
        order = []
        self.broker.refresh.side_effect = lambda *args: order.append("refresh")
        self.intent_for.side_effect = lambda snapshot: order.append("intent")
        result = self.compensator.tick()
        self.assertEqual(order, ["refresh", "intent", "refresh", "intent"])
        self.assertEqual(result["covered"], [1, 2])
        self.bridge.dispatch.assert_not_called()

    def test_fabricated_or_mismatched_event_proof_never_dispatches(self):
        for value in (
            {},
            EventProof("wrong", "snapshot", 1, 1),
            EventProof("event", "snapshot", 0, 1),
        ):
            with self.subTest(value=value):
                self.broker.verify_event.return_value = value
                result = self.compensator.tick()
                self.assertEqual(len(result["pending"]), 2)
                self.bridge.dispatch.assert_not_called()

    def test_mismatched_snapshot_never_dispatches(self):
        self.intent_for.side_effect = lambda snapshot: {"pr": {"number": 999}}
        self.assertEqual(len(self.compensator.tick()["pending"]), 2)
        self.bridge.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
