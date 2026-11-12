"""Discovery depends on workflow name, not GitHub event or actor."""

import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, VLLM_REPO
from ci_core.github import GitHubClient, MultiRepoGitHubClient
from ci_core.router import Router


class GitHubDiscoveryTests(unittest.TestCase):
    def test_api_discovers_prefix_without_event_filter(self):
        client = GitHubClient("fixture", VLLM_REPO)
        calls = []

        def request(method, path):
            calls.append(path)
            if path == "/actions/workflows/50":
                return {"name": "lite-ci-workflow"}
            status = "queued" if "status=queued" in path else "in_progress"
            return {
                "workflow_runs": [
                    {
                        "id": 1,
                        "run_attempt": 1,
                        "name": "lite-ci-check",
                        "event": "schedule",
                        "status": status,
                    },
                    {
                        "id": 2,
                        "run_attempt": 1,
                        "name": "other",
                        "event": "issue_comment",
                        "status": status,
                    },
                    {
                        "id": 3,
                        "run_attempt": 1,
                        "name": "custom run title",
                        "workflow_id": 50,
                        "event": "issue_comment",
                        "status": status,
                    },
                ]
            }

        with patch.object(client, "request", side_effect=request):
            runs = client.running_workflows()
        self.assertEqual([run["id"] for run in runs], [1, 3])
        self.assertEqual(runs[1]["name"], "lite-ci-workflow")
        self.assertTrue(all("event=" not in path for path in calls))

    def test_implicit_self_discovery_does_not_duplicate_or_retry_every_tick(self):
        github = MultiRepoGitHubClient(
            "fixture", (VLLM_REPO,), implicit_self_smoke=True
        )
        model, own = github.for_repo(VLLM_REPO), github.for_repo(SELF_REPO)
        # On an API failure keep polling the model; do not hammer the self repo.
        with (
            patch.object(
                model, "running_workflows", return_value=[{"id": 1}]
            ) as models,
            patch.object(
                own, "request", side_effect=[OSError("offline"), {"workflow_runs": []}]
            ) as reads,
            patch("ci_core.github.time.monotonic", side_effect=[100, 101, 159, 160]),
        ):
            for _ in range(4):
                self.assertEqual(github.running_workflows(), [{"id": 1}])
            self.assertEqual(reads.call_count, 2)
            self.assertEqual(models.call_count, 4)

    def test_prefix_accepts_all_trigger_types(self):
        router = Router.__new__(Router)
        run = {
            "_ci_repo": VLLM_REPO,
            "name": "lite-ci-any-check",
            "path": ".github/workflows/any.yml",
            "status": "in_progress",
        }
        for event in (
            "workflow_dispatch",
            "schedule",
            "issue_comment",
            "pull_request",
            "push",
        ):
            with self.subTest(event=event):
                self.assertTrue(
                    router.eligible(
                        {**run, "event": event, "actor": {"login": "anyone"}}
                    )
                )
        self.assertFalse(router.eligible({**run, "name": "unrelated"}))
        self.assertFalse(router.eligible({**run, "status": "completed"}))

    def test_self_smoke_still_binds_the_deployed_checkout(self):
        router = Router.__new__(Router)
        router.root, router.self_sha = Path("/fixture"), "a" * 40
        run = {
            "_ci_repo": SELF_REPO,
            "name": "lite-ci-self-smoke",
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "status": "in_progress",
            "head_sha": "a" * 40,
            "event": "schedule",
        }
        with patch("ci_core.self_identity.checkout_sha", return_value="a" * 40):
            self.assertTrue(router.eligible(run))
            self.assertFalse(router.eligible({**run, "head_sha": "b" * 40}))
