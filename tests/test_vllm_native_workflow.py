"""Protect the existing GitHub vLLM entry while adding GitCode polling."""

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.contract import VLLM_REPO, create_plan, normalize
from ci_core.router import Router, task_key
from ci_core.vllm_ascend import definition
from tests.scenarios import write_workspace


class NativeVllmWorkflowTest(unittest.TestCase):
    def test_admitted_native_os_probe_preserves_source_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_workspace(root)
            row = definition("vllm_worker_os_blue")
            defaults = {
                "worker": "blue",
                "target_pool": "pool_blue",
                "docker": "training",
            }
            case = normalize(
                SimpleNamespace(
                    test_name=row["test_id"],
                    test_descr=row["label"],
                    nnodes=0,
                    ngpu=0,
                    timeout=row["timeout"],
                    ci_kind=row["kind"],
                    ci_overrides=row["ci_overrides"],
                ),
                defaults,
                0,
            )
            sha = "a" * 40
            run = dict(
                id=123,
                run_attempt=1,
                head_sha=sha,
                _ci_control_sha=sha,
                _ci_repo=VLLM_REPO,
                path=".github/workflows/lite_actions_dispatcher_smoke.yml",
                status="in_progress",
                event="workflow_dispatch",
                head_branch="main",
                actor={"login": "depeng1994"},
                created_at="2026-01-01T00:00:00Z",
                name="Dispatcher-Smoke",
                run_number=123,
            )
            plan = create_plan(
                [case],
                run_id=123,
                attempt=1,
                sha=sha,
                workflow="lite_actions_dispatcher_smoke.yml",
                defaults=defaults,
                repo=VLLM_REPO,
            )
            source = dict(
                schema=1,
                provider="github",
                repository=VLLM_REPO,
                repository_id=1438625957,
                run_id=123,
                attempt=1,
                plan_digest=plan["plan_digest"],
                control_sha=sha,
                head_sha=sha,
                base_ref="main",
                test_ids=[case["test_id"]],
                mode="commit",
                pr_number=0,
                head_repo_id=1438625957,
                base_sha=sha,
                source_sha=sha,
                tree_sha="b" * 40,
            )

            class Client:
                repo = VLLM_REPO
                comments = []

                def ci_request(self, current):
                    return plan

                def workflow_status(self, ident):
                    return {"status": "in_progress", "run_attempt": 1}

                def request(self, method, path, data=None):
                    if "/attempts/1/jobs?" in path:
                        return {
                            "total_count": 1,
                            "jobs": [
                                dict(
                                    name="prepare",
                                    run_id=123,
                                    run_attempt=1,
                                    status="completed",
                                    conclusion="success",
                                )
                            ],
                        }
                    if "/artifacts?" in path:
                        return {
                            "artifacts": [
                                dict(
                                    id=456,
                                    name="lite-ci-source-1",
                                    size_in_bytes=100,
                                    expired=False,
                                    workflow_run={"id": 123},
                                )
                            ]
                        }
                    if path.startswith("/git/commits/"):
                        return {"sha": sha, "tree": {"sha": source["tree_sha"]}}
                    raise AssertionError(path)

                def download_artifact(self, ident, *, limit):
                    data = io.BytesIO()
                    with zipfile.ZipFile(data, "w") as archive:
                        archive.writestr("source.json", json.dumps(source))
                    return data.getvalue()

                def commit_comments(self, commit):
                    return self.comments

                def send_result(self, commit, body):
                    item = {
                        "id": len(self.comments) + 1,
                        "body": body,
                        "user": {"login": "reporter"},
                    }
                    self.comments.append(item)
                    return item

            client = Client()
            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_STATE_DIR": "",
                        "LITE_ACTIONS_VLLM_CONTROL_SHAS": sha,
                    },
                ),
                patch("ci_core.router.progress"),
                patch(
                    "ci_core.router.probe_worker",
                    return_value=SimpleNamespace(
                        returncode=0, stdout="WORKER_HOSTNAME=fixture\n", stderr=""
                    ),
                ),
            ):
                router = Router(root, "blue", client, "reporter")
                try:
                    router.receive([run])
                    router.pump()
                    for future, _, _ in list(router.active.values()):
                        future.result(timeout=5)
                    router.pump()
                    router.flush()
                    self.assertEqual(
                        router.state.read()[task_key(run, case)]["phase"], "REPORTED"
                    )
                    results = [
                        json.loads(c["body"].split("\n", 1)[1])
                        for c in client.comments
                        if c["body"].startswith("Lite-CI-V3 ")
                    ]
                    self.assertEqual(
                        [(r["test_id"], r["status"]) for r in results],
                        [(case["test_id"], "PASS")],
                    )
                    self.assertEqual(results[0]["native_source"], source)
                finally:
                    router.close()
