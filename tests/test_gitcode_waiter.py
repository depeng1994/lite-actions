"""Fixed-ID canonical result following; no GitHub workflow conclusion shortcut."""

import copy
import tempfile
import unittest
from pathlib import Path

from ci_core.gitcode_contract import (
    VerifiedRequest,
    canonical,
    request_id,
    seal_plan,
    source_identity,
)
from ci_core.gitcode_waiter import GitCodeWaiter, WaiterError
from tests.test_gitcode_pipeline import HostingAPI


class DuplicateWaiterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = HostingAPI(Path(self.tmp.name))
        self.plan = self.api.produce()
        self.receipt = {
            "request_id": request_id(self.plan),
            "plan_digest": self.plan["plan_digest"],
            "request_spec_digest": self.plan["request_spec_digest"],
            "source_snapshot_id": self.plan["bridge"]["source_snapshot_id"],
            "dispatch_intent_id": self.plan["bridge"]["dispatch_intent_id"],
            "canonical_execution_id": "cpu-test-canonical-identity",
            "pr_generation": 1,
            "execution_generation": 1,
            "publication_revision": 1,
            "duplicate": False,
        }
        self.api.channel.publish_receipt(self.plan, self.receipt)
        self.results = []
        for case in self.plan["cases"]:
            result = {
                "case_id": case["case_id"],
                "receipt": self.receipt,
                "identity": source_identity(self.plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": ["Waiter CPU fixture, not a hardware execution"],
            }
            self.api.channel.publish_case(self.plan, self.receipt, result)
            self.results.append(result)
        self.api.channel.publish_aggregate(
            self.plan, self.receipt, self.results, current=True
        )
        duplicate = copy.deepcopy(self.plan)
        duplicate["github"].update(run_id=200, prepare_job_id=201)
        duplicate["result_slots"] = {}
        duplicate = seal_plan(duplicate)
        duplicate["result_slots"] = self.api.channel.precreate(duplicate)
        self.duplicate = seal_plan(duplicate)
        self.duplicate_receipt = {
            **self.receipt,
            "request_id": request_id(self.duplicate),
            "plan_digest": self.duplicate["plan_digest"],
            "duplicate": True,
        }
        self.pointer = {
            "request_id": request_id(self.plan),
            "plan_digest": self.plan["plan_digest"],
            "github": self.plan["github"],
            "result_slots": self.plan["result_slots"],
        }
        self.api.channel.publish_receipt(
            self.duplicate, self.duplicate_receipt, canonical_execution=self.pointer
        )

    def waiter(self, **kwargs):
        return GitCodeWaiter(
            VerifiedRequest(canonical(self.duplicate)),
            self.api.channel,
            current=lambda plan: True,
            resolve_canonical=kwargs.get(
                "resolve", lambda pointer: VerifiedRequest(canonical(self.plan))
            ),
        )

    def change_envelope(self, **changes):
        slot = self.api.checks[self.duplicate["result_slots"]["aggregate"]["id"]]
        data = self.api.channel._payload(slot)
        data.update(changes)
        slot["output"]["summary"] = canonical(data).decode()

    def test_duplicate_follows_only_fixed_canonical_case_slots(self):
        self.api.calls.clear()
        outcome = self.waiter().poll(now=1100)
        self.assertEqual(outcome["test_result"], "PASS")
        self.assertEqual(outcome["receipt"], self.receipt)
        self.assertEqual(
            outcome["followed_from_request_id"], request_id(self.duplicate)
        )
        duplicate_cases = {
            x["id"]
            for k, x in self.duplicate["result_slots"].items()
            if k != "aggregate"
        }
        self.assertFalse(
            any(
                path == f"/check-runs/{ident}"
                for _, path in self.api.calls
                for ident in duplicate_cases
            )
        )
        self.assertFalse(
            any(
                "/actions/runs/" in path or "comments" in path
                for _, path in self.api.calls
            )
        )

    def test_unverified_or_wrong_canonical_plan_is_rejected(self):
        with self.assertRaisesRegex(WaiterError, "VERIFIED_CANONICAL"):
            self.waiter(resolve=lambda p: self.plan).poll(now=1100)
        wrong = copy.deepcopy(self.plan)
        wrong["github"]["run_id"] = 999
        with self.assertRaisesRegex(WaiterError, "POINTER_PLAN"):
            self.waiter(resolve=lambda p: VerifiedRequest(canonical(wrong))).poll(
                now=1100
            )

    def test_pointer_cannot_redirect_to_another_case_specification(self):
        wrong = copy.deepcopy(self.plan)
        wrong["control"]["control_sha"] = "0" * 40
        wrong = seal_plan(wrong)
        self.change_envelope(
            canonical={
                "request_id": request_id(wrong),
                "plan_digest": wrong["plan_digest"],
                "github": wrong["github"],
                "result_slots": wrong["result_slots"],
            }
        )
        with self.assertRaisesRegex(WaiterError, "SPECIFICATION_MISMATCH"):
            self.waiter(resolve=lambda p: VerifiedRequest(canonical(wrong))).poll(
                now=1100
            )

    def test_different_execution_generation_cannot_use_old_green(self):
        receipt = {**self.duplicate_receipt, "execution_generation": 2}
        self.change_envelope(receipt=receipt)
        with self.assertRaisesRegex(WaiterError, "EXECUTION_RECEIPT"):
            self.waiter().poll(now=1100)

    def test_self_reference_is_not_a_canonical_result(self):
        self.change_envelope(
            canonical={**self.pointer, "request_id": request_id(self.duplicate)}
        )
        with self.assertRaisesRegex(WaiterError, "CYCLE"):
            self.waiter().poll(now=1100)

    def test_missing_canonical_resolver_never_uses_duplicate_run_conclusion(self):
        with self.assertRaisesRegex(WaiterError, "RESOLVER_REQUIRED"):
            self.waiter(resolve=None).poll(now=1100)

    def test_follower_authority_must_still_be_current(self):
        waiter = self.waiter()
        self.assertEqual(waiter.poll(now=1100)["test_result"], "PASS")
        waiter.follower.current = lambda plan: False
        self.assertEqual(waiter.poll(now=1101)["test_result"], "STALE")


if __name__ == "__main__":
    unittest.main()
