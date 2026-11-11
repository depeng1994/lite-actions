"""End-to-end contracts for model-owned vLLM smoke source identity."""

import io
import json
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import VLLM_REPO
from ci_core.gitcode import source_plan
from ci_core.github_source import (
    approved_controls,
    bind_request_source,
    check_current,
    prepare_ready,
    read_source,
    validate_source,
)
from ci_core.plan import verify_definition
from ci_core.reporting import report
from ci_core.router import Router
from tests.integration_tests.tools.lite_actions.entrypoint import inspect_profile

ROOT = Path(__file__).resolve().parents[1]


class ModelOwnedVllmTests(unittest.TestCase):
    def setUp(self):
        self.head, self.control, self.base, self.merge, self.tree = (
            c * 40 for c in "abcde"
        )
        self.default_head = self.control
        self.controller_is_ancestor = True
        self.run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": self.head,
            "_ci_repo": VLLM_REPO,
            "_ci_pr_number": 7,
            "_ci_source_repo_id": 87,
            "_ci_source_repo": "another-fork/vllm-ascend",
            "_ci_base_ref": "releases/v0.30.0",
            "_ci_control_sha": self.control,
            "path": ".github/workflows/lite_actions_dispatcher_smoke.yml",
            "event": "pull_request_target",
        }
        self.plan = source_plan(inspect_profile(), self.run, VLLM_REPO)
        self.source = {
            "schema": 1,
            "provider": "github",
            "repository": VLLM_REPO,
            "repository_id": 1438625957,
            "run_id": 42,
            "attempt": 1,
            "plan_digest": self.plan["plan_digest"],
            "control_sha": self.control,
            "head_sha": self.head,
            "base_ref": "releases/v0.30.0",
            "test_ids": [c["test_id"] for c in self.plan["cases"]],
            "mode": "merge_result",
            "pr_number": 7,
            "head_repo_id": 87,
            "base_sha": self.base,
            "source_sha": self.merge,
            "tree_sha": self.tree,
        }
        self.pr = {
            "number": 7,
            "state": "open",
            "merged": False,
            "draft": True,
            "head": {
                "sha": self.head,
                "repo": {"id": 87, "full_name": "another-fork/vllm-ascend"},
            },
            "base": {
                "ref": "releases/v0.30.0",
                "repo": {"id": 1438625957, "full_name": VLLM_REPO},
            },
            "mergeable": True,
            "merge_commit_sha": self.merge,
        }

    def client(self):
        outer = self

        class Client:
            repo = VLLM_REPO

            def request(self, method, path):
                assert method == "GET"
                if path == "":
                    return {"id": 1438625957, "default_branch": "main"}
                if path == "/branches/main":
                    return {"commit": {"sha": outer.default_head}}
                if (
                    path
                    == f"/compare/{outer.control}...{outer.default_head}?per_page=1"
                ):
                    return {
                        "merge_base_commit": {
                            "sha": outer.control
                            if outer.controller_is_ancestor
                            else "0" * 40
                        }
                    }
                if path == "/branches/ci%2Fpilot":
                    return {"commit": {"sha": outer.control}}
                if path == "/pulls/7":
                    return outer.pr
                if path == "/branches/releases%2Fv0.30.0":
                    return {"commit": {"sha": outer.base}}
                if path.startswith("/actions/runs/42/attempts/"):
                    return {
                        "total_count": 1,
                        "jobs": [
                            {
                                "name": "prepare",
                                "run_id": 42,
                                "run_attempt": 1,
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ],
                    }
                if path.startswith("/actions/runs/42/artifacts?"):
                    return {
                        "artifacts": [
                            {
                                "id": 99,
                                "name": "lite-ci-source-1",
                                "workflow_run": {"id": 42},
                            }
                        ]
                    }
                if path == "/git/commits/" + outer.merge:
                    return {
                        "sha": outer.merge,
                        "tree": {"sha": outer.tree},
                        "parents": [{"sha": outer.base}, {"sha": outer.head}],
                    }
                raise AssertionError(path)

            def workflow_status(self, run_id):
                assert run_id == 42
                return {"status": "in_progress", "run_attempt": 1}

            def download_artifact(self, artifact_id, *, limit):
                assert artifact_id == 99 and limit == 131072
                result = io.BytesIO()
                with zipfile.ZipFile(result, "w") as zipped:
                    zipped.writestr("source.json", json.dumps(outer.source))
                return result.getvalue()

        return Client()

    def test_source_identity_is_required_and_stale_fork_or_merge_is_rejected(self):
        client = self.client()
        self.assertEqual(read_source(client, self.run, self.plan), self.source)
        self.assertEqual(
            report(
                {**self.run, "_ci_source": self.source},
                self.plan["cases"][0],
                self.plan["plan_digest"],
                "PASS",
                0,
                [],
            )["native_source"],
            self.source,
        )
        for field, value in (
            ("head_sha", "f" * 40),
            ("head_repo_id", 88),
            ("source_sha", "f" * 40),
            ("plan_digest", "f" * 64),
        ):
            broken = {**self.source, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_source(
                    broken, self.run, self.plan, {self.control}
                ) if field != "source_sha" else check_current(client, broken)
        self.pr["head"]["sha"] = "f" * 40
        with self.assertRaises(ValueError):
            check_current(client, self.source)
        self.pr["head"]["sha"] = self.head
        self.pr["head"]["repo"] = None
        with self.assertRaises(ValueError):
            check_current(client, self.source)
        self.pr["head"]["repo"] = {
            "id": 87,
            "full_name": "another-fork/vllm-ascend",
        }
        self.pr["mergeable"] = None
        self.pr["merge_commit_sha"] = None
        with self.assertRaises(RuntimeError):
            check_current(client, self.source)
        self.pr["mergeable"] = False
        with self.assertRaises(ValueError):
            check_current(client, self.source)
        with self.assertRaises(ValueError):
            validate_source(self.source, self.run, self.plan, {"f" * 40})

    def test_controller_moves_with_default_branch_without_manual_reconfiguration(self):
        client = self.client()
        self.assertEqual(read_source(client, self.run, self.plan), self.source)
        self.default_head = "f" * 40
        self.assertEqual(read_source(client, self.run, self.plan), self.source)
        self.controller_is_ancestor = False
        with self.assertRaises(ValueError):
            read_source(client, self.run, self.plan)
        self.source["control_sha"] = self.default_head
        self.run["_ci_control_sha"] = self.default_head
        self.assertEqual(read_source(client, self.run, self.plan), self.source)

    def test_pr_recheck_accepts_previous_trusted_controller_after_main_moves(self):
        run = {
            **self.run,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "actor": {"login": "github-actions[bot]"},
        }
        self.default_head = "f" * 40
        self.assertEqual(read_source(self.client(), run, self.plan), self.source)
        self.controller_is_ancestor = False
        with self.assertRaises(ValueError):
            read_source(self.client(), run, self.plan)

    def test_manual_commit_is_distinct_from_controller_but_fully_bound(self):
        run = {**self.run, "event": "workflow_dispatch", "head_branch": "main"}
        source = {
            **self.source,
            "mode": "commit",
            "pr_number": 0,
            "head_repo_id": 1438625957,
            "base_ref": "main",
            "base_sha": self.head,
            "source_sha": self.head,
        }
        self.assertEqual(
            validate_source(source, run, self.plan, {self.control}), source
        )
        with self.assertRaises(ValueError):
            validate_source(
                {**source, "source_sha": self.control}, run, self.plan, {self.control}
            )
        with self.assertRaises(ValueError):
            validate_source(source, run, self.plan, {self.head})

    def test_generic_request_binds_commit_and_pr_without_parsing_title(self):
        run = {
            **self.run,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": self.control,
            "name": "lite-ci-anything",
            "display_title": "a readable title",
        }
        bound = bind_request_source(self.client(), run, self.source)
        self.assertEqual(
            (bound["head_sha"], bound["_ci_control_sha"], bound["_ci_pr_number"]),
            (self.head, self.control, 7),
        )
        self.assertEqual(bound["_ci_source_repo"], "another-fork/vllm-ascend")
        self.assertEqual(
            read_source(self.client(), bound, self.plan, source=self.source),
            self.source,
        )
        with self.assertRaises(ValueError):
            bind_request_source(
                self.client(), run, {**self.source, "control_sha": self.head}
            )
        commit_source = {**self.source, "mode": "commit", "pr_number": 0}
        bound = bind_request_source(self.client(), run, commit_source)
        self.assertEqual(
            (bound["head_sha"], bound["_ci_control_sha"]), (self.head, self.control)
        )

    def test_prepare_can_be_found_beyond_the_first_page(self):
        class Client:
            def request(self, method, path):
                self_test = {
                    "name": "prepare",
                    "run_id": 42,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "success",
                }
                if path.endswith("page=1"):
                    return {"total_count": 101, "jobs": [{"name": "case"}] * 100}
                return {"total_count": 101, "jobs": [self_test]}

        self.assertTrue(prepare_ready(Client(), self.run))

    def test_manual_source_is_the_exact_approved_branch_commit(self):
        run = {
            **self.run,
            "head_sha": self.control,
            "head_branch": "ci/pilot",
            "event": "workflow_dispatch",
        }
        plan = source_plan(inspect_profile(), run, VLLM_REPO)
        source = {
            **self.source,
            "mode": "commit",
            "head_sha": self.control,
            "base_sha": self.control,
            "source_sha": self.control,
            "base_ref": "ci/pilot",
            "head_repo_id": 1438625957,
            "pr_number": 0,
            "plan_digest": plan["plan_digest"],
        }
        self.assertEqual(validate_source(source, run, plan, {self.control}), source)
        with self.assertRaises(ValueError):
            validate_source({**source, "base_ref": "main"}, run, plan, {self.control})
        self.assertIn(self.control, approved_controls(self.client(), run))

    def test_model_owned_archive_matches_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "model.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in (
                    "tests/__init__.py",
                    "tests/integration_tests/__init__.py",
                    "tests/integration_tests/training_cases/dispatch_smoke.py",
                    "tests/integration_tests/tools/lite_actions/entrypoint.py",
                ):
                    tar.add(ROOT / name, arcname=name)
            for item in self.plan["cases"]:
                self.assertEqual(
                    verify_definition(
                        archive, item, self.plan["defaults"], repo=VLLM_REPO
                    )["test_id"],
                    item["test_id"],
                )

    def test_pr_admission_binds_every_instance_to_merge_source(self):
        class Client:
            repo = VLLM_REPO

            def ci_request(inner, run):
                return self.plan

        run = {
            **self.run,
            "actor": {"login": "another-contributor"},
            "status": "in_progress",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "ci_core.router.load", return_value=({"blue": {"enabled": True}}, {})
            ),
            patch("ci_core.github_source.read_source", return_value=self.source),
            patch.object(Router, "transition"),
            patch.dict("os.environ", {"LITE_ACTIONS_STATE_DIR": ""}),
        ):
            router = Router(Path(tmp), "blue", Client(), "reporter")
            try:
                router._receive_one(run)
                records = router.state.read().values()
                self.assertEqual(len(records), len(self.plan["cases"]))
                self.assertTrue(
                    all(r["run"]["_ci_execution_sha"] == self.merge for r in records)
                )
            finally:
                router.close()

    def test_generic_request_is_admitted_from_artifacts_not_run_title(self):
        client = self.client()
        run = {
            **self.run,
            "name": "lite-ci-generic",
            "display_title": "ordinary readable title",
            "path": ".github/workflows/lite-ci-generic.yml",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": self.control,
            "status": "in_progress",
        }
        plan_run = {**run, "head_sha": self.head}
        plan = source_plan(inspect_profile(), plan_run, VLLM_REPO)
        source = {**self.source, "plan_digest": plan["plan_digest"]}
        client.ci_request = lambda bound: plan
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "ci_core.router.load", return_value=({"blue": {"enabled": True}}, {})
            ),
            patch("ci_core.github_source.read_source_artifact", return_value=source),
            patch.object(Router, "eligible", return_value=True),
            patch.object(Router, "transition"),
            patch.dict("os.environ", {"LITE_ACTIONS_STATE_DIR": ""}),
        ):
            router = Router(Path(tmp), "blue", client, "reporter")
            try:
                router._receive_one(run)
                records = router.state.read().values()
                self.assertEqual(len(records), len(plan["cases"]))
                self.assertTrue(
                    all(
                        row["run"]["_ci_execution_sha"] == self.merge for row in records
                    )
                )
            finally:
                router.close()

    def test_new_attempt_is_not_cancelled_by_stale_previous_attempt(self):
        class Client:
            repo = VLLM_REPO

            def workflow_status(self, run_id):
                return {"status": "in_progress", "run_attempt": 2}

        old = {**self.run, "_ci_source": {**self.source, "attempt": 1}}
        new = {
            **self.run,
            "run_attempt": 2,
            "_ci_source": {**self.source, "attempt": 2},
        }

        def verify(_client, source):
            if source["attempt"] == 1:
                raise ValueError("old source")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "ci_core.router.load", return_value=({"blue": {"enabled": True}}, {})
            ),
            patch("ci_core.github_source.check_current", side_effect=verify),
            patch.object(Router, "flush"),
            patch.object(Router, "pump"),
            patch.object(Router, "receive"),
            patch.object(Router, "finish") as finish,
            patch.dict("os.environ", {"LITE_ACTIONS_STATE_DIR": ""}),
        ):
            client = Client()
            client.running_workflows = lambda: []
            router = Router(Path(tmp), "blue", client, "reporter")
            try:
                router.state.update("old", phase="QUEUED", run=old)
                router.state.update("new", phase="QUEUED", run=new)
                router.tick()
                self.assertEqual(finish.call_count, 1)
                self.assertEqual(finish.call_args.args[0], "old")
            finally:
                router.close()

    def test_head_or_base_change_cancels_queued_and_running_cases(self):
        for change in ("head", "base"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                client = self.client()
                client.running_workflows = lambda: []
                run = {**self.run, "_ci_source": self.source}
                if change == "head":
                    self.pr["head"]["sha"] = "f" * 40
                else:
                    self.base = "f" * 40
                with (
                    patch(
                        "ci_core.router.load",
                        return_value=({"blue": {"enabled": True}}, {}),
                    ),
                    patch.object(Router, "flush"),
                    patch.object(Router, "pump"),
                    patch.object(Router, "receive"),
                    patch.dict("os.environ", {"LITE_ACTIONS_STATE_DIR": ""}),
                ):
                    router = Router(Path(tmp), "blue", client, "reporter")
                    cancel = threading.Event()
                    try:
                        record = {
                            "run": run,
                            "item": self.plan["cases"][1],
                            "digest": self.plan["plan_digest"],
                        }
                        router.state.update("queued", phase="QUEUED", **record)
                        router.state.update("running", phase="RUNNING", **record)
                        router.active["running"] = (None, cancel, None)
                        router.tick()
                        self.assertEqual(
                            router.state.read()["queued"]["report"]["status"],
                            "CANCELLED",
                        )
                        self.assertTrue(cancel.is_set())
                    finally:
                        router.active.clear()
                        router.close()
                        self.pr["head"]["sha"] = self.head
                        self.base = self.source["base_sha"]


if __name__ == "__main__":
    unittest.main()
