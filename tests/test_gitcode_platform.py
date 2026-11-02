"""Observed-history ledger fault tests; not native event/hidden ABA proof."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_contract import select_cases
from ci_core.gitcode_platform import ObservedGitCodeAuthority, PlatformError
from ci_core.gitcode_state import Coordinator
from tests.test_gitcode_contract import fixture


class ObservedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        plan, ctx = fixture()
        self.policy = ctx["policy"]
        self.client = Mock(repo_id=self.policy.gitcode_repo_id)
        self.snapshot = {
            "identity": copy.deepcopy(plan["pr"]),
            "state": "open",
            "authorized": True,
        }
        self.client.authority_snapshot.side_effect = lambda *a, **k: copy.deepcopy(
            self.snapshot
        )
        self.client.authenticated_actor.return_value = self.policy.allowed_actors[0]
        self.catalog = ctx["catalog"]
        self.path = Path(self.temp.name) / "events.sqlite3"
        self.provider = self.reopen()
        self.selectors = [{"test_id": "vllm_worker_os_blue"}]

    def reopen(self):
        return ObservedGitCodeAuthority(
            self.client, self.policy, self.catalog, self.path, lambda: 1100
        )

    def create(self, **kwargs):
        return self.provider.create_intent(
            self.snapshot["identity"]["number"],
            self.selectors,
            pipeline="smoke",
            **kwargs,
        )

    def test_fallback_coordinator_has_trusted_decision_clock(self):
        self.assertEqual(self.provider.coordinator.decision_clock(), 1100)
        self.provider.clock = lambda: 1102
        self.assertEqual(self.provider.coordinator.decision_clock(), 1102)

    def test_restart_and_retries_reuse_original_intent_and_proof(self):
        intent = self.create()
        proof = self.provider.verify_event(intent)
        self.provider = self.reopen()
        self.assertEqual(self.create(), intent)
        self.assertEqual(self.provider.verify_event(intent), proof)

    def test_observed_aba_revokes_even_when_rejection_first_observes_b(self):
        intent = self.create()
        original = copy.deepcopy(self.snapshot)
        self.snapshot["identity"]["base_sha"] = "e" * 40
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(intent)
        self.snapshot = original
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(intent)
        self.assertNotEqual(
            self.create()["bridge"]["dispatch_intent_id"],
            intent["bridge"]["dispatch_intent_id"],
        )

    def test_observed_aba_between_coordinator_polls_advances_generation(self):
        coordinator = Coordinator(Path(self.temp.name) / "coordinator.sqlite3")
        pr = self.snapshot["identity"]

        def read():
            return self.provider.read_authority(pr["repo_id"], pr["number"])

        self.assertEqual(
            coordinator.reconcile(pr["repo_id"], pr["number"], read, now=1100), 1
        )
        self.snapshot["identity"]["base_sha"] = "e" * 40
        read()
        self.snapshot["identity"]["base_sha"] = pr["base_sha"] = "b" * 40
        read()
        self.assertEqual(
            coordinator.reconcile(pr["repo_id"], pr["number"], read, now=1100), 2
        )

    def test_verify_plan_binds_cases_and_rejects_newer_authorized_rerun(self):
        intent = self.create()
        plan, _ = fixture()
        plan["bridge"] = copy.deepcopy(intent["bridge"])
        plan["cases"] = select_cases(self.selectors, self.catalog)
        self.provider.verify_plan(plan)
        plan["cases"][0]["test_id"] = "changed"
        with self.assertRaisesRegex(PlatformError, "BINDING_MISMATCH"):
            self.provider.verify_plan(plan)
        plan["cases"] = select_cases(self.selectors, self.catalog)
        self.create(trigger_kind="manual", rerun_intent_id="rerun-2")
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_plan(plan)

    def test_failed_closed_create_does_not_roll_back_revocation(self):
        intent = self.create()
        self.snapshot["state"] = "closed"
        with self.assertRaisesRegex(PlatformError, "NOT_AUTHORIZED"):
            self.create()
        self.snapshot["state"] = "open"
        self.provider = self.reopen()
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(intent)

    def test_unknown_or_changed_late_intent_never_enrolled(self):
        intent = self.create()
        for field, value in (
            ("dispatch_intent_id", "unknown"),
            ("bridge_event_id", "old"),
        ):
            changed = copy.deepcopy(intent)
            changed["bridge"][field] = value
            with self.assertRaisesRegex(PlatformError, "UNKNOWN_OR_CHANGED"):
                self.provider.verify_event(changed)

    def test_base_retarget_close_and_authorization_loss_revoke(self):
        for mutate in (
            lambda s: s["identity"].update(base_ref="release"),
            lambda s: s.update(state="closed"),
            lambda s: s.update(authorized=False),
        ):
            intent = self.create()
            before = copy.deepcopy(self.snapshot)
            mutate(self.snapshot)
            with self.assertRaisesRegex(PlatformError, "REVOKED"):
                self.provider.verify_event(intent)
            self.snapshot = before
            with self.assertRaisesRegex(PlatformError, "REVOKED"):
                self.provider.verify_event(intent)

    def test_unavailable_authority_never_verifies_or_creates(self):
        intent = self.create()
        self.client.authority_snapshot.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.provider.verify_event(intent)
        with self.assertRaises(OSError):
            self.create()

    def test_periodic_cannot_claim_action_or_authorize_rerun(self):
        with self.assertRaisesRegex(PlatformError, "REAL_ACTION"):
            self.create(trigger_kind="action")
        with self.assertRaisesRegex(PlatformError, "CANNOT_AUTHORIZE"):
            self.create(rerun_intent_id="rerun-2")
        first = self.create()
        second = self.create(trigger_kind="manual", rerun_intent_id="rerun-2")
        self.assertNotEqual(first, second)
        self.assertTrue(self.provider.verify_event(second).authorized_rerun)
        self.assertEqual(self.create(), second)
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            self.provider.verify_event(first)
        self.assertTrue(first["bridge"]["bridge_action_run"].startswith("observer-"))

    def test_same_sha_selector_change_does_not_reuse_intent(self):
        first = self.create()
        self.selectors.append({"test_id": "dispatch_smoke_a3_8p"})
        second = self.create()
        self.assertNotEqual(
            first["bridge"]["dispatch_intent_id"],
            second["bridge"]["dispatch_intent_id"],
        )


if __name__ == "__main__":
    unittest.main()
