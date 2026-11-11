"""GitHub manual admission follows repository permissions and the fixed branch commit."""

import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, VLLM_REPO
from ci_core.router import Router


class PermissionClient:
    def __init__(self, repo):
        self.repo = repo
        self.role = "write"
        self.roles = {}
        self.current = "a" * 40
        self.fail = False

    def request(self, method, path):
        if self.fail:
            raise OSError("GitHub unavailable")
        if path.startswith("/collaborators/"):
            actor = path.split("/")[2]
            return {"permission": self.roles.get(actor, self.role)}
        if path.startswith("/branches/"):
            return {"commit": {"sha": self.current}}
        if path == "":
            return {"default_branch": "main"}
        raise AssertionError(path)


class GitHubManualPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.router = Router.__new__(Router)
        self.router.client = PermissionClient(VLLM_REPO)
        self.run = {
            "_ci_repo": VLLM_REPO,
            "path": ".github/workflows/lite_actions_dispatcher_smoke.yml",
            "event": "workflow_dispatch",
            "status": "in_progress",
            "head_branch": "features/smoke",
            "head_sha": "a" * 40,
            "actor": {"login": "contributor"},
        }

    def test_repository_writers_can_use_any_current_branch(self):
        for role in ("write", "maintain", "admin"):
            with self.subTest(role=role):
                self.router.client.role = role
                self.assertTrue(self.router.eligible(self.run))
        for role in ("read", "triage", "none"):
            with self.subTest(role=role):
                self.router.client.role = role
                self.assertFalse(self.router.eligible(self.run))
        self.router.client.role = "write"
        self.router.client.current = "b" * 40
        self.assertFalse(self.router.eligible(self.run))
        self.router.client.fail = True
        self.assertFalse(self.router.eligible(self.run))

    def test_rerun_checks_triggering_actor_permission(self):
        self.router.client.roles = {"contributor": "admin", "reader": "read"}
        rerun = {**self.run, "triggering_actor": {"login": "reader"}}
        self.assertFalse(self.router.eligible(rerun))
        self.assertTrue(
            self.router.eligible(
                {**rerun, "triggering_actor": {"login": "contributor"}}
            )
        )

    def test_authorized_person_can_recheck_from_their_branch(self):
        recheck = {**self.run, "_ci_pr_number": 4, "_ci_control_sha": "a" * 40}
        self.assertTrue(self.router.eligible(recheck))
        self.router.client.role = "read"
        self.assertFalse(self.router.eligible(recheck))

    def test_prefix_admission_is_independent_of_workflow_filename_or_run_title(self):
        run = {
            **self.run,
            "name": "lite-ci-any-check",
            "path": ".github/workflows/anything.yml",
            "display_title": "a human friendly title",
        }
        self.assertTrue(self.router.eligible(run))
        self.router.client.role = "read"
        self.assertFalse(self.router.eligible(run))
        self.router.client.role = "write"
        self.assertFalse(self.router.eligible({**run, "name": "other-check"}))

    def test_reconciler_bot_is_confined_to_default_branch(self):
        bot = {
            **self.run,
            "actor": {"login": "github-actions[bot]"},
            "head_branch": "main",
            "_ci_pr_number": 4,
            "_ci_control_sha": "c" * 40,
        }
        self.assertTrue(self.router.eligible(bot))
        self.assertFalse(self.router.eligible({**bot, "head_branch": "other"}))
        self.assertFalse(self.router.eligible({**bot, "_ci_pr_number": None}))

    def test_self_smoke_also_requires_checkout_identity(self):
        self.router.client = PermissionClient(SELF_REPO)
        self.router.root = Path("/fixture")
        self.router.self_sha = "a" * 40
        run = {
            **self.run,
            "_ci_repo": SELF_REPO,
            "path": ".github/workflows/" + SELF_WORKFLOW,
        }
        with patch("ci_core.self_identity.checkout_sha", return_value="a" * 40):
            self.assertTrue(self.router.eligible(run))
            self.router.client.role = "read"
            self.assertFalse(self.router.eligible(run))
            self.router.client.role = "admin"
            self.assertFalse(self.router.eligible({**run, "head_sha": "b" * 40}))


if __name__ == "__main__":
    unittest.main()
