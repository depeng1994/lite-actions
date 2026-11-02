"""Allowlisted cross-fork identity and actual local-Git merge tests (no platform/NPU)."""

import copy
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from ci_core.gitcode_api import GitCodeAPIError, GitCodeClient
from ci_core.gitcode_contract import ContractError, validate_pr
from ci_core.gitcode_prepare import prepare_pr_source
from ci_core.gitcode_source import (
    MergeSourceBuilder,
    SourceError,
    extract_verified_source,
)
from tests.test_gitcode_contract import fixture


class ForkAPI:
    target = "official/model"
    source = "personal/model"

    def __init__(self):
        self.ids = {self.target: "9028822", self.source: "9075802"}
        self.refs = {
            (self.target, "master"): "b" * 40,
            (self.source, "change"): "a" * 40,
        }
        self.pr = {
            "number": 7,
            "state": "open",
            "merged_at": None,
            "user": {"login": "owner"},
            "head": {
                "ref": "change",
                "sha": "a" * 40,
                "repo": {"full_name": self.source},
            },
            "base": {
                "ref": "master",
                "sha": "c" * 40,
                "repo": {"full_name": self.target},
            },
        }
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path == f"/repos/{self.target}/pulls/7":
            return copy.deepcopy(self.pr)
        for repo in (self.target, self.source):
            prefix = f"/repos/{repo}"
            if path == prefix:
                return {
                    "id": int(self.ids[repo]),
                    "full_name": repo,
                    "default_branch": "master",
                }
            if path.startswith(prefix + "/branches/"):
                ref = unquote(path[len(prefix + "/branches/") :])
                return {"name": ref, "commit": {"id": self.refs[(repo, ref)]}}
        raise AssertionError("unexpected API path")

    def client(self, **kwargs):
        return GitCodeClient(
            self.target,
            "9028822",
            transport=self,
            allowed_head_repositories=kwargs.pop("allowed", {self.source: "9075802"}),
            **kwargs,
        )


