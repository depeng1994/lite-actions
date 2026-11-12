"""CPU-only coverage of source-profile admission and shared-pool scheduling."""

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import digest, parse_plan
from ci_core.executor import Execution
from ci_core.gitcode import GitCodeClient, MultiPlatformClient, source_plan
from ci_core.job_state import JobState
from ci_core.reporting import report
from ci_core.router import Router, task_key
from tests.scenarios import (
    DEFAULTS,
    Service,
    completed_execution,
    definition,
    write_workspace,
)


def sample_run():
    return {
        "_ci_provider": "gitcode",
        "_ci_repo": "depeng1994/torchtitan-npu",
        "id": "smoke_run_1",
        "run_attempt": 1,
        "head_sha": "b" * 40,
        "path": ".gitcode/workflows/lite-actions.yml",
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_branch": "master",
        "actor": {"login": "maintainer"},
        "created_at": "2026-01-01T00:00:00Z",
        "name": "lite-ci-dispatcher-smoke",
        "run_number": 1,
    }


def sample_source_plan(run):
    rows = []
    for name, nodes in (("smoke_alpha", 1), ("smoke_beta", 2), ("smoke_alpha", 1)):
        rows.append(
            {
                "test_id": name,
                "label": name,
                "kind": "npu_smoke",
                "ci_overrides": DEFAULTS,
                "nnodes": nodes,
                "ngpu": 2,
                "timeout": 90,
                "env_vars": {"ASCEND_SET_ENV_PATH": "/fixture/cann"},
            }
        )
    return source_plan(rows, run, run["_ci_repo"])


