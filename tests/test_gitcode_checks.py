"""Check channel transport tests; these do not assert a real App is installed."""

import copy
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ci_core.gitcode_checks import ChannelError, CheckRunChannel, CreationJournal
from ci_core.gitcode_contract import seal_plan, source_identity
from tests.test_gitcode_contract import fixture, receipt_for


class FakeAPI:
    repo_id = 1438625957
    repo = "cann-framework/vllm-ascend"

    def __init__(self):
        self.checks, self.calls = {}, []
        self.lose_create_response = False
        self.invisible = False
        self.suites = 1

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if method == "POST":
            ident = 1000 + len(self.checks)
            check = {
                **copy.deepcopy(data),
                "id": ident,
                "app": {"id": 81},
                "conclusion": None,
                "url": f"https://api.github.com/repos/{self.repo}/check-runs/{ident}",
            }
            self.checks[ident] = check
            if self.lose_create_response:
                self.lose_create_response = False
                raise TimeoutError("response lost after commit")
            return copy.deepcopy(check)
        if "/check-suites?" in path:
            return {"total_count": self.suites, "check_suites": []}
        if "/check-runs?" in path:
            args = parse_qs(urlsplit(path).query)
            assert args["filter"] == ["all"]
            assert args["app_id"] == ["81"]
            checks = [
                copy.deepcopy(v)
                for v in self.checks.values()
                if v["name"] == args["check_name"][0]
            ]
            if self.invisible:
                checks = []
            return {"total_count": len(checks), "check_runs": checks}
        ident = int(path.rsplit("/", 1)[-1])
        if ident not in self.checks:
            raise FileNotFoundError("404")
        if method == "PATCH":
            self.checks[ident].update(copy.deepcopy(data))
        return copy.deepcopy(self.checks[ident])


class CheckChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "slots.sqlite3"
        self.api = FakeAPI()
        self.channel = CheckRunChannel(
            self.api,
            repo_id=self.api.repo_id,
            app_id=81,
            journal=CreationJournal(self.path),
        )
        self.plan, _ = fixture()

    def ready(self):
        self.plan["result_slots"] = self.channel.precreate(self.plan)
        self.plan = seal_plan(self.plan)
        return receipt_for(self.plan)

    def result(self, receipt, index=0):
        return {
            "case_id": self.plan["cases"][index]["case_id"],
            "receipt": receipt,
            "identity": source_identity(self.plan),
            "status": "PASS",
            "cleanup_verified": True,
            "summary": ["fixture passed and cleaned"],
        }

    def test_slots_are_created_once_and_read_by_bound_ids(self):
        self.ready()
        before = sum(c[0] == "POST" for c in self.api.calls)
        self.channel.precreate(self.plan)
        self.assertEqual(before, 3)
        self.assertEqual(sum(c[0] == "POST" for c in self.api.calls), before)
        self.assertFalse(any("/commits/" in c[1] for c in self.api.calls))

    def test_lost_create_response_recovers_without_second_post(self):
        self.api.lose_create_response = True
        slot = self.channel.create_slot(self.plan, "aggregate")
        self.assertEqual(slot["id"], 1000)
        self.assertEqual(sum(c[0] == "POST" for c in self.api.calls), 1)
        self.assertTrue(any("filter=all" in c[1] for c in self.api.calls))

    def test_invisible_unknown_create_never_retries_post_after_restart(self):
        self.api.lose_create_response = True
        self.api.invisible = True
        with self.assertRaisesRegex(ChannelError, "UNKNOWN_OR_AMBIGUOUS"):
            self.channel.create_slot(self.plan, "aggregate")
        restored = CheckRunChannel(
            self.api,
            repo_id=self.api.repo_id,
            app_id=81,
            journal=CreationJournal(self.path),
        )
        with self.assertRaisesRegex(ChannelError, "UNKNOWN_OR_AMBIGUOUS"):
            restored.create_slot(self.plan, "aggregate")
        self.assertEqual(sum(c[0] == "POST" for c in self.api.calls), 1)
        self.api.invisible = False
        self.assertEqual(restored.create_slot(self.plan, "aggregate")["id"], 1000)

    def test_truncated_scope_and_ambiguous_slots_fail_closed(self):
        self.api.lose_create_response = True
        self.api.suites = 1000
        with self.assertRaisesRegex(ChannelError, "SCOPE_NOT_PROVABLY_COMPLETE"):
            self.channel.create_slot(self.plan, "aggregate")
        self.api.suites = 1
        self.api.checks[1001] = {**copy.deepcopy(self.api.checks[1000]), "id": 1001}
        with self.assertRaisesRegex(ChannelError, "UNKNOWN_OR_AMBIGUOUS"):
            self.channel.create_slot(self.plan, "aggregate")

    def test_bound_slot_deleted_never_falls_back_to_name_search(self):
        self.ready()
        aggregate = self.plan["result_slots"]["aggregate"]["id"]
        del self.api.checks[aggregate]
        self.api.calls.clear()
        with self.assertRaises(FileNotFoundError):
            self.channel.read_slot(self.plan, "aggregate")
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.api.calls[0][1], f"/check-runs/{aggregate}")

    def test_wrong_app_commit_external_id_or_repository_is_rejected(self):
        self.ready()
        ident = self.plan["result_slots"]["aggregate"]["id"]
        original = copy.deepcopy(self.api.checks[ident])
        for patch in (
            {"app": {"id": 99}},
            {"head_sha": "0" * 40},
            {"external_id": "wrong"},
            {"url": "https://api.github.com/repos/other/repo/check-runs/1000"},
        ):
            self.api.checks[ident] = {**original, **patch}
            with self.subTest(patch=patch), self.assertRaises(ChannelError):
                self.channel.read_slot(self.plan, "aggregate")

    def test_receipt_and_case_round_trip_are_identity_bound(self):
        receipt = self.ready()
        self.assertIsNone(self.channel.read_receipt(self.plan))
        self.channel.publish_receipt(self.plan, receipt)
        self.assertEqual(self.channel.read_receipt(self.plan)["receipt"], receipt)
        result = self.result(receipt)
        self.channel.publish_case(self.plan, receipt, result)
        self.assertEqual(
            self.channel.read_case(self.plan, receipt, result["case_id"]), result
        )
        wrong = {**receipt, "pr_generation": 2}
        with self.assertRaisesRegex(ChannelError, "RECEIPT_BINDING"):
            self.channel.read_case(self.plan, wrong, result["case_id"])

    def test_terminal_result_cannot_be_changed_but_identical_retry_is_safe(self):
        receipt = self.ready()
        result = self.result(receipt)
        self.channel.publish_case(self.plan, receipt, result)
        before = sum(c[0] == "PATCH" for c in self.api.calls)
        self.channel.publish_case(self.plan, receipt, result)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.api.calls), before)
        result["status"] = "FAIL"
        with self.assertRaisesRegex(ChannelError, "TERMINAL_CHECK_RESULT_IMMUTABLE"):
            self.channel.publish_case(self.plan, receipt, result)

    def test_wrong_receipt_cannot_replace_admission(self):
        receipt = self.ready()
        self.channel.publish_receipt(self.plan, receipt)
        with self.assertRaisesRegex(ChannelError, "RECEIPT_BINDING"):
            self.channel.publish_receipt(
                self.plan, {**receipt, "execution_generation": 2}
            )

    def test_aggregate_waits_for_all_and_records_no_atomic_merge_guarantee(self):
        receipt = self.ready()
        self.channel.publish_receipt(self.plan, receipt)
        first = self.result(receipt)
        check = self.channel.publish_aggregate(
            self.plan, receipt, [first], current=True
        )
        self.assertEqual(check["status"], "in_progress")
        results = [first, self.result(receipt, 1)]
        for result in results:
            self.channel.publish_case(self.plan, receipt, result)
        check = self.channel.publish_aggregate(
            self.plan, receipt, results, current=True
        )
        self.assertEqual(check["conclusion"], "success")
        self.assertEqual(
            self.channel.read_receipt(self.plan)["outcome"]["merge_gate"],
            "NOT_PROVIDED",
        )

    def test_success_with_unclean_evidence_is_failure_not_green(self):
        receipt = self.ready()
        result = self.result(receipt)
        result["cleanup_verified"] = False
        check = self.channel.publish_case(self.plan, receipt, result)
        self.assertEqual(check["conclusion"], "failure")

    def test_duplicate_receipt_requires_canonical_pointer(self):
        receipt = self.ready()
        receipt["duplicate"] = True
        with self.assertRaisesRegex(ChannelError, "CANONICAL_RESULT_POINTER_REQUIRED"):
            self.channel.publish_receipt(self.plan, receipt)

    def test_fake_success_in_platform_conclusion_is_rejected(self):
        receipt = self.ready()
        result = self.result(receipt)
        result["status"] = "FAIL"
        check = self.channel.publish_case(self.plan, receipt, result)
        self.api.checks[check["id"]]["conclusion"] = "success"
        with self.assertRaisesRegex(ChannelError, "CONCLUSION_PAYLOAD_CONFLICT"):
            self.channel.read_case(self.plan, receipt, result["case_id"])


if __name__ == "__main__":
    unittest.main()
