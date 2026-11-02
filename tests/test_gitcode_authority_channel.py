"""CPU only: fresh exact-generation proof, no native platform acceptance."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.gitcode_authority_channel import (
    AuthorityChannelError,
    AuthorityResponder,
    HostedCurrent,
    artifact_name,
)
from ci_core.gitcode_checks import CreationJournal
from ci_core.gitcode_contract import (
    VerifiedRequest,
    canonical,
    request_id,
    seal_plan,
    source_identity,
)
from ci_core.gitcode_waiter import GitCodeWaiter
from tests.test_gitcode_contract import receipt_for
from tests.test_gitcode_pipeline import HostingAPI


class AuthorityChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = HostingAPI(self.root)
        self.api.producer.authority_protocol = "artifact-check-v1"
        self.plan = self.api.produce()
        self.verified = VerifiedRequest(canonical(self.plan))
        self.receipt = receipt_for(self.plan)
        self.api.channel.publish_receipt(self.plan, self.receipt)
        self.now = 1100
        self.client = self.hosted()
        self.responder = AuthorityResponder(
            self.api.reader,
            self.api.channel,
            resolve_request=lambda rid: (
                self.verified if rid == request_id(self.plan) else None
            ),
            cleanup_journal=CreationJournal(self.root / "authority-closures.sqlite3"),
            clock=lambda: self.now,
        )

    def hosted(self):
        return HostedCurrent(
            self.api.reader,
            self.api.channel,
            self.api.upload,
            state_directory=self.root / "waiter",
            owner_run_id=self.plan["github"]["run_id"],
            clock=lambda: self.now,
        )

    def test_nonce_challenge_and_fixed_slot_current_response(self):
        self.assertIsNone(self.client(self.plan))
        called = []
        self.assertTrue(
            self.responder.respond(
                self.verified,
                self.receipt,
                current=lambda verified, receipt: (
                    called.append((verified.plan, receipt)) or True
                ),
            )
        )
        self.assertTrue(self.client(self.plan))
        self.assertEqual(called, [(self.plan, self.receipt)])
        self.assertEqual(self.api.uploads.count(artifact_name(self.plan)), 1)
        slot = self.api.channel.read_slot(self.plan, "authority")
        self.assertEqual(slot["status"], "in_progress")
        self.assertIsNone(slot["conclusion"])

    def test_same_owner_receipt_is_read_once_per_call_not_cached_between_polls(self):
        with patch.object(
            self.api.channel, "read_receipt", wraps=self.api.channel.read_receipt
        ) as read:
            self.assertIsNone(self.client(self.plan))
            read.assert_called_once_with(self.plan)
            read.reset_mock()
            self.assertIsNone(self.client(self.plan))
            read.assert_called_once_with(self.plan)

    def test_revoked_or_unavailable_never_returns_cached_pass(self):
        self.client(self.plan)
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        self.assertTrue(self.client(self.plan))
        self.responder.respond(self.verified, self.receipt, current=lambda *args: False)
        self.assertFalse(self.client(self.plan))

        def offline(*args):
            raise OSError("authority unreachable")

        self.responder.respond(self.verified, self.receipt, current=offline)
        self.assertFalse(self.client(self.plan))

    def test_response_freshness_and_challenge_expiry_are_bounded(self):
        self.client(self.plan)
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        self.now += 16
        self.assertIsNone(self.client(self.plan))
        self.now = 1280
        with self.assertRaisesRegex(AuthorityChannelError, "EXPIRED"):
            self.client(self.plan)
        self.assertEqual(self.api.uploads.count(artifact_name(self.plan)), 1)

    def test_unknown_upload_response_recovers_exact_content_without_retry(self):
        self.api.lost_after = artifact_name(self.plan)
        self.assertIsNone(self.client(self.plan))
        self.client = self.hosted()
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        self.assertTrue(self.client(self.plan))
        self.assertEqual(self.api.uploads.count(artifact_name(self.plan)), 1)

    def test_upload_never_retried_if_unknown_before_creation(self):
        self.api.lost_before = artifact_name(self.plan)
        self.assertIsNone(self.client(self.plan))
        self.client = self.hosted()
        self.assertIsNone(self.client(self.plan))
        self.assertEqual(self.api.uploads.count(artifact_name(self.plan)), 1)

    def test_wrong_nonce_or_generation_cannot_grant(self):
        self.client(self.plan)
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        check_id = self.plan["result_slots"]["authority"]["id"]
        original = copy.deepcopy(self.api.checks[check_id])
        payload = self.api.channel._payload(original)
        payload["challenge"]["nonce"] = "0" * 32
        self.api.checks[check_id]["output"]["summary"] = canonical(payload).decode()
        with self.assertRaisesRegex(AuthorityChannelError, "BINDING"):
            self.client(self.plan)
        self.api.checks[check_id] = original
        changed = {**self.receipt, "execution_generation": 2}
        with self.assertRaisesRegex(AuthorityChannelError, "TARGET_CHANGED"):
            self.responder.respond(self.verified, changed, current=lambda *args: True)

    def test_duplicate_owns_challenge_but_targets_exact_canonical_generation(self):
        duplicate = copy.deepcopy(self.plan)
        duplicate["github"].update(run_id=200, prepare_job_id=201)
        duplicate["result_slots"] = {}
        duplicate = seal_plan(duplicate)
        duplicate["result_slots"] = self.api.channel.precreate(duplicate)
        duplicate = seal_plan(duplicate)
        receipt = {
            **self.receipt,
            "request_id": request_id(duplicate),
            "plan_digest": duplicate["plan_digest"],
            "duplicate": True,
        }
        pointer = {
            "request_id": request_id(self.plan),
            "plan_digest": self.plan["plan_digest"],
            "github": self.plan["github"],
            "result_slots": self.plan["result_slots"],
        }
        self.api.channel.publish_receipt(
            duplicate, receipt, canonical_execution=pointer
        )
        self.api.run["id"] = 200
        original_request = self.api.request
        self.api.request = lambda method, path, data=None: original_request(
            method, path.replace("/actions/runs/200", "/actions/runs/100"), data
        )
        original_upload = self.api.upload

        def upload(name, path):
            ident = original_upload(name, path)
            self.api.metadata[ident]["workflow_run"]["id"] = 200
            return ident

        self.api.upload = upload

        # Preserve the real load_results keyword-only interface. Only its
        # platform materialization boundary is synthetic in this protocol test.
        def load(run_id, workspace, *, now, results_only):
            self.assertEqual(run_id, 200)
            self.assertTrue(results_only)
            return SimpleNamespace(verified=VerifiedRequest(canonical(duplicate)))

        self.api.reader._load = load
        client = HostedCurrent(
            self.api.reader,
            self.api.channel,
            self.api.upload,
            state_directory=self.root / "duplicate",
            owner_run_id=200,
            clock=lambda: self.now,
        )
        with patch.object(
            self.api.channel, "read_receipt", wraps=self.api.channel.read_receipt
        ) as read:
            self.assertIsNone(client(self.plan))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args_list[0].args, (duplicate,))
            self.assertEqual(read.call_args_list[1].args, (self.plan,))
        calls = []
        self.responder.respond(
            VerifiedRequest(canonical(duplicate)),
            receipt,
            current=lambda verified, received: (
                calls.append((verified.plan, received)) or True
            ),
        )
        self.assertTrue(client(self.plan))
        self.assertEqual(calls, [(self.plan, self.receipt)])
        self.assertIn("lite-authority-200-1", self.api.uploads)

        original_receipt = self.api.channel.read_receipt

        def changed_generation(plan):
            envelope = copy.deepcopy(original_receipt(plan))
            if plan["github"]["run_id"] == 200:
                envelope["receipt"]["execution_generation"] += 1
            return envelope

        self.api.channel.read_receipt = changed_generation
        with self.assertRaisesRegex(AuthorityChannelError, "GENERATION_MISMATCH"):
            client(self.plan)

    def test_follower_owner_uses_real_completed_results_reader(self):
        target = copy.deepcopy(self.plan)
        target["github"]["run_id"] = 999
        self.api.run.update(status="completed", conclusion="success")
        self.api.job.update(status="completed", conclusion="success")
        # Actual reader, authenticated-artifact fixture and actual keyword-only
        # load_results API; no replacement of load_results or _load.
        self.assertEqual(self.client._own_plan(target), self.plan)

    def test_follower_owner_rejects_actual_reader_identity_failure(self):
        target = copy.deepcopy(self.plan)
        target["github"]["run_id"] = 999
        self.api.run["head_sha"] = "f" * 40
        with self.assertRaises(ValueError):
            self.client._own_plan(target)
        self.assertIsNone(self.client._owner)

    def test_waiter_cannot_finish_until_fresh_proof_after_complete_results(self):
        waiter = GitCodeWaiter(self.verified, self.api.channel, current=self.client)
        self.assertEqual(
            waiter.poll(now=self.now)["reason"], "AWAITING_COMPLETE_AGGREGATE"
        )
        self.assertNotIn(artifact_name(self.plan), self.api.uploads)
        results = []
        for case in self.plan["cases"]:
            result = {
                "case_id": case["case_id"],
                "receipt": self.receipt,
                "identity": source_identity(self.plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": [],
            }
            self.api.channel.publish_case(self.plan, self.receipt, result)
            results.append(result)
        self.api.channel.publish_aggregate(
            self.plan, self.receipt, results, current=True
        )
        self.assertEqual(
            waiter.poll(now=self.now)["reason"], "AWAITING_FRESH_AUTHORITY"
        )
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        self.assertEqual(waiter.poll(now=self.now)["test_result"], "PASS")

    def test_slow_aggregate_confirmation_cannot_extend_authority_ttl(self):
        results = []
        for case in self.plan["cases"]:
            result = {
                "case_id": case["case_id"],
                "receipt": self.receipt,
                "identity": source_identity(self.plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": [],
            }
            self.api.channel.publish_case(self.plan, self.receipt, result)
            results.append(result)
        self.api.channel.publish_aggregate(
            self.plan, self.receipt, results, current=True
        )
        self.client(self.plan)
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        original = self.api.channel.read_slot
        reads = []

        def slow(plan, case_id):
            check = original(plan, case_id)
            if case_id == "aggregate":
                reads.append(case_id)
                if len(reads) == 2:
                    self.now += 16
            return check

        self.api.channel.read_slot = slow
        waiter = GitCodeWaiter(self.verified, self.api.channel, current=self.client)
        self.assertEqual(waiter.poll(now=1100)["reason"], "AWAITING_FRESH_AUTHORITY")

    def test_slow_current_observation_cannot_be_timestamped_fresh_at_end(self):
        self.client(self.plan)

        def slow(*args):
            self.now += 16
            return True

        with self.assertRaisesRegex(AuthorityChannelError, "OBSERVATION_EXPIRED"):
            self.responder.respond(self.verified, self.receipt, current=slow)
        self.assertIsNone(self.client(self.plan))

    def test_authority_slot_closes_only_neutral_after_owner_terminal(self):
        self.client(self.plan)
        self.responder.respond(self.verified, self.receipt, current=lambda *args: True)
        self.assertFalse(self.responder.cleanup(self.verified, self.receipt))
        self.assertEqual(
            self.api.channel.read_slot(self.plan, "authority")["status"], "in_progress"
        )
        self.api.run.update(status="completed", conclusion="cancelled")
        self.now += 200  # Expiry is not terminal proof, but must not block cleanup.
        self.assertTrue(self.responder.cleanup(self.verified, self.receipt))
        check = self.api.channel.read_slot(self.plan, "authority")
        self.assertEqual(
            (check["status"], check["conclusion"]), ("completed", "neutral")
        )
        with self.assertRaises(Exception):
            self.client(self.plan)
        with self.assertRaises(Exception):
            self.responder.respond(
                self.verified, self.receipt, current=lambda *args: True
            )

    def test_lost_neutral_patch_response_is_idempotent_and_durably_acked(self):
        self.api.run.update(status="completed", conclusion="success")
        original = self.api.request
        writes = []

        def lost(method, path, data=None):
            result = original(method, path, data)
            if method == "PATCH":
                writes.append(path)
                raise TimeoutError("response lost after neutral PATCH")
            return result

        self.api.request = lost
        with self.assertRaises(TimeoutError):
            self.responder.cleanup(self.verified, self.receipt)
        self.api.request = original
        self.assertTrue(self.responder.cleanup(self.verified, self.receipt))
        self.assertEqual(len(writes), 1)
        self.api.request = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("offline")
        )
        self.assertTrue(self.responder.cleanup(self.verified, self.receipt))

    def test_no_protocol_cannot_silently_fall_back(self):
        old = copy.deepcopy(self.plan)
        del old["authority_protocol"]
        with self.assertRaisesRegex(AuthorityChannelError, "PROTOCOL"):
            self.client(old)


if __name__ == "__main__":
    unittest.main()
