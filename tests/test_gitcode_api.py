"""Fixtures mirror observed GitCode v5 fields, including absent embedded IDs."""

import copy
import unittest
from urllib.parse import unquote

from ci_core.gitcode_api import GitCodeAPIError, GitCodeClient, repository_id


class FixtureTransport:
    def __init__(self):
        self.repo = "depeng1994/torchtitan-npu"
        self.metadata = {
            "id": 9075802,
            "full_name": self.repo,
            "default_branch": "master",
        }
        self.pr = {
            "number": 7,
            "state": "open",
            "merged_at": None,
            "user": {"login": "depeng1994"},
            "head": {
                "ref": "feature/change",
                "sha": "a" * 40,
                "repo": {"full_name": self.repo},
            },
            "base": {
                "ref": "master",
                "sha": "c" * 40,
                "repo": {"full_name": self.repo},
            },
        }
        self.branches = {"feature/change": "a" * 40, "master": "b" * 40}
        self.calls, self.pr_reads = [], 0
        self.change_second_read = False

    def __call__(self, path):
        self.calls.append(path)
        if path == "/repos/" + self.repo:
            return copy.deepcopy(self.metadata)
        if path.endswith("/pulls/7"):
            self.pr_reads += 1
            result = copy.deepcopy(self.pr)
            if self.change_second_read and self.pr_reads == 2:
                result["head"]["sha"] = "d" * 40
            return result
        if "/branches/" in path:
            ref = unquote(path.split("/branches/", 1)[1])
            return {"name": ref, "commit": {"id": self.branches[ref]}}
        raise AssertionError("unexpected path: " + path)


