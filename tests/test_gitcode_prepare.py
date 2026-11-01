"""Preparation orchestration and release publication gates."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_prepare import (
    PreparationError,
    prepare_pr_source,
    verify_control_release,
)
from tests.test_gitcode_contract import fixture


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.plan, ctx = fixture()
        self.policy = ctx["policy"]
        self.observations = {
            "upstream": {"repo_id": "43", "default_ref": "main", "sha": "c" * 40},
            "repository": {
                "id": 1438625957,
                "full_name": "cann-framework/vllm-ascend",
                "default_branch": "main",
            },
            "branch": {"name": "main", "commit": {"sha": "c" * 40}},
            "workflow": {
                "id": 91,
                "path": self.policy.workflow_path,
                "state": "active",
            },
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Mock(repo_id="42")
        self.client.repository.return_value = {
            "repo_id": "42",
            "repo": "owner/model",
            "default_ref": "main",
            "clone_url": "https://gitcode.com/owner/model.git",
        }
        self.current = {
            "identity": copy.deepcopy(self.plan["pr"]),
            "state": "open",
            "authorized": True,
        }
        self.client.open_pull_snapshot.return_value = self.current
        self.builder = Mock(root=self.root)
        self.factory = Mock(return_value=self.builder)

    def prepare(self, **kwargs):
        return prepare_pr_source(
            self.client,
            1,
            self.root / "build",
            allowed_base_refs=("main",),
            allowed_actors=("depeng1994",),
            expected_git_version="2.43.0",
            builder_factory=self.factory,
            **kwargs,
        )

    def test_exact_upstream_and_github_default_control_is_required(self):
        verify_control_release(self.policy, **self.observations)
        for group, key, value in (
            ("upstream", "sha", "0" * 40),
            ("repository", "id", 1),
            ("repository", "default_branch", "ci"),
            ("branch", "name", "ci"),
            ("workflow", "state", "disabled_manually"),
        ):
            obs = copy.deepcopy(self.observations)
            obs[group][key] = value
            with (
                self.subTest(group=group, key=key),
                self.assertRaisesRegex(PreparationError, "NOT_PUBLISHED"),
            ):
                verify_control_release(self.policy, **obs)

    def test_current_source_is_fetched_by_exact_refs_and_shas(self):
        handle, pr = self.prepare()
        self.assertEqual(pr, self.plan["pr"])
        self.assertEqual(handle, self.builder.build.return_value)
        self.assertEqual(self.builder.fetch_ref.call_count, 2)
        self.assertEqual(self.client.open_pull_snapshot.call_count, 2)
        self.assertEqual(
            self.factory.call_args.kwargs["allowed_urls"],
            ("https://gitcode.com/owner/model.git",),
        )
        self.builder.build.return_value.verify.assert_called_once()

    def test_stale_dispatch_is_rejected_before_fetch(self):
        old = {**self.plan["pr"], "base_sha": "0" * 40}
        with self.assertRaisesRegex(PreparationError, "NO_LONGER_CURRENT"):
            self.prepare(expected_identity=old)
        self.factory.assert_not_called()

    def test_concurrent_head_or_base_change_removes_unpublished_bundle(self):
        for field in ("head_sha", "base_sha", "base_ref"):
            before = copy.deepcopy(self.current)
            after = copy.deepcopy(before)
            after["identity"][field] = "release" if field == "base_ref" else "0" * 40
            self.client.open_pull_snapshot.side_effect = [before, after]
            (self.root / "source.tar.gz").write_bytes(b"must not be published")
            with self.assertRaisesRegex(PreparationError, "CHANGED_DURING_PREPARATION"):
                self.prepare()
            self.assertFalse((self.root / "source.tar.gz").exists())

    def test_final_authority_failure_removes_unpublished_bundle(self):
        self.client.open_pull_snapshot.side_effect = [self.current, OSError("offline")]
        (self.root / "source.tar.gz").write_bytes(b"not current")
        with self.assertRaises(OSError):
            self.prepare()
        self.assertFalse((self.root / "source.tar.gz").exists())

    def test_fetch_mismatch_never_calls_builder(self):
        self.builder.fetch_ref.side_effect = ValueError("FETCHED_REF_SHA_MISMATCH")
        with self.assertRaises(ValueError):
            self.prepare()
        self.builder.build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