class SmokeQueueTests(unittest.TestCase):
    def test_upgrade_waits_for_old_jobs_but_reports_completed_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            github = Service([definition("smoke", kind="npu_smoke")], root)
            run = {**github.runs[20], "_ci_catalog": True}
            plan = github.plans[20]
            item = plan["cases"][0]
            environment = {"LITE_ACTIONS_STATE_DIR": ""}
            with patch.dict(os.environ, environment), github.connected():
                state = JobState(root)
                state.update(
                    "legacy",
                    run=run,
                    item=item,
                    phase="QUEUED",
                    report=report(run, item, plan["plan_digest"], "PASS", 0, [], True),
                )
                with self.assertRaisesRegex(ValueError, "unfinished tasks"):
                    router = Router(root, "blue", github, "reporter")
                    router.close()
                state.update("legacy", phase="PENDING_REPORT")
                Router(root, "blue", github, "reporter").close()
                self.assertEqual(github.results()[0]["status"], "PASS")
                self.assertEqual(state.read()["legacy"]["phase"], "REPORTED")
                Router(root, "blue", github, "reporter").close()

    def test_submission_failure_releases_capacity_for_next_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            github = Service([definition("smoke", kind="npu_smoke")], root)
            environment = {
                "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer",
                "LITE_ACTIONS_STATE_DIR": "",
                "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
            }
            with patch.dict(os.environ, environment), github.connected():
                router = Router(root, "blue", github, "reporter")
                try:
                    with patch.object(
                        router.training,
                        "submit",
                        side_effect=RuntimeError("unavailable"),
                    ):
                        router.receive([github.runs[20]])
                        router.pump()
                        router.flush()
                    self.assertEqual(github.results()[0]["status"], "INFRA_ERROR")
                    github.add_run(21)
                    with patch(
                        "ci_core.router.execute",
                        side_effect=lambda case, run, **_: completed_execution(
                            root, case, run
                        ),
                    ):
                        router.receive([github.runs[21]])
                        for _ in range(10):
                            router.pump()
                            for future, _, _ in list(router.active.values()):
                                future.result(10)
                            router.flush()
                            if len(github.results()) == 2:
                                break
                    self.assertEqual(
                        [result["status"] for result in github.results()],
                        ["INFRA_ERROR", "PASS"],
                    )
                finally:
                    router.close()

    def test_repeated_smoke_instances_reject_conflicting_definitions(self):
        run = sample_run()
        plan = sample_source_plan(run)
        self.assertEqual(
            [case["test_id"] for case in plan["cases"]],
            ["smoke_alpha", "smoke_beta", "smoke_alpha"],
        )
        self.assertEqual(len({case["case_id"] for case in plan["cases"]}), 3)
        for field, error in (
            ("case_id", "case order/ID"),
            ("topology", "duplicate/invalid test"),
        ):
            altered = copy.deepcopy(plan)
            if field == "case_id":
                altered["cases"][2]["case_id"] = altered["cases"][0]["case_id"]
            else:
                altered["cases"][2]["resources"]["nodes"] = 2
            altered["plan_digest"] = digest(
                {k: v for k, v in altered.items() if k != "plan_digest"}
            )
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                parse_plan(altered, run, altered["workflow"], repo=run["_ci_repo"])

    def test_gitcode_and_github_share_fifo_and_repeated_smokes_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            definitions = [
                definition("smoke_alpha", kind="npu_smoke"),
                definition("smoke_beta", kind="npu_smoke", nodes=2),
            ]
            github = Service([definitions[0], definitions[0]], root)
            gitcode = GitCodeClient("fixture", "depeng1994/torchtitan-npu")
            run = sample_run()
            plan = sample_source_plan(run)
            comments = []
            first_entered, release_first = threading.Event(), threading.Event()
            started = []

            def send(_, body, **kwargs):
                row = {
                    "id": len(comments) + 1,
                    "body": body,
                    "user": {"login": "reporter"},
                }
                comments.append(row)
                return row

            def execute(case, current, **_):
                instance = current.get("_ci_case_id", current["_ci_test_id"])
                source = current.get("_ci_provider", "github")
                started.append((source, instance))
                if (source, instance) == ("gitcode", "case_000_smoke_alpha"):
                    first_entered.set()
                    if not release_first.wait(10):
                        raise RuntimeError("fixture wait timed out")
                log = root / (source + "_" + instance + ".log")
                log.write_text(f"CASE_RESULT {current['_ci_test_id']} PASS 0\n")
                return Execution(0, ["fixture completed"], log, True)

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer",
                        "LITE_ACTIONS_GITCODE_ALLOWED_ACTORS": "maintainer",
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                        "LITE_ACTIONS_STATE_DIR": "",
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
                patch.object(gitcode, "commit_comments", return_value=comments),
                patch.object(gitcode, "send_result", side_effect=send),
                patch.object(gitcode, "request", return_value=None),
                patch(
                    "ci_core.router.verify_definition",
                    return_value={"env_vars": {"ASCEND_SET_ENV_PATH": "/fixture/cann"}},
                ),
                patch("ci_core.router.execute", side_effect=execute),
            ):
                router = Router(
                    root,
                    "blue",
                    MultiPlatformClient(github, {gitcode.repo: gitcode}),
                    "reporter",
                )
                try:
                    router.receive([run])
                    router.receive([run])
                    router.receive([github.runs[20]])
                    state = router.state.read()
                    keys = [task_key(run, item) for item in plan["cases"]]
                    keys.extend(
                        task_key(github.runs[20], item)
                        for item in github.plans[20]["cases"]
                    )
                    self.assertEqual(
                        [state[key]["admission_seq"] for key in keys], [1, 2, 3, 4, 5]
                    )
                    router.pump()
                    self.assertTrue(first_entered.wait(10))
                    state = router.state.read()
                    for item in plan["cases"][1:]:
                        self.assertEqual(state[task_key(run, item)]["phase"], "QUEUED")
                    for item in github.plans[20]["cases"]:
                        self.assertEqual(
                            state[task_key(github.runs[20], item)]["phase"], "QUEUED"
                        )
                    self.assertEqual(started, [("gitcode", "case_000_smoke_alpha")])
                    release_first.set()
                    for _ in range(20):
                        for future, _, _ in list(router.active.values()):
                            future.result(10)
                        router.pump()
                        router.flush()
                        if not router.active and all(
                            x["phase"] == "REPORTED"
                            for x in router.state.read().values()
                        ):
                            break
                    else:
                        self.fail("fixture did not settle")
                    gitcode_results = [
                        json.loads(c["body"].split("\n", 1)[1])
                        for c in comments
                        if c["body"].startswith("Lite-CI-V3 ")
                    ]
                    self.assertEqual(
                        {x["case_id"] for x in gitcode_results},
                        {x["case_id"] for x in plan["cases"]},
                    )
                    self.assertTrue(all(x["status"] == "PASS" for x in gitcode_results))
                    self.assertEqual(
                        [r["status"] for r in github.results()], ["PASS", "PASS"]
                    )
                    self.assertEqual(
                        {r["case_id"] for r in github.results()},
                        {item["case_id"] for item in github.plans[20]["cases"]},
                    )
                    self.assertLess(
                        started.index(("gitcode", "smoke_beta")),
                        started.index(("gitcode", "case_002_smoke_alpha")),
                    )
                finally:
                    release_first.set()
                    router.close()
