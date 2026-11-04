"""Exercise a GitCode request through the shared router without real NPUs."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.broadcast import notification
from ci_core.contract import digest, normalize
from ci_core.gitcode import GitCodeClient, MultiPlatformClient
from ci_core.router import Router, task_key
from tests.scenarios import Service, definition, write_workspace


class GitCodeWorkflowTests(unittest.TestCase):
    def test_gitcode_hardware_notification_uses_opaque_run_identity(self):
        run = {
            "_ci_provider": "gitcode",
            "_ci_repo": "depeng1994/torchtitan-npu",
            "id": "abc123",
        }
        message = notification(
            run,
            "begin",
            node_rank=0,
            node_ip="192.0.2.1",
            devices=list(range(8)),
            nnodes=1,
            ngpus_per_node=8,
        )
        self.assertIn(
            "https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/abc123", message
        )

    def test_gitcode_and_github_probes_report_to_their_own_repositories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_workspace(root)
            github = Service([definition("github_os", kind="worker_probe")], root)
            gitcode = GitCodeClient("unused-token", "depeng1994/torchtitan-npu")
            case = normalize(
                definition("gitcode_os", kind="worker_probe"),
                {"worker": "blue", "target_pool": "pool_blue", "docker": "training"},
                0,
            )
            run = {
                "_ci_provider": "gitcode",
                "_ci_repo": gitcode.repo,
                "id": "abcdef0123",
                "run_attempt": 1,
                "head_sha": "b" * 40,
                "path": ".gitcode/workflows/lite-actions.yml",
                "event": "workflow_dispatch",
                "status": "in_progress",
                "head_branch": "master",
                "actor": {"login": "maintainer"},
                "created_at": "2026-01-01T00:00:00Z",
                "name": "Dispatcher-Smoke",
                "run_number": 1,
            }
            plan = {
                "schema": 3,
                "repo": gitcode.repo,
                "workflow": "lite-actions.yml",
                "run_id": run["id"],
                "attempt": 1,
                "sha": run["head_sha"],
                "defaults": {
                    "worker": "blue",
                    "target_pool": "pool_blue",
                    "docker": "training",
                },
                "cases": [case],
            }
            plan["plan_digest"] = digest(plan)
            comments = []

            def send(sha, body):
                entry = {
                    "id": len(comments) + 1,
                    "body": body,
                    "user": {"login": "reporter"},
                }
                comments.append(entry)
                return entry

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer",
                        "LITE_ACTIONS_GITCODE_ALLOWED_ACTORS": "maintainer",
                        "LITE_ACTIONS_STATE_DIR": "",
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "locks"),
                    },
                ),
                github.connected(),
                patch.object(gitcode, "running_workflows", return_value=[run]),
                patch.object(gitcode, "ci_request", return_value=plan),
                patch.object(gitcode, "validate_current"),
                patch.object(
                    gitcode,
                    "workflow_status",
                    return_value={"status": "in_progress", "run_attempt": 1},
                ),
                patch.object(
                    gitcode, "commit_comments", side_effect=lambda sha: comments
                ),
                patch.object(gitcode, "send_result", side_effect=send),
                patch.object(gitcode, "request", return_value=None),
                patch("ci_core.router.verify_definition"),
                patch(
                    "ci_core.router.probe_worker",
                    return_value=subprocess.CompletedProcess(
                        [], 0, "OS probe passed\n", ""
                    ),
                ),
            ):
                client = MultiPlatformClient(github, {gitcode.repo: gitcode})
                router = Router(root, "blue", client, "reporter")
                try:
                    router.tick()
                    for _ in range(10):
                        for future, _, _ in list(router.active.values()):
                            future.result(timeout=10)
                        router.pump()
                        router.flush()
                        if not router.active:
                            break
                    self.assertEqual(
                        router.state.read()[task_key(run, case)]["phase"], "REPORTED"
                    )
                    gitcode_results = [
                        json.loads(x["body"].split("\n", 1)[1])
                        for x in comments
                        if x["body"].startswith("Lite-CI-V3 ")
                    ]
                    self.assertEqual(
                        [(x["test_id"], x["status"]) for x in gitcode_results],
                        [("gitcode_os", "PASS")],
                    )
                    self.assertEqual(
                        [(x["test_id"], x["status"]) for x in github.results()],
                        [("github_os", "PASS")],
                    )
                finally:
                    router.close()
