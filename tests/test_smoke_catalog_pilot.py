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
from types import SimpleNamespace
from unittest.mock import patch

from ci_core import ssh_runner
from ci_core.contract import digest, parse_plan
from ci_core.executor import Execution
from ci_core.gitcode import GitCodeClient, MultiPlatformClient, catalog_plan
from ci_core.job_state import JobState
from ci_core.resources import reserve, reserve_host_budget, reserve_ports
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
        "name": "Dispatcher-Smoke",
        "run_number": 1,
    }


def sample_catalog():
    rows = []
    for name, nodes in (("smoke_alpha", 1), ("smoke_beta", 2)):
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
        "profiles": {"fifo_smoke": ["smoke_alpha", "smoke_beta", "smoke_alpha"]},
    }


class CatalogTests(unittest.TestCase):
    def test_smoke_ids_use_model_owned_staging_launch_and_verification(self):
        node = dict(
            host_id="fixture",
            ssh="fixture",
            node_ip="192.0.2.1",
            position=0,
            devices=[0, 1],
        )
        nodes = [node, {**node, "node_ip": "192.0.2.2", "position": 1}]
        runner = dict(
            kind="npu_smoke",
            mode="distributed",
            env_vars={"ASCEND_SET_ENV_PATH": "/fixture/cann"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "source.tar.gz"
            archive.write_bytes(b"source")
            with (
                patch.object(ssh_runner, "VLLM", False),
                patch("ci_core.transport.run"),
                patch.object(
                    ssh_runner, "ssh", return_value=SimpleNamespace(returncode=0)
                ) as ssh,
            ):
                ssh_runner.stage(node, "/fixture", archive, "a" * 40)
                self.assertIn(
                    "repo/tests/integration_tests/__init__.py", ssh.call_args.args[1]
                )
                for test_id in (
                    "dispatch_smoke_a3_8p",
                    "dispatch_smoke_a3_16p",
                    "new_smoke_case",
                ):
                    with self.subTest(test_id=test_id):
                        command = ssh_runner.remote_command(
                            node, nodes, runner, "/fixture/node0", "stamp", 60, test_id
                        )
                        self.assertIn(
                            "tests.integration_tests.tools.lite_actions.entrypoint launch",
                            command,
                        )
                        self.assertIn(test_id, command)
                        self.assertNotIn("trusted_hccl.py", command)
                        self.assertEqual(
                            ssh_runner.verify(nodes, runner, "/fixture", 60, test_id),
                            0,
                        )
                        self.assertIn(
                            "tests.integration_tests.tools.lite_actions.entrypoint verify",
                            ssh.call_args.args[1],
                        )
                        self.assertIn(test_id, ssh.call_args.args[1])

    def test_failed_submission_never_runs_and_releases_the_complete_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leases = root / "leases"
            write_workspace(root)
            github = Service([definition("case_alpha", kind="npu_smoke")], root)
            environment = {
                "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer",
                "LITE_ACTIONS_STATE_DIR": "",
                "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(leases),
            }
            with patch.dict(os.environ, environment), github.connected():
                router = Router(root, "blue", github, "reporter")
                queued = []

                def fail_after_enqueue(callback, *args):
                    queued.append((callback, args))
                    raise RuntimeError("failure after enqueue")

                try:
                    with (
                        patch.object(
                            router.training, "submit", side_effect=fail_after_enqueue
                        ),
                        patch.object(router, "work") as work,
                    ):
                        router.receive([github.runs[20]])
                        router.pump()
                        case = github.plans[20]["cases"][0]
                        record = router.state.read()[task_key(github.runs[20], case)]
                        self.assertEqual(record["report"]["status"], "INFRA_ERROR")
                        self.assertFalse(record["quarantined"])
                        allocation = record["allocation"]
                        with (
                            reserve(leases / "devices", allocation),
                            reserve_host_budget(
                                leases / "host-budget", allocation, max_jobs_per_host=2
                            ) as slots,
                            reserve_ports(leases / "ports", allocation) as ports,
                        ):
                            self.assertEqual(
                                slots, record["runtime"]["host_budget_slots"]
                            )
                            self.assertEqual(
                                ports["port_slot"], record["runtime"]["port_slot"]
                            )
                            callback, args = queued[0]
                            delayed = threading.Thread(
                                target=callback, args=args, daemon=True
                            )
                            delayed.start()
                            delayed.join(2)
                            self.assertFalse(delayed.is_alive())
                            work.assert_not_called()
                finally:
                    router.close()

    def test_interrupted_registration_releases_lease_and_closes_cleanly(self):
        for inserted, release_fails in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(inserted=inserted, release_fails=release_fails),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                leases = root / "leases"
                write_workspace(root)
                github = Service([definition("smoke", kind="npu_smoke")], root)
                environment = {
                    "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer",
                    "LITE_ACTIONS_STATE_DIR": "",
                    "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(leases),
                    "LITE_ACTIONS_DISABLE_QUARANTINE_WORKERS": "",
                }
                with patch.dict(os.environ, environment), github.connected():
                    router = Router(root, "blue", github, "reporter")

                    class InterruptedRegistration(dict):
                        def __setitem__(self, key, value):
                            assert key in router.active
                            if inserted:
                                super().__setitem__(key, value)
                            raise KeyboardInterrupt

                    router.preflight = InterruptedRegistration()
                    original_claim = router.claim

                    def claim(case):
                        lease, selected = original_claim(case)
                        if release_fails:

                            def fail_release():
                                raise RuntimeError("fixture release unconfirmed")

                            lease.callback(fail_release)
                        return lease, selected

                    try:
                        with (
                            patch.object(router, "work") as work,
                            patch.object(router, "claim", side_effect=claim),
                            patch("ci_core.router.logging.exception"),
                        ):
                            router.receive([github.runs[20]])
                            with self.assertRaises(KeyboardInterrupt):
                                router.pump()
                            self.assertFalse(router.active)
                            self.assertFalse(router.preflight)
                            item = github.plans[20]["cases"][0]
                            key = task_key(github.runs[20], item)
                            record = router.state.read()[key]
                            self.assertEqual(
                                record["phase"],
                                "RESERVED" if release_fails else "QUEUED",
                            )
                            if not release_fails:
                                self.assertEqual(record["allocation"], [])
                            allocation = record["runtime"]["allocation"]
                            with (
                                reserve(leases / "devices", allocation),
                                reserve_host_budget(
                                    leases / "host-budget",
                                    allocation,
                                    max_jobs_per_host=2,
                                ) as slots,
                                reserve_ports(leases / "ports", allocation) as ports,
                            ):
                                self.assertEqual(
                                    slots, record["runtime"]["host_budget_slots"]
                                )
                                self.assertEqual(
                                    ports["port_slot"], record["runtime"]["port_slot"]
                                )
                                router.close()
                                work.assert_not_called()
                            resumed = Router(root, "blue", github, "reporter")
                            try:
                                if release_fails:
                                    record = resumed.state.read()[key]
                                    self.assertEqual(
                                        record["report"]["status"], "INFRA_ERROR"
                                    )
                                    self.assertFalse(
                                        record["report"]["cleanup_verified"]
                                    )
                                    self.assertTrue(record["quarantined"])
                                    continue
                                with patch(
                                    "ci_core.router.execute",
                                    side_effect=lambda case, run, **_: (
                                        completed_execution(root, case, run)
                                    ),
                                ):
                                    resumed.pump()
                                    resumed.active[key][0].result(10)
                                    resumed.pump()
                                    resumed.flush()
                                self.assertEqual(
                                    resumed.state.read()[key]["report"]["status"],
                                    "PASS",
                                )
                            finally:
                                resumed.close()
                    finally:
                        router.close()

    def test_catalog_validation_rejects_ambiguity_and_invalid_unused_case(self):
        run = sample_run()
        catalog = sample_catalog()
        plan = catalog_plan(json.dumps(catalog), run, run["_ci_repo"])
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
            root = Path(tmp)
            state = JobState(root)
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

    def test_catalog_and_github_share_fifo_and_repeated_smokes_are_distinct(self):
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
                source = current.get("_ci_provider", "github")
                started.append((source, instance))
                if (source, instance) == ("gitcode", "case_000_smoke_alpha"):
                    first_entered.set()
                    if not release_first.wait(10):
                        raise RuntimeError("fixture wait timed out")
                log = root / (source + "_" + instance + ".log")
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
                    self.assertFalse(started)  # no launch before the plan receipt
                    router.flush()
                    receipts = [
                        c for c in comments if c["body"].startswith("Lite-CI-PLAN ")
                    ]
                    self.assertEqual(len(receipts), 1)
                    gitcode.publish_plan(run, plan, "reporter")
                    self.assertEqual(
                        len(
                            [
                                c
                                for c in comments
                                if c["body"].startswith("Lite-CI-PLAN ")
                            ]
                        ),
                        1,
                    )
                    original = receipts[0]["body"]
                    receipts[0]["body"] += "different"
                    with self.assertRaisesRegex(ValueError, "conflicting"):
                        gitcode.publish_plan(run, plan, "reporter")
                    receipts[0]["body"] = original
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
