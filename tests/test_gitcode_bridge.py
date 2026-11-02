"""Bridge transport fault injection; no real workflow is dispatched."""

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_bridge import BridgeError, DispatchJournal, GitCodeBridge
from ci_core.gitcode_prepare import PreparationError
from tests.test_gitcode_contract import fixture


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.plan, ctx = fixture()
        self.policy = ctx["policy"]
        self.intent = copy.deepcopy(ctx["intent"])
        self.intent["bridge"]["bridge_nonce"] = "fixed-nonce-0123456789"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dispatch.sqlite3"
        self.github = Mock(repo="cann-framework/vllm-ascend", repo_id=1438625957)
        self.personal = Mock(repo_id="42")
        self.personal.open_pull_snapshot.return_value = {
            "identity": self.intent["pr"],
            "state": "open",
            "authorized": True,
        }
        self.upstream = Mock(repo_id="43")
        self.upstream.control_tip.return_value = {
            "repo_id": "43",
            "default_ref": "main",
            "sha": "c" * 40,
        }
        self.run = {
            "id": 999,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "repository": {"id": 1438625957, "full_name": "cann-framework/vllm-ascend"},
            "head_sha": "c" * 40,
            "head_branch": "main",
            "workflow_id": 91,
            "path": self.policy.workflow_path,
            "display_title": GitCodeBridge.run_name(self.intent),
            "actor": {"login": "depeng1994"},
        }
        self.posts = 0
        self.lost = self.invisible = self.ambiguous = self.incomplete = False
        self.github.request.side_effect = self.request
        self.bridge = self.new_bridge()

    def new_bridge(self):
        return GitCodeBridge(
            policy=self.policy,
            github=self.github,
            personal=self.personal,
            upstream=self.upstream,
            journal=DispatchJournal(self.path),
            catalog=fixture()[1]["catalog"],
            github_actor="depeng1994",
        )

    def request(self, method, path, payload=None):
        if path == "":
            return {
                "id": 1438625957,
                "full_name": self.github.repo,
                "default_branch": "main",
            }
        if path == "/branches/main":
            return {"name": "main", "commit": {"sha": "c" * 40}}
        if path == "/actions/workflows/91":
            return {"id": 91, "path": self.policy.workflow_path, "state": "active"}
        if method == "POST":
            self.posts += 1
            self.assertEqual(payload["ref"], "main")
            self.assertTrue(payload["return_run_details"])
            if self.lost:
                raise TimeoutError("response lost")
            return {
                "workflow_run_id": 999,
                "run_url": "https://api.github.com/repos/cann-framework/vllm-ascend/actions/runs/999",
                "html_url": "https://github.com/cann-framework/vllm-ascend/actions/runs/999",
            }
        if "/runs?" in path:
            runs = [] if self.invisible else [self.run]
            if self.ambiguous:
                runs.append({**self.run, "id": 1000})
            return {
                "total_count": 101 if self.incomplete else len(runs),
                "workflow_runs": runs,
            }
        if path == "/actions/runs/999":
            return self.run
        raise AssertionError((method, path))

    def test_signed_envelope_is_transported_unchanged_not_treated_as_authorization(
        self,
    ):
        envelope = {"protocol": "fixture-not-real-signature", "payload": {"n": 1}}
        self.bridge.dispatch(self.intent, now=1100, attestation=envelope)
        posts = [
            call
            for call in self.github.request.call_args_list
            if call.args[0] == "POST"
        ]
        import json

        self.assertEqual(
            json.loads(posts[0].args[2]["inputs"]["gitcode_attestation"]), envelope
        )

    def test_returned_run_id_and_restart_do_not_dispatch_twice(self):
        result = self.bridge.dispatch(self.intent, now=1100)
        self.assertEqual(result["run_id"], 999)
        self.assertEqual(result["merge_gate"], "NOT_PROVIDED")
        self.assertEqual(self.new_bridge().dispatch(self.intent, now=1101), result)
        self.assertEqual(self.posts, 1)

    def test_lost_response_recovers_exact_nonce_without_retrying_post(self):
        self.lost = True
        self.assertEqual(self.bridge.dispatch(self.intent, now=1100)["run_id"], 999)
        self.assertEqual(self.posts, 1)

    def test_unknown_dispatch_survives_restart_without_second_post(self):
        self.lost = self.invisible = True
        with self.assertRaisesRegex(BridgeError, "UNKNOWN_OR_AMBIGUOUS"):
            self.bridge.dispatch(self.intent, now=1100)
        with self.assertRaisesRegex(BridgeError, "UNKNOWN_OR_AMBIGUOUS"):
            self.new_bridge().dispatch(self.intent, now=1101)
        self.assertEqual(self.posts, 1)
        self.invisible = False
        self.assertEqual(
            self.new_bridge().dispatch(self.intent, now=1102)["run_id"], 999
        )
        self.assertEqual(self.posts, 1)

    def test_no_latest_run_or_truncated_scan_fallback(self):
        self.lost = self.ambiguous = True
        with self.assertRaisesRegex(BridgeError, "UNKNOWN_OR_AMBIGUOUS"):
            self.bridge.dispatch(self.intent, now=1100)
        self.ambiguous, self.incomplete = False, True
        with self.assertRaisesRegex(BridgeError, "SCOPE_INCOMPLETE"):
            self.new_bridge().dispatch(self.intent, now=1101)
        self.assertEqual(self.posts, 1)

    def test_same_intent_cannot_change_selection_or_rerun_identity(self):
        self.bridge.dispatch(self.intent, now=1100)
        altered = copy.deepcopy(self.intent)
        altered["selectors"] = [{"test_id": "dispatch_smoke_a3_16p"}]
        with self.assertRaisesRegex(BridgeError, "INTENT_IMMUTABLE"):
            self.bridge.dispatch(altered, now=1101)
        self.assertEqual(self.posts, 1)

    def test_unpublished_control_has_zero_dispatches(self):
        self.upstream.control_tip.return_value["sha"] = "0" * 40
        with self.assertRaisesRegex(PreparationError, "NOT_PUBLISHED"):
            self.bridge.dispatch(self.intent, now=1100)
        self.assertEqual(self.posts, 0)

    def test_stale_pr_has_zero_dispatches(self):
        self.personal.open_pull_snapshot.return_value = {
            "identity": {**self.intent["pr"], "base_sha": "0" * 40},
            "state": "open",
            "authorized": True,
        }
        with self.assertRaisesRegex(BridgeError, "PR_VERSION_STALE"):
            self.bridge.dispatch(self.intent, now=1100)
        self.assertEqual(self.posts, 0)

    def test_disabled_provider_does_not_even_query_control(self):
        self.policy = replace(self.policy, enabled=False)
        with self.assertRaisesRegex(BridgeError, "PROVIDER_DISABLED"):
            self.new_bridge().dispatch(self.intent, now=1100)
        self.github.request.assert_not_called()

    def test_wrong_run_commit_actor_repository_or_attempt_is_rejected(self):
        for patch in (
            {"head_sha": "0" * 40},
            {"run_attempt": 2},
            {"repository": {"id": 1}},
            {"actor": {"login": "intruder"}},
            {"display_title": "newest run"},
        ):
            original = self.run
            self.run = {**original, **patch}
            with (
                self.subTest(patch=patch),
                self.assertRaisesRegex(BridgeError, "RUN_IDENTITY_MISMATCH"),
            ):
                self.bridge.dispatch(self.intent, now=1100)
            self.run = original
        self.assertEqual(self.posts, 1)


if __name__ == "__main__":
    unittest.main()