class CrossForkTests(unittest.TestCase):
    def setUp(self):
        self.api = ForkAPI()
        self.client = self.api.client()

    def snapshot(self):
        return self.client.open_pull_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("owner",)
        )

    def test_missing_embedded_ids_resolve_each_configured_repository(self):
        pr = self.snapshot()["identity"]
        self.assertEqual(
            (pr["repo_id"], pr["base_repo_id"], pr["head_repo_id"]),
            ("9028822", "9028822", "9075802"),
        )
        self.assertEqual(pr["base_sha"], "b" * 40)
        self.assertIn("/repos/personal/model/branches/change", self.api.calls)
        self.assertNotIn("/repos/official/model/branches/change", self.api.calls)
        self.assertEqual(self.api.calls.count("/repos/personal/model"), 2)

    def test_default_client_and_unlisted_source_fail_without_following_urls(self):
        self.client = self.api.client(allowed={})
        self.api.pr["head"]["repo"]["clone_url"] = "https://evil.invalid/steal"
        with self.assertRaisesRegex(GitCodeAPIError, "EXTERNAL_FORK"):
            self.snapshot()
        self.assertFalse(
            any("personal" in path or "evil" in path for path in self.api.calls)
        )

    def test_source_numeric_id_reassignment_or_embedded_conflict_rejected(self):
        self.api.ids[self.api.source] = "999"
        with self.assertRaisesRegex(GitCodeAPIError, "IDENTITY_CHANGED"):
            self.snapshot()
        self.api.ids[self.api.source] = "9075802"
        self.api.pr["head"]["repo"]["id"] = 999
        with self.assertRaisesRegex(GitCodeAPIError, "IDENTITY_CHANGED"):
            self.snapshot()

    def test_target_reassignment_is_not_crossfork_permission(self):
        self.api.pr["base"]["repo"]["full_name"] = self.api.source
        with self.assertRaisesRegex(GitCodeAPIError, "TARGET_REPOSITORY"):
            self.snapshot()

    def test_crossfork_authority_preserves_source_for_retarged_revocation(self):
        self.api.pr["base"]["ref"] = "release"
        self.api.refs[(self.api.target, "release")] = "d" * 40
        snapshot = self.client.authority_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("owner",)
        )
        self.assertFalse(snapshot["authorized"])
        self.assertEqual(snapshot["identity"]["head_repo_id"], "9075802")

    def test_contract_is_strict_by_default_and_explicit_allowlist_only(self):
        pr = self.snapshot()["identity"]
        with self.assertRaisesRegex(ContractError, "EXTERNAL_FORK"):
            validate_pr(pr)
        validate_pr(pr, allowed_repo_id="9028822", allowed_head_repo_ids=("9075802",))
        with self.assertRaisesRegex(ContractError, "EXTERNAL_FORK"):
            validate_pr(pr, allowed_head_repo_ids=("999",))
        with self.assertRaisesRegex(ContractError, "TARGET_REPOSITORY"):
            validate_pr({**pr, "base_repo_id": "9075802"}, allowed_head_repo_ids=None)
        _, ctx = fixture()
        policy = replace(
            ctx["policy"], allowed_head_repositories=(("personal/model", "9075802"),)
        )
        self.assertEqual(policy.allowed_head_repositories[0][1], "9075802")
        for pairs in ((("evil/repo?token=x", "9075802"),), (("personal/model", "0"),)):
            with self.assertRaisesRegex(ContractError, "HEAD_REPOSITORY"):
                replace(policy, allowed_head_repositories=pairs)

    def test_two_distinct_git_repositories_merge_true_current_h_and_b(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target, source = root / "target", root / "source"

            def git(repo, *args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()

            target.mkdir()
            git(target, "init", "--initial-branch=master")
            git(target, "config", "user.name", "Fixture")
            git(target, "config", "user.email", "fixture@example.invalid")
            (target / "common.txt").write_text("ancestor\n")
            git(target, "add", ".")
            git(target, "commit", "-m", "ancestor")
            subprocess.run(
                ["git", "clone", str(target), str(source)],
                check=True,
                capture_output=True,
            )
            git(source, "config", "user.name", "Fixture")
            git(source, "config", "user.email", "fixture@example.invalid")
            git(source, "checkout", "-b", "change")
            (source / "head-only.txt").write_text("fork head\n")
            git(source, "add", ".")
            git(source, "commit", "-m", "head")
            (target / "base-only.txt").write_text("advanced target\n")
            git(target, "add", ".")
            git(target, "commit", "-m", "base")
            head, base = (
                git(source, "rev-parse", "HEAD"),
                git(target, "rev-parse", "HEAD"),
            )
            self.api.pr["head"]["sha"] = head
            self.api.refs[(self.api.source, "change")] = head
            self.api.refs[(self.api.target, "master")] = base
            paths = {
                "https://gitcode.com/official/model.git": target,
                "https://gitcode.com/personal/model.git": source,
            }
            fetched = []

            class LocalObjectBuilder(MergeSourceBuilder):
                def fetch_ref(self, *, url, ref, expected_sha, role):
                    # Test-only local transport; production remains HTTPS-only.
                    self._check_url(url)
                    if url not in self.allowed_urls or url not in paths:
                        raise SourceError("UNTRUSTED_GIT_REMOTE")
                    fetched.append((role, url))
                    subprocess.run(
                        [
                            "git",
                            "--git-dir=" + str(self.repo),
                            "fetch",
                            "--no-tags",
                            str(paths[url]),
                            "+refs/heads/" + ref + ":refs/heads/input-" + role,
                        ],
                        check=True,
                        capture_output=True,
                    )
                    actual = (
                        self._git("rev-parse", "refs/heads/input-" + role)
                        .decode()
                        .strip()
                    )
                    if actual != expected_sha:
                        raise SourceError("FETCHED_REF_SHA_MISMATCH")

            version = (
                subprocess.check_output(["git", "--version"], text=True)
                .strip()
                .removeprefix("git version ")
            )
            handle, identity = prepare_pr_source(
                self.client,
                7,
                root / "build",
                allowed_base_refs=("master",),
                allowed_actors=("owner",),
                expected_git_version=version,
                builder_factory=LocalObjectBuilder,
            )
            self.assertEqual(identity["head_sha"], head)
            self.assertEqual(identity["base_sha"], base)
            self.assertEqual(
                fetched,
                [
                    ("base", "https://gitcode.com/official/model.git"),
                    ("head", "https://gitcode.com/personal/model.git"),
                ],
            )
            extract_verified_source(handle, root / "out")
            self.assertEqual(
                (root / "out" / "base-only.txt").read_text(), "advanced target\n"
            )
            self.assertEqual(
                (root / "out" / "head-only.txt").read_text(), "fork head\n"
            )


if __name__ == "__main__":
    unittest.main()
