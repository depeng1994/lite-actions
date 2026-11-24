"""Exercise a GitCode request through the shared router without real NPUs."""

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.config import runtime_case
from ci_core.contract import create_plan, digest, normalize, parse_plan
from ci_core.gitcode import GitCodeClient, MultiPlatformClient, source_plan
from ci_core.router import Router
from tests.scenarios import Service, completed_execution, definition, write_workspace


class GitCodeWorkflowTests(unittest.TestCase):
    def test_existing_fixed_request_is_bound_to_its_run(self):
        client = GitCodeClient("fixture", "depeng1994/torchtitan-npu")
        run = {
            "_ci_provider": "gitcode",
            "id": "run_1",
            "run_attempt": 1,
            "head_sha": "b" * 40,
            "path": ".gitcode/workflows/lite-actions.yml",
        }
        plan = source_plan(
            [
                dict(
                    test_id="probe",
                    label="probe",
                    kind="worker_probe",
                    nnodes=0,
                    ngpu=0,
                    timeout=90,
                    ci_overrides={"worker": "blue"},
                )
            ],
            run,
            client.repo,
        )

        def download(request, **_):
            self.assertNotIn("Private-token", request.headers)
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr("ci-request.json", json.dumps(plan))
            return io.BytesIO(data.getvalue())

        class Redirect:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "redirect",
                    {"Location": "https://fixture.invalid/asset"},
                    None,
                )

        artifact = {
            "name": "lite-ci-request-1",
            "workflow_run_id": "run_1",
            "id": "artifact",
        }
        with (
            patch.dict(os.environ, {"LITE_ACTIONS_GITCODE_SOURCE_WORKFLOWS": ""}),
            patch.object(client, "validate_current"),
            patch.object(client, "request", return_value={"artifacts": [artifact]}),
            patch("urllib.request.build_opener", return_value=Redirect()),
            patch("urllib.request.urlopen", side_effect=download),
        ):
            self.assertEqual(client.ci_request(run), plan)
            plan["run_id"] = "other_run"
            with self.assertRaises(ValueError):
                client.ci_request(run)

    def test_model_artifacts_run_fixed_worker_probe_on_either_platform(self):
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
                "event": "Manual",
                "status": "in_progress",
                "head_branch": "master",
                "actor": {"login": "maintainer"},
                "created_at": "2026-01-01T00:00:00Z",
                "name": "lite-ci-dispatcher-smoke",
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
                "workflow_name": "lite-ci-dispatcher-smoke",
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

            def send(sha, body, **kwargs):
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
                    gitcode,
                    "commit_comments",
                    side_effect=lambda *args, **kwargs: comments,
                ),
                patch.object(gitcode, "send_result", side_effect=send),
                patch.object(gitcode, "request", side_effect=request),
                patch("ci_core.router.execute") as execute,
                patch("ci_core.router.model_archive") as archive,
                patch(
                    "ci_core.router.probe_worker",
                    return_value=subprocess.CompletedProcess(
                        [], 0, "WORKER_HOSTNAME=blue-host\nWORKER_ARCH=x86_64\n", ""
                    ),
                ) as probe,
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
                    router.tick()
                finally:
                    router.close()
                execute.assert_not_called()
                archive.assert_not_called()
                self.assertEqual(probe.call_count, 2)
            gitcode_results = [
                json.loads(c["body"].split("\n", 1)[1])
                for c in comments
                if c["body"].startswith("Lite-CI-V3 ")
            ]
            self.assertEqual(len(gitcode_results), 1)
            self.assertEqual(gitcode_results[0]["status"], "PASS")
            self.assertEqual(gitcode_results[0]["test_id"], "gitcode_os")
            self.assertIn(
                "WORKER_HOSTNAME=blue-host", gitcode_results[0]["last_20_lines"]
            )
            self.assertEqual(len(github.results()), 1)
            self.assertEqual(github.results()[0]["status"], "PASS")
            self.assertEqual(github.results()[0]["test_id"], "github_os")
            self.assertIn(
                "WORKER_HOSTNAME=blue-host", github.results()[0]["last_20_lines"]
            )

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
                "event": "Manual",
                "status": "in_progress",
                "head_branch": "master",
                "actor": {"login": "maintainer"},
                "created_at": "2026-01-01T00:00:00Z",
                "name": "lite-ci-dispatcher-smoke",
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
            first_ready = threading.Event()

            def execute(selected, current, **kwargs):
                allocations.append(selected)
                current["_ci_phase_callback"]("LAUNCHING")
                first_ready.set()
                entered.wait(10)
                release.wait(10)
                return completed_execution(root, selected, current)

            def send(sha, body, **kwargs):
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
                    gitcode,
                    "commit_comments",
                    side_effect=lambda *args, **kwargs: comments,
                ),
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
                router.pools["pool_blue"]["nodes"] = router.pools["pool_blue"]["nodes"][
                    :1
                ]
                try:
                    router.tick()
                    self.assertTrue(first_ready.wait(10))
                    router.pump()
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

    def test_gitcode_dispatcher_smoke_multi_case_request_artifact_without_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_workspace(root)
            gitcode = GitCodeClient("unused-token", "cann/torchtitan-npu")
            defaults = {
                "worker": "blue",
                "target_pool": "pool_blue",
                "docker": "training",
            }
            env_vars = {
                "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh",
                "TORCH_NPU_EXTRA": "1",
            }
            cases = [
                normalize(
                    SimpleNamespace(
                        test_name="dispatch_smoke_8p",
                        test_descr="8P smoke",
                        nnodes=1,
                        ngpu=2,
                        ci_kind="npu_smoke",
                        ci_overrides={},
                        timeout=75,
                        env_vars=env_vars,
                    ),
                    defaults,
                    0,
                ),
                normalize(
                    SimpleNamespace(
                        test_name="dispatch_smoke_16p",
                        test_descr="16P smoke",
                        nnodes=2,
                        ngpu=2,
                        ci_kind="npu_smoke",
                        ci_overrides={"docker": None},
                        timeout=75,
                        env_vars={"TORCH_NPU_EXTRA": "1"},
                    ),
                    defaults,
                    1,
                ),
                normalize(
                    SimpleNamespace(
                        test_name="dispatch_smoke_8p",
                        test_descr="8P smoke",
                        nnodes=1,
                        ngpu=2,
                        ci_kind="npu_smoke",
                        ci_overrides={},
                        timeout=75,
                        env_vars=env_vars,
                    ),
                    defaults,
                    2,
                ),
                normalize(
                    SimpleNamespace(
                        test_name="training_defaults",
                        test_descr="Training with deployment defaults",
                        nnodes=1,
                        ngpu=2,
                        ci_kind="train",
                        ci_overrides={"docker": None},
                        timeout=90,
                        env_vars={"DATASET_PATH": "tests/assets/fixtures"},
                    ),
                    defaults,
                    3,
                ),
            ]
            run_id = "run_multi_1"
            sha = "c" * 40
            plan = create_plan(
                cases,
                run_id=run_id,
                attempt=1,
                sha=sha,
                workflow="dispatcher_smoke.yml",
                defaults=defaults,
                repo=gitcode.repo,
                provider="gitcode",
            )
            details = {
                "workflow_run_id": run_id,
                "attempt": 1,
                "event": "MR",
                "workflow_name": "lite-ci-dispatcher-smoke",
                "status": "RUNNING",
                "head_sha": sha,
                "head_branch": "master",
                "file_path": ".gitcode/workflows/dispatcher_smoke.yml",
                "pull_request_id": "1344",
                "actor": {"login": "depeng1994"},
                "start_time": 1767225600,
                "run_number": 1,
                "stages": [
                    {
                        "jobs": [
                            {
                                "name": "Dispatcher Smoke",
                                "status": "RUNNING",
                                "last_dispatch_id": "dispatch_99",
                                "start_time": 1767225600000,
                            }
                        ]
                    }
                ],
            }
            pr = {
                "number": 1344,
                "state": "open",
                "head": {
                    "ref": "master",
                    "sha": sha,
                    "repo": {"full_name": "depeng1994/torchtitan-npu"},
                },
                "base": {"repo": {"full_name": gitcode.repo}},
            }
            artifact_ready = False
            comments = []
            seen_envs = {}

            def request(method, path, **kwargs):
                if path.startswith("/actions/runs?"):
                    return {"workflow_runs": [details]}
                if path == f"/actions/runs/{run_id}":
                    return details
                if path == "/pulls/1344":
                    return pr
                if path == f"/actions/runs/{run_id}/artifacts?per_page=100":
                    return {
                        "artifacts": (
                            [
                                {
                                    "id": "req_zip_1",
                                    "name": "lite-ci-request-1",
                                    "workflow_run_id": run_id,
                                    "size_bytes": 1500,
                                }
                            ]
                            if artifact_ready
                            else []
                        )
                    }
                raise AssertionError((method, path))

            def download_zip(artifact_id, limit, *, category=None):
                self.assertEqual(artifact_id, "req_zip_1")
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as archive:
                    archive.writestr(
                        "tmp/lite-ci-request/ci-request.json", json.dumps(plan)
                    )
                return buf.getvalue()

            def execute(selected, current, **kwargs):
                self.assertEqual(selected["docker"], "training")
                seen_envs[current["_ci_case_id"]] = selected["verified_env"]
                current["_ci_phase_callback"]("LAUNCHING")
                return completed_execution(root, selected, current)

            def send(target_sha, body, **kwargs):
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
                        "LITE_ACTIONS_GITCODE_ALLOWED_ACTORS": "depeng1994",
                        "LITE_ACTIONS_GITCODE_ALLOWED_BRANCHES": "master",
                        "LITE_ACTIONS_GITCODE_WORKFLOWS": "dispatcher_smoke.yml",
                        "LITE_ACTIONS_STATE_DIR": "",
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "locks"),
                    },
                ),
                patch.object(gitcode, "request", side_effect=request),
                patch.object(
                    gitcode, "_download_artifact_zip", side_effect=download_zip
                ),
                patch.object(
                    gitcode,
                    "workflow_status",
                    return_value={"status": "in_progress", "run_attempt": 1},
                ),
                patch.object(
                    gitcode,
                    "commit_comments",
                    side_effect=lambda *args, **kwargs: comments,
                ),
                patch.object(gitcode, "send_result", side_effect=send),
                patch(
                    "ci_core.router.verify_definition",
                    side_effect=AssertionError("unexpected catalog check"),
                ),
                patch(
                    "ci_core.registry.inspect_archive",
                    side_effect=AssertionError("unexpected archive inspect"),
                ),
                patch("ci_core.router.execute", side_effect=execute),
            ):
                router = Router(
                    root,
                    "blue",
                    MultiPlatformClient(None, {gitcode.repo: gitcode}),
                    "reporter",
                )
                try:
                    router.tick()
                    self.assertEqual(router.state.read(), {})
                    artifact_ready = True
                    router.tick()
                    for _ in range(20):
                        for future, _, _ in list(router.active.values()):
                            future.result(10)
                        router.pump()
                        router.flush()
                        if (
                            not router.active
                            and len(router.state.read()) == 4
                            and all(
                                r["phase"] == "REPORTED"
                                for r in router.state.read().values()
                            )
                        ):
                            break
                    records = list(router.state.read().values())
                    self.assertEqual(len(records), 4)
                    self.assertTrue(
                        all(r["report"]["status"] == "PASS" for r in records)
                    )
                    self.assertEqual(
                        seen_envs,
                        {
                            "case_000_dispatch_smoke_8p": env_vars,
                            "case_001_dispatch_smoke_16p": {
                                **env_vars,
                                "ASCEND_SET_ENV_PATH": "/fixture/cann",
                            },
                            "case_002_dispatch_smoke_8p": env_vars,
                            "case_003_training_defaults": {
                                "DATASET_PATH": "tests/assets/fixtures",
                                "ASCEND_SET_ENV_PATH": "/fixture/cann",
                                "HF_ASSETS_PATH": "/fixture/model",
                            },
                        },
                    )
                    results = [
                        json.loads(c["body"].split("\n", 1)[1])
                        for c in comments
                        if c["body"].startswith("Lite-CI-V3 ")
                    ]
                    self.assertEqual(
                        [
                            r["case_id"]
                            for r in results
                            if r["test_id"] != "training_defaults"
                        ],
                        [
                            "case_000_dispatch_smoke_8p",
                            "case_001_dispatch_smoke_16p",
                            "case_002_dispatch_smoke_8p",
                        ],
                    )
                finally:
                    router.close()

    def test_runtime_pool_default_requires_unambiguous_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_workspace(root)
            pools = json.loads((root / "config/pools.json").read_text())
        pool = pools["pool_blue"]
        item = {
            "target_pool": "pool_blue",
            "case_id": "case_000_train",
            "kind": "train",
            "execution_timeout_seconds": 75,
            "resources": {"nodes": 1, "devices_per_node": 2},
            "docker": None,
            "env_vars": {},
        }
        self.assertEqual(runtime_case(item, pools, "blue")["docker"], "training")
        node = pool["nodes"][1]
        node["environments"]["another"] = node["environments"]["training"]
        with self.assertRaisesRegex(ValueError, "DEFAULT_DOCKER_AMBIGUOUS"):
            runtime_case(item, pools, "blue")
        self.assertEqual(
            runtime_case({**item, "docker": "training"}, pools, "blue")["docker"],
            "training",
        )
        pool["default_docker"] = "another"
        self.assertEqual(runtime_case(item, pools, "blue")["docker"], "another")
        del pool["default_docker"]
        node["paused"] = True
        self.assertEqual(runtime_case(item, pools, "blue")["docker"], "training")
        del node["paused"]
        node["capabilities"] = ["npu_smoke"]
        del node["environments"]["training"]
        self.assertEqual(runtime_case(item, pools, "blue")["docker"], "training")
        with self.assertRaisesRegex(ValueError, "DEFAULT_DOCKER_AMBIGUOUS"):
            runtime_case({**item, "kind": "npu_smoke"}, pools, "blue")
        pool["nodes"][0]["capabilities"] = ["npu_smoke"]
        with self.assertRaisesRegex(ValueError, "CONTAINER_BINDING_MISSING"):
            runtime_case(item, pools, "blue")

    def test_gitcode_request_artifact_rejects_worker_probe_and_unsafe_env(self):
        defaults = {
            "worker": "blue",
            "target_pool": "pool_blue",
            "docker": "training",
        }
        with self.assertRaisesRegex(ValueError, "probe cannot define environment"):
            normalize(
                SimpleNamespace(
                    test_name="worker_os_blue",
                    test_descr="probe",
                    nnodes=0,
                    ngpu=0,
                    ci_kind="worker_probe",
                    ci_overrides={"worker": "blue"},
                    timeout=90,
                    env_vars={"FOO": "bar"},
                ),
                defaults,
                0,
            )

        run_ctx = {
            "_ci_provider": "gitcode",
            "id": "run_bad_env",
            "run_attempt": 1,
            "head_sha": "d" * 40,
        }
        for bad_env in (
            {"ASCEND_SET_ENV_PATH": "/fixture/cann", "ASCEND_RT_VISIBLE_DEVICES": "0"},
            {"ASCEND_SET_ENV_PATH": "/fixture/cann", "BASH_ENV": "/tmp/evil"},
            {"ASCEND_SET_ENV_PATH": "/fixture/cann", "LD_PRELOAD": "/tmp/lib.so"},
            {"ASCEND_SET_ENV_PATH": "/fixture/cann\nrm -rf /"},
            {"ASCEND_SET_ENV_PATH": "relative/set_env.sh"},
        ):
            with self.subTest(bad_env=bad_env):
                raw_case = normalize(
                    SimpleNamespace(
                        test_name="smoke_bad",
                        test_descr="bad",
                        nnodes=1,
                        ngpu=2,
                        ci_kind="npu_smoke",
                        ci_overrides={},
                        timeout=90,
                    ),
                    defaults,
                    0,
                )
                raw_case["env_vars"] = bad_env
                plan = {
                    "schema": 3,
                    "repo": "cann/torchtitan-npu",
                    "workflow": "dispatcher_smoke.yml",
                    "run_id": run_ctx["id"],
                    "attempt": 1,
                    "sha": run_ctx["head_sha"],
                    "defaults": defaults,
                    "cases": [raw_case],
                }
                plan["plan_digest"] = digest(plan)
                with self.assertRaises(ValueError):
                    parse_plan(
                        plan,
                        run_ctx,
                        "dispatcher_smoke.yml",
                        repo="cann/torchtitan-npu",
                    )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_workspace(root)
            gitcode = GitCodeClient("unused-token", "cann/torchtitan-npu")
            for unapproved_env in (
                {"ASCEND_SET_ENV_PATH": "/unapproved/set_env.sh"},
                {
                    "ASCEND_SET_ENV_PATH": "/fixture/cann",
                    "HF_ASSETS_PATH": "/unapproved/model",
                },
            ):
                with self.subTest(unapproved_env=unapproved_env):
                    case = normalize(
                        SimpleNamespace(
                            test_name="smoke_unapproved",
                            test_descr="unapproved",
                            nnodes=1,
                            ngpu=2,
                            ci_kind="npu_smoke",
                            ci_overrides={},
                            timeout=90,
                            env_vars=unapproved_env,
                        ),
                        defaults,
                        0,
                    )
                    run = {
                        "_ci_provider": "gitcode",
                        "_ci_repo": gitcode.repo,
                        "id": "run_unapproved",
                        "run_attempt": 1,
                        "head_sha": "e" * 40,
                        "path": ".gitcode/workflows/dispatcher_smoke.yml",
                        "event": "Manual",
                        "status": "in_progress",
                        "head_branch": "master",
                        "actor": {"login": "maintainer"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "name": "lite-ci-dispatcher-smoke",
                        "run_number": 1,
                    }
                    plan = create_plan(
                        [case],
                        run_id=run["id"],
                        attempt=1,
                        sha=run["head_sha"],
                        workflow="dispatcher_smoke.yml",
                        defaults=defaults,
                        repo=gitcode.repo,
                        provider="gitcode",
                    )
                    comments = []
                    with (
                        patch.dict(
                            os.environ,
                            {
                                "LITE_ACTIONS_GITCODE_ALLOWED_ACTORS": "maintainer",
                                "LITE_ACTIONS_STATE_DIR": "",
                                "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "locks"),
                            },
                        ),
                        patch.object(gitcode, "running_workflows", return_value=[run]),
                        patch.object(gitcode, "ci_request", return_value=plan),
                        patch.object(gitcode, "validate_current"),
                        patch.object(
                            gitcode,
                            "workflow_status",
                            return_value={"status": "in_progress", "run_attempt": 1},
                        ),
                        patch.object(
                            gitcode,
                            "commit_comments",
                            side_effect=lambda *args, **kwargs: comments,
                        ),
                        patch.object(
                            gitcode,
                            "send_result",
                            side_effect=lambda *a, **kw: {"id": 1},
                        ),
                        patch("ci_core.router.execute") as execute_mock,
                    ):
                        router = Router(
                            root,
                            "blue",
                            MultiPlatformClient(None, {gitcode.repo: gitcode}),
                            "reporter",
                        )
                        try:
                            router.tick()
                            for future, _, _ in list(router.active.values()):
                                future.result(10)
                            router.pump()
                            router.flush()
                            execute_mock.assert_not_called()
                            records = list(router.state.read().values())
                            self.assertEqual(len(records), 1)
                            self.assertEqual(
                                records[0]["report"]["status"], "INFRA_ERROR"
                            )
                        finally:
                            router.close()

    def test_train_case_without_init_ckpt_and_suite_dispatch_job_names(self):
        from ci_core.gitcode import source_dispatch
        from ci_core.registry import validate_case_environment

        defaults = {
            "worker": "blue",
            "target_pool": "a3_shared_blue",
            "docker": "cann_master_torchtitan_master_xyj",
        }
        train_env = validate_case_environment(
            {
                "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh",
                "HF_ASSETS_PATH": "/data/models/deepseek_v4_tokenizer_only",
            },
            kind="train",
        )
        self.assertEqual(
            train_env["HF_ASSETS_PATH"], "/data/models/deepseek_v4_tokenizer_only"
        )
        with self.assertRaisesRegex(ValueError, "HF_ASSETS_PATH"):
            validate_case_environment(
                {
                    "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh",
                    "HF_ASSETS_PATH": "relative/tokenizer",
                },
                kind="train",
            )
        case = normalize(
            SimpleNamespace(
                test_name="dsv4_flash_a3_16p_cp2_ep8_eager",
                test_descr="base_01: DSV4 Flash A3 1x16P",
                nnodes=1,
                ngpu=16,
                ci_kind="train",
                ci_overrides={},
                timeout=14400,
                env_vars=train_env,
            ),
            defaults,
            0,
        )
        run_ctx = {
            "_ci_provider": "gitcode",
            "id": "run_suite_1",
            "run_attempt": 1,
            "head_sha": "f" * 40,
        }
        plan = create_plan(
            [case],
            run_id=run_ctx["id"],
            attempt=1,
            sha=run_ctx["head_sha"],
            workflow="per_pr_alltest.yml",
            defaults=defaults,
            repo="cann/torchtitan-npu",
            provider="gitcode",
        )
        parsed = parse_plan(
            plan, run_ctx, "per_pr_alltest.yml", repo="cann/torchtitan-npu"
        )
        self.assertEqual(parsed[0]["resources"], {"nodes": 1, "devices_per_node": 16})
        for job_name in ("Per-PR Alltest", "Nightly Alltest"):
            details = {
                "stages": [
                    {
                        "jobs": [
                            {
                                "name": job_name,
                                "status": "RUNNING",
                                "last_dispatch_id": "disp_1",
                                "start_time": 1767225600000,
                            }
                        ]
                    }
                ]
            }
            self.assertEqual(source_dispatch(details), "disp_1_1767225600000")
