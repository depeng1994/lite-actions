"""Exercise a GitCode request through the shared router without real NPUs."""

import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
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
            details = {
                "workflow_run_id": run["id"],
                "attempt": 1,
                "event": "Manual",
                "status": "RUNNING",
                "head_sha": run["head_sha"],
                "head_branch": run["head_branch"],
                "file_path": run["path"],
                "actor": run["actor"],
                "start_time": 1767225600,
                "run_number": 1,
            }

            def request(method, path, **kwargs):
                if path.startswith("/actions/runs?"):
                    return {
                        "workflow_runs": [
                            {**details, "workflow_run_id": "temporarily_unavailable"},
                            details,
                        ]
                    }
                if path.endswith("/temporarily_unavailable"):
                    raise urllib.error.URLError("temporary API failure")
                if path.endswith("/" + run["id"]):
                    return details
                raise AssertionError(f"unexpected API request: {path}")

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
                patch.object(gitcode, "request", side_effect=request),
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

    def test_cross_platform_jobs_share_device_and_port_leases(self):
        import threading

        from ci_core.resources import ResourceBusy, reserve
        from tests.scenarios import completed_execution

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_workspace(root)
            github = Service([definition("github_smoke", kind="npu_smoke")], root)
            gitcode = GitCodeClient("unused-token", "depeng1994/torchtitan-npu")
            defaults = {
                "worker": "blue",
                "target_pool": "pool_blue",
                "docker": "training",
            }
            case = normalize(definition("gitcode_smoke", kind="npu_smoke"), defaults, 0)
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
                "defaults": defaults,
                "cases": [case],
            }
            plan["plan_digest"] = digest(plan)
            entered, release, allocations, comments = (
                threading.Barrier(3),
                threading.Event(),
                [],
                [],
            )

            def execute(selected, current, **kwargs):
                allocations.append(selected)
                entered.wait(10)
                release.wait(10)
                return completed_execution(root, selected, current)

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
                patch("ci_core.router.execute", side_effect=execute),
            ):
                router = Router(
                    root,
                    "blue",
                    MultiPlatformClient(github, {gitcode.repo: gitcode}),
                    "reporter",
                )
                router.pools["pool_blue"]["nodes"] = router.pools["pool_blue"]["nodes"][
                    :1
                ]
                try:
                    router.tick()
                    entered.wait(10)
                    left, right = allocations
                    self.assertNotEqual(left["master_port"], right["master_port"])
                    self.assertTrue(
                        set(left["allocation"][0]["devices"]).isdisjoint(
                            right["allocation"][0]["devices"]
                        )
                        or left["allocation"][0]["host_id"]
                        != right["allocation"][0]["host_id"]
                    )
                    for selected in allocations:
                        with (
                            self.assertRaises(ResourceBusy),
                            reserve(root / "locks/devices", selected["allocation"]),
                        ):
                            pass
                    release.set()
                    for future, _, _ in list(router.active.values()):
                        future.result(10)
                    router.pump()
                    router.flush()
                    self.assertEqual(github.results()[0]["status"], "PASS")
                    result = [
                        x for x in comments if x["body"].startswith("Lite-CI-V3 ")
                    ]
                    self.assertEqual(
                        json.loads(result[0]["body"].split("\n", 1)[1])["status"],
                        "PASS",
                    )
                finally:
                    release.set()
                    router.close()
