"""CPU-only coverage of catalog admission and shared-pool scheduling."""

import copy
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from ci_core.executor import Execution
from ci_core.gitcode import GitCodeClient, MultiPlatformClient, catalog_plan
from ci_core.job_state import JobState
from ci_core.router import Router, task_key
from tests.scenarios import DEFAULTS, Service, definition, write_workspace


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
        "name": "Dispatcher-Smoke",
        "run_number": 1,
    }


def sample_catalog():
    rows = []
    for name, nodes in (("smoke_8p", 1), ("smoke_16p", 2)):
        rows.append(
            {
                "test_id": name,
                "label": name,
                "kind": "npu_smoke",
                "worker": "blue",
                "nodes": nodes,
                "devices_per_node": 2,
                "timeout": 90,
                "env_vars": {"ASCEND_SET_ENV_PATH": "/fixture/cann"},
            }
        )
    return {
        "version": 1,
        "default_profile": "fifo_smoke",
        "defaults": DEFAULTS,
        "cases": rows,
        "profiles": {"fifo_smoke": ["smoke_8p", "smoke_16p", "smoke_8p"]},
    }


class CatalogTests(unittest.TestCase):
    def test_catalog_validation_rejects_ambiguity_and_invalid_unused_case(self):
        run = sample_run()
        catalog = sample_catalog()
        plan = catalog_plan(json.dumps(catalog), run, run["_ci_repo"])
        self.assertEqual(
            [case["test_id"] for case in plan["cases"]],
            ["smoke_8p", "smoke_16p", "smoke_8p"],
        )
        self.assertEqual(len({case["case_id"] for case in plan["cases"]}), 3)
        for field, bad in (("timeout", 0), ("nodes", True), ("env_vars", {"key": 3})):
            broken = copy.deepcopy(catalog)
            broken["cases"].append(
                {**broken["cases"][0], "test_id": "unused", field: bad}
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                catalog_plan(json.dumps(broken), run, run["_ci_repo"])
        with self.assertRaises(ValueError):
            catalog_plan('{"version":1,"version":1}', run, run["_ci_repo"])

    def test_uploaded_catalog_must_match_pinned_source_and_probe_guard(self):
        run = sample_run()
        raw = json.dumps(sample_catalog()).encode()
        client = GitCodeClient("secret-fixture-token", run["_ci_repo"])
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("smoke_cases.json", raw)
        payload = data.getvalue()

        class Redirect:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": "https://fixture.invalid/asset"},
                    None,
                )

        def download(request, **_):
            self.assertNotIn("Private-token", request.headers)
            self.assertNotIn("Authorization", request.headers)
            return io.BytesIO(payload)

        def source(contents, directory):
            path = directory / "pinned.tar.gz"
            with tarfile.open(path, "w:gz") as tar:
                info = tarfile.TarInfo(".ci/lite_actions/smoke_cases.json")
                info.size = len(contents)
                tar.addfile(info, io.BytesIO(contents))
            return path

        with tempfile.TemporaryDirectory() as tmp:
            pinned = source(raw, Path(tmp))
            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_GITCODE_CATALOG_WORKFLOW": "lite-actions.yml",
                        "LITE_ACTIONS_GITCODE_CATALOG_BRANCHES": "master",
                        "LITE_ACTIONS_GITCODE_PROBE_ONLY": "0",
                    },
                ),
                patch.object(
                    client,
                    "request",
                    return_value={
                        "artifacts": [
                            {
                                "name": "lite-ci-smoke-catalog-1",
                                "workflow_run_id": run["id"],
                                "id": "artifact",
                                "size_bytes": len(payload),
                            }
                        ]
                    },
                ),
                patch("urllib.request.build_opener", return_value=Redirect()),
                patch("urllib.request.urlopen", side_effect=download),
                patch("ci_core.ssh_runner.model_archive", return_value=pinned),
            ):
                plan = client.ci_request(run)
                self.assertTrue(run["_ci_catalog"])
                self.assertEqual(len(plan["cases"]), 3)
                with (
                    patch.dict(os.environ, {"LITE_ACTIONS_GITCODE_PROBE_ONLY": "1"}),
                    self.assertRaises(ValueError),
                ):
                    client.ci_request(sample_run())
                source(b"different", Path(tmp))
                with self.assertRaisesRegex(ValueError, "does not match"):
                    client.ci_request(sample_run())

    def test_batch_admission_is_idempotent_and_survives_metadata_rollback(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"LITE_ACTIONS_STATE_DIR": ""}),
        ):
            state = JobState(Path(tmp))
            run = sample_run()
            plan = catalog_plan(json.dumps(sample_catalog()), run, run["_ci_repo"])
            entries = [
                (
                    task_key(run, item),
                    {
                        "run": run,
                        "item": item,
                        "digest": plan["plan_digest"],
                        "defaults": plan["defaults"],
                        "phase": "QUEUED",
                    },
                )
                for item in plan["cases"]
            ]
            self.assertEqual(len(state.admit(entries)), 3)
            self.assertEqual(state.admit(entries), [])
            self.assertEqual(
                [state.read()[key]["admission_seq"] for key, _ in entries], [1, 2, 3]
            )
            changed = copy.deepcopy(entries)
            for _, item in changed:
                item["run"]["path"] = ".gitcode/workflows/other.yml"
            with self.assertRaisesRegex(ValueError, "conflicting"):
                state.admit(changed)
            raw = json.loads(state.path.read_text())
            del raw["__lite_actions_metadata__"]
            state.path.write_text(json.dumps(raw))
            other = copy.deepcopy(entries[0][1])
            other["run"]["id"] = "next_run"
            self.assertEqual(state.admit([("new", other)]), ["new"])
            self.assertEqual(state.read()["new"]["admission_seq"], 4)
            self.assertNotIn("__lite_actions_metadata__", state.read())

    def test_plan_publication_is_idempotent_and_conflicts_fail_closed(self):
        run = sample_run()
        client = GitCodeClient("fixture", run["_ci_repo"])
        plan = catalog_plan(json.dumps(sample_catalog()), run, run["_ci_repo"])
        comments = []

        def send(_, body):
            comments.append({"body": body, "user": {"login": "reporter"}})

        with (
            patch.object(client, "commit_comments", return_value=comments),
            patch.object(client, "send_result", side_effect=send),
        ):
            client.publish_plan(run, plan, "reporter")
            client.publish_plan(run, plan, "reporter")
            self.assertEqual(len(comments), 1)
            comments[0]["body"] += "different"
            with self.assertRaisesRegex(ValueError, "conflicting"):
                client.publish_plan(run, plan, "reporter")

    def test_catalog_and_github_share_fifo_and_repeated_smokes_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            definitions = [
                definition("smoke_8p", kind="npu_smoke"),
                definition("smoke_16p", kind="npu_smoke", nodes=2),
            ]
            github = Service([definitions[0]], root)
            gitcode = GitCodeClient("fixture", "depeng1994/torchtitan-npu")
            run = sample_run()
            plan = catalog_plan(json.dumps(sample_catalog()), run, gitcode.repo)
            comments = []
            first_entered, release_first = threading.Event(), threading.Event()
            started = []

            def send(_, body):
                row = {
                    "id": len(comments) + 1,
                    "body": body,
                    "user": {"login": "reporter"},
                }
                comments.append(row)
                return row

            def execute(case, current, **_):
                instance = current.get("_ci_case_id", current["_ci_test_id"])
                started.append(instance)
                if instance == "case_000_smoke_8p":
                    first_entered.set()
                    if not release_first.wait(10):
                        raise RuntimeError("fixture wait timed out")
                log = root / (instance + ".log")
                log.write_text(f"CASE_RESULT {current['_ci_test_id']} PASS 0\n")
                return Execution(0, ["fixture completed"], log, True)

            def get_catalog(current):
                current["_ci_catalog"] = True
                return plan

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
                patch.object(gitcode, "ci_request", side_effect=get_catalog),
                patch.object(gitcode, "validate_current"),
                patch.object(
                    gitcode,
                    "workflow_status",
                    return_value={"status": "in_progress", "run_attempt": 1},
                ),
                patch.object(gitcode, "commit_comments", return_value=comments),
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
                try:
                    router.receive([run])
                    router.receive([github.runs[20]])
                    self.assertEqual(len(router.state.read()), 4)
                    router.pump()
                    self.assertFalse(started)  # no launch before the plan receipt
                    router.flush()
                    router.pump()
                    self.assertTrue(first_entered.wait(10))
                    state = router.state.read()
                    for item in plan["cases"][1:]:
                        self.assertEqual(state[task_key(run, item)]["phase"], "QUEUED")
                    self.assertEqual(
                        state[task_key(github.runs[20], github.plans[20]["cases"][0])][
                            "phase"
                        ],
                        "QUEUED",
                    )
                    self.assertEqual(started, ["case_000_smoke_8p"])
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
                    self.assertEqual(github.results()[0]["status"], "PASS")
                    self.assertLess(
                        started.index("smoke_16p"), started.index("case_002_smoke_8p")
                    )
                finally:
                    release_first.set()
                    router.close()