class GitCodeAPITests(unittest.TestCase):
    def setUp(self):
        self.transport = FixtureTransport()
        self.client = GitCodeClient(
            self.transport.repo, "9075802", transport=self.transport
        )

    def snapshot(self):
        return self.client.open_pull_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
        )

    def test_missing_embedded_repo_ids_are_independently_resolved(self):
        result = self.snapshot()
        self.assertEqual(result["identity"]["repo_id"], "9075802")
        self.assertEqual(result["identity"]["head_repo_id"], "9075802")
        self.assertEqual(result["identity"]["base_repo_id"], "9075802")
        self.assertEqual(self.transport.calls.count("/repos/" + self.transport.repo), 2)

    def test_cached_pr_base_sha_is_never_used_as_current_base(self):
        result = self.snapshot()
        self.assertEqual(result["identity"]["base_sha"], "b" * 40)
        self.assertNotEqual(
            result["identity"]["base_sha"], self.transport.pr["base"]["sha"]
        )
        self.assertIn(
            "/repos/" + self.transport.repo + "/branches/feature%2Fchange",
            self.transport.calls,
        )

    def test_repository_path_cannot_be_reassigned_to_another_numeric_id(self):
        self.transport.metadata["id"] = 999
        with self.assertRaisesRegex(GitCodeAPIError, "IDENTITY_CHANGED"):
            self.snapshot()

    def test_embedded_id_if_present_must_match_independent_lookup(self):
        self.transport.pr["head"]["repo"]["id"] = 999
        with self.assertRaisesRegex(GitCodeAPIError, "IDENTITY_CHANGED"):
            self.snapshot()

    def test_external_fork_is_rejected_without_following_its_urls(self):
        self.transport.pr["head"]["repo"]["full_name"] = "external/repo"
        self.transport.pr["head"]["repo"]["html_url"] = "https://evil.invalid/steal"
        with self.assertRaisesRegex(GitCodeAPIError, "EXTERNAL_FORK"):
            self.snapshot()
        self.assertTrue(
            all(
                "external" not in path and "evil" not in path
                for path in self.transport.calls
            )
        )

    def test_head_change_between_reads_and_branch_pr_lag_are_rejected(self):
        self.transport.change_second_read = True
        with self.assertRaisesRegex(GitCodeAPIError, "CHANGED_DURING_READ"):
            self.snapshot()
        self.transport.change_second_read = False
        self.transport.branches["feature/change"] = "e" * 40
        with self.assertRaisesRegex(GitCodeAPIError, "CHANGED_DURING_READ"):
            self.snapshot()

    def test_closed_merged_wrong_actor_and_retarget_are_not_admitted(self):
        for field, value in (
            ("state", "closed"),
            ("merged_at", "2026-11-01T00:00:00Z"),
        ):
            before = copy.deepcopy(self.transport.pr)
            self.transport.pr[field] = value
            with self.assertRaisesRegex(GitCodeAPIError, "PR_NOT_OPEN"):
                self.snapshot()
            self.transport.pr = before
        self.transport.pr["user"]["login"] = "another"
        with self.assertRaisesRegex(GitCodeAPIError, "ACTOR_NOT_AUTHORIZED"):
            self.snapshot()
        self.transport.pr["user"]["login"] = "depeng1994"
        self.transport.pr["base"]["ref"] = "release"
        with self.assertRaisesRegex(GitCodeAPIError, "BASE_REF_NOT_AUTHORIZED"):
            self.snapshot()

    def test_control_tip_reads_actual_default_ref(self):
        tip = self.client.control_tip()
        self.assertEqual(tip["default_ref"], "master")
        self.assertEqual(tip["sha"], "b" * 40)

    def test_invalid_id_and_repository_path(self):
        for value in (True, None, 0, "0", "../1", "1.2"):
            with (
                self.subTest(value=value),
                self.assertRaises((GitCodeAPIError, ValueError)),
            ):
                repository_id(value)
        for repo in (
            "../repo",
            "owner/repo?token=x",
            "https://gitcode.com/o/r",
            "o/r/extra",
        ):
            with self.assertRaises(GitCodeAPIError):
                GitCodeClient(repo, "9075802", transport=self.transport)

    def test_discovery_paginates_and_never_uses_cached_list_identity(self):
        calls = []

        def transport(path):
            calls.append(path)
            if "/pulls?" not in path:
                return self.transport(path)
            page = int(path.rsplit("page=", 1)[1])
            if page == 1:
                return [{"number": i, "state": "open"} for i in range(1, 101)]
            return [{"number": 101, "state": "open", "base": {"sha": "stale"}}]

        self.client._transport = transport
        self.assertEqual(self.client.open_pull_numbers(), list(range(1, 102)))
        self.assertEqual(sum("/pulls?" in path for path in calls), 2)

    def test_discovery_incomplete_and_duplicate_pages_fail_closed(self):
        def transport(path):
            if "/pulls?" not in path:
                return self.transport(path)
            return [{"number": i, "state": "open"} for i in range(1, 101)]

        self.client._transport = transport
        with self.assertRaisesRegex(GitCodeAPIError, "DISCOVERY_INCOMPLETE"):
            self.client.open_pull_numbers(max_pages=1)
        with self.assertRaisesRegex(GitCodeAPIError, "CHANGED_DURING_SCAN"):
            self.client.open_pull_numbers(max_pages=2)

    def test_discovery_rejects_object_response_and_non_open_row(self):
        for value in ({"number": 1}, [{"number": 1, "state": "closed"}]):
            self.client._transport = lambda path: (
                value if "/pulls?" in path else self.transport(path)
            )
            with self.assertRaises(GitCodeAPIError):
                self.client.open_pull_numbers()

    def test_authority_observes_closed_and_retarget_without_grant(self):
        self.transport.pr["state"] = "closed"
        observation = self.client.authority_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
        )
        self.assertEqual(observation["state"], "closed")
        self.transport.pr["state"] = "open"
        self.transport.pr["base"]["ref"] = "release"
        self.transport.branches["release"] = "d" * 40
        observation = self.client.authority_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
        )
        self.assertFalse(observation["authorized"])
        self.assertEqual(observation["identity"]["base_ref"], "release")

    def test_action_metadata_preserves_actual_uppercase_event_and_missing_proofs(self):
        run = {
            "workflow_run_id": "run-1",
            "workflow_id": "workflow-1",
            "file_path": ".gitcode/workflows/bridge.yml",
            "event": "PUSH",
            "status": "FAILED",
            "attempt": 1,
            "head_sha": "a" * 40,
            "head_branch": "master",
            "start_time": 1787052415000,
            "exist_in_default_branch": True,
            "stages": [{"untrusted": "omitted"}],
        }

        def transport(path):
            if path == "/api/v8/repos/" + self.transport.repo + "/actions/runs/run-1":
                return copy.deepcopy(run)
            return self.transport(path)

        self.client._transport = transport
        observation = self.client.action_run("run-1")
        self.assertEqual(observation["event"], "PUSH")
        self.assertNotIn("actor", observation)
        self.assertNotIn("pull_request", observation)
        self.assertNotIn("stages", observation)
        run["workflow_run_id"] = "another"
        with self.assertRaisesRegex(GitCodeAPIError, "IDENTITY_MISMATCH"):
            self.client.action_run("run-1")

    def test_token_is_not_exposed_in_repr(self):
        client = GitCodeClient(self.transport.repo, "9075802", token="never-print-this")
        self.assertNotIn("never-print-this", repr(client))
        self.assertNotIn("never-print-this", str(client))

    def test_no_revision_is_fabricated_from_updated_at(self):
        self.transport.pr["updated_at"] = "2026-11-01T00:00:00Z"
        result = self.snapshot()
        self.assertNotIn("revision", result)
        self.assertNotIn("event_order", result)


if __name__ == "__main__":
    unittest.main()
