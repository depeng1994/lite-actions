"""CPU contracts for the self-owned smoke workflow and candidate identity."""

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, TORCHTITAN_REPO, parse_plan
from ci_core.github import GitHubClient
from ci_core.reporting import body_for, report
from ci_core.router import Router
from ci_core.self_identity import checkout_sha
from ci_core.self_smoke import prepare, results, wait
from tests.integration_tests.tools.lite_actions.runner import run_dispatch_smoke
from tests.integration_tests.training_cases.dispatch_smoke import build_smoke_list
from tests.scenarios import Service, completed_execution, write_workspace
from tests.scenarios import definition as fixture_definition

ROOT = Path(__file__).resolve().parents[1]


class SelfSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {
            "GITHUB_REPOSITORY": SELF_REPO,
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "a" * 40,
        }
        with patch("ci_core.self_smoke.PLAN", Path(self.temp.name) / "plan.json"):
            self.plan = prepare(self.env)

    def test_model_copies_and_default_plan(self):
        self.assertEqual(
            [item["test_id"] for item in self.plan["cases"]],
            [
                "worker_os_blue",
                "worker_os_yellow",
                "dispatch_smoke_8p",
                "dispatch_smoke_16p",
                "dispatch_smoke_8p",
            ],
        )
        self.assertEqual(
            [
                item["resources"]["nodes"] * item["resources"]["devices_per_node"]
                for item in self.plan["cases"]
            ],
            [0, 0, 8, 16, 8],
        )
        self.assertEqual(len({item["case_id"] for item in self.plan["cases"]}), 5)
        yellow = self.plan["cases"][1]
        self.assertEqual((yellow["kind"], yellow["worker"]), ("worker_probe", "yellow"))
        self.assertEqual((yellow["target_pool"], yellow["docker"]), (None, None))
        run = {"id": 42, "run_attempt": 1, "head_sha": "a" * 40}
        with self.assertRaises(ValueError):
            parse_plan(self.plan, run, SELF_WORKFLOW, repo=TORCHTITAN_REPO)

    def test_smoke_discovery_needs_no_hardware_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-m",
                "tests.integration_tests.tools.lite_actions.entrypoint",
                "inspect",
                "--suite",
                "dispatch_smoke",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(len(json.loads(result.stdout)), 5)

    def test_complete_identity_bound_results_and_fail_closed(self):
        run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "_ci_repo": SELF_REPO,
        }
        comments = [
            {
                "user": {"login": "reporter"},
                "body": body_for(
                    run,
                    item,
                    report(run, item, self.plan["plan_digest"], "PASS", 0, [], True),
                ),
            }
            for item in self.plan["cases"]
        ]
        self.assertEqual(len(results(comments, self.plan, "reporter")), 5)
        # Four Blue results cannot satisfy the missing Yellow instance.
        blue_only = comments[:1] + comments[2:]
        self.assertNotIn(
            self.plan["cases"][1]["case_id"], results(blue_only, self.plan, "reporter")
        )
        self.assertEqual(len(results(blue_only, self.plan, "reporter")), 4)
        duplicate = copy.deepcopy(comments[-1])
        marker, payload = duplicate["body"].split("\n", 1)
        result = json.loads(payload)
        result["cleanup_verified"] = 1  # Equal to True in Python, not in JSON.
        duplicate["body"] = marker + "\n" + json.dumps(result)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            results([duplicate, *comments], self.plan, "reporter")
        for mutation in ("sha", "unknown"):
            bad = copy.deepcopy(comments)
            if mutation == "unknown":
                bad.append(
                    {
                        "user": {"login": "reporter"},
                        "body": "Lite-CI-V3 42:1:unexpected\n{}",
                    }
                )
            else:
                marker, payload = bad[-1]["body"].split("\n", 1)
                result = json.loads(payload)
                result["sha"] = "b" * 40
                bad[-1]["body"] = marker + "\n" + json.dumps(result)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                results(bad, self.plan, "reporter")

    def test_failure_reports_case_and_never_waits_for_missing_cases(self):
        run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "_ci_repo": SELF_REPO,
        }
        comments = []
        for item in self.plan["cases"][:2]:
            result = report(run, item, self.plan["plan_digest"], "PASS", 0, [], True)
            if comments:
                result["cleanup_verified"] = False
            comments.append(
                {"user": {"login": "reporter"}, "body": body_for(run, item, result)}
            )
        plan_path = Path(self.temp.name) / "plan.json"
        plan_path.write_text(json.dumps(self.plan))
        output = io.StringIO()
        client = Mock()
        client.commit_comments.return_value = comments
        env = {**self.env, "CI_RESULT_AUTHOR": "reporter", "GH_TOKEN": "token"}
        with (
            patch("ci_core.self_smoke.PLAN", plan_path),
            patch("ci_core.self_smoke.GitHubClient", return_value=client),
            redirect_stdout(output),
            self.assertRaisesRegex(ValueError, "self-smoke cases failed"),
        ):
            wait(env)
        self.assertIn(f"{self.plan['cases'][0]['case_id']}: PASS", output.getvalue())
        self.assertIn("cleanup_verified=False", output.getvalue())

    def test_full_follows_declared_profile_without_required_worker_names(self):
        from tests.integration_tests.training_cases.dispatch_smoke import SMOKE_PROFILES

        # A differently sized profile, with no Yellow and repeated NPU instances.
        names = ["dispatch_smoke_8p", "worker_os_blue", "dispatch_smoke_8p"]
        with (
            patch.dict(SMOKE_PROFILES, dispatch_smoke=names),
            patch("ci_core.self_smoke.PLAN", Path(self.temp.name) / "plan.json"),
        ):
            plan = prepare(self.env)
        self.assertEqual(
            [c["case_id"] for c in plan["cases"]],
            [f"case_{i:03d}_{name}" for i, name in enumerate(names)],
        )

    def test_matrix_output_is_the_selected_plan(self):
        # Job IDs come from the selected plan, including repeated 8P instances.
        for profile, size in (("full", 5), ("cpu-only", 1)):
            with self.subTest(profile=profile):
                output = Path(self.temp.name) / (profile + ".out")
                env = {
                    **self.env,
                    "GITHUB_OUTPUT": str(output),
                    "SELF_SMOKE_PROFILE": profile,
                }
                with patch(
                    "ci_core.self_smoke.PLAN", Path(self.temp.name) / "plan.json"
                ):
                    plan = prepare(env)
                key, value = output.read_text().strip().split("=", 1)
                self.assertEqual(key, "matrix")
                self.assertEqual(json.loads(value), {"include": plan["cases"]})
                self.assertEqual(len({c["case_id"] for c in plan["cases"]}), size)

    def test_matrix_receipts_and_final_gate(self):
        run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": self.env["GITHUB_SHA"],
            "path": SELF_WORKFLOW,
            "_ci_repo": SELF_REPO,
        }
        env = {**self.env, "GH_TOKEN": "fixture", "CI_RESULT_AUTHOR": "reporter"}
        rows = [
            {
                "user": {"login": "reporter"},
                "body": body_for(
                    run,
                    item,
                    report(
                        run,
                        item,
                        self.plan["plan_digest"],
                        "PASS",
                        0,
                        [
                            f"WORKER_HOSTNAME=sample-{item['worker']}",
                            "WORKER_CPU_ONLINE=17",
                        ],
                        True,
                    ),
                ),
            }
            for item in self.plan["cases"]
        ]
        yellow = self.plan["cases"][1]["case_id"]
        ids = [c["case_id"] for c in self.plan["cases"] if c["worker"] == "blue"]
        blue = rows[:1] + rows[2:]
        client = Mock()
        with (
            patch("ci_core.self_smoke.PLAN", Path(self.temp.name) / "plan.json"),
            patch("ci_core.self_smoke.GitHubClient", return_value=client),
            redirect_stdout(io.StringIO()) as out,
        ):
            client.commit_comments.return_value = blue
            for case_id in ids:
                wait(env, only=case_id, once=True)
            for only in (yellow, None):
                with self.assertRaisesRegex(TimeoutError, yellow):
                    wait(env, only=only, once=True)
            self.assertIn("Waiting for yellow Worker receipt", out.getvalue())
            with self.assertRaisesRegex(ValueError, "case selector"):
                wait(env, only="", once=True)
            forged = copy.deepcopy(rows[1])
            marker, payload = forged["body"].split("\n", 1)
            data = json.loads(payload)
            for key, value in [
                ("worker", "blue"),
                ("sha", "f" * 40),
                ("plan_digest", "f" * 64),
                ("attempt", 2),
            ]:
                client.commit_comments.return_value = blue + [
                    {**forged, "body": marker + "\n" + json.dumps({**data, key: value})}
                ]
                with (
                    self.subTest(field=key),
                    self.assertRaisesRegex(ValueError, "identity mismatch"),
                ):
                    wait(env, only=yellow, once=True)
            client.commit_comments.return_value = blue + [
                {
                    **forged,
                    "body": marker
                    + "\n"
                    + json.dumps({**data, "cleanup_verified": False}),
                }
            ]
            wait(env, only=ids[0], once=True)
            for only in (yellow, None):
                with self.assertRaisesRegex(ValueError, "self-smoke cases failed"):
                    wait(env, only=only, once=True)
            out.seek(0)
            out.truncate()
            client.commit_comments.return_value = blue + [
                {**forged, "user": {"login": "someone-else"}}
            ]
            with self.assertRaisesRegex(TimeoutError, yellow):
                wait(env, only=yellow, once=True)
            self.assertNotIn(
                "WORKER_HOSTNAME=sample-yellow", out.getvalue().splitlines()
            )
            client.commit_comments.return_value = rows
            wait(env, only=yellow, once=True)
            wait(env, once=True)
            self.assertEqual(
                out.getvalue().splitlines().count("WORKER_HOSTNAME=sample-yellow"), 1
            )
            self.assertIn("WORKER_CPU_ONLINE=17", out.getvalue().splitlines())

    def test_launch_consumes_complete_two_node_lease(self):
        case = next(
            case
            for case in build_smoke_list()
            if case.test_name == "dispatch_smoke_16p"
        )
        env = {
            "NODE_IPS": "192.0.2.1,192.0.2.2",
            "NODE_RANK": "1",
            "NNODES": "2",
            "NGPU": "8",
            "LITE_CI_JOB_ID": "fixture",
            "MASTER_PORT": "16001",
            "HCCL_IF_BASE_PORT": "20128",
            "HCCL_HOST_SOCKET_PORT_RANGE": "24128-24191",
            "HCCL_NPU_SOCKET_PORT_RANGE": "28128-28191",
        }
        with patch.dict(os.environ, env, clear=True), patch("subprocess.run") as run:
            run_dispatch_smoke(case)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--node-rank") + 1], "1")
            self.assertEqual(command[command.index("--nproc-per-node") + 1], "8")
            self.assertEqual(run.call_args.kwargs["env"]["MASTER_PORT"], "16001")
            del os.environ["HCCL_NPU_SOCKET_PORT_RANGE"]
            with self.assertRaises(RuntimeError):
                run_dispatch_smoke(case)

    def test_native_plan_routes_and_reports_each_instance(self):
        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        path = root / "config/pools.json"
        pools = json.loads(path.read_text())
        pool = pools.pop("pool_blue")
        for node in pool["nodes"]:
            node["device_ids"] = list(range(16))
            node["allocation_groups"] = [
                {"devices": list(range(8)), "verified": True},
                {"devices": list(range(8, 16)), "verified": True},
            ]
            node["environments"]["cann_master_torchtitan_master_xyj"] = node[
                "environments"
            ].pop("training")
        pool["verified_multinode_allocations"] = [
            [
                {"host_id": node["host_id"], "devices": list(range(8))}
                for node in pool["nodes"]
            ]
        ]
        pools["a3_shared_blue"] = pool
        path.write_text(json.dumps(pools))
        service = Service([fixture_definition("dummy")], root)
        service.repo = SELF_REPO
        service.runs = {
            42: {
                "id": 42,
                "run_attempt": 1,
                "head_sha": "a" * 40,
                "path": ".github/workflows/" + SELF_WORKFLOW,
                "created_at": "2026-01-01T00:00:00Z",
                "status": "in_progress",
                "event": "workflow_dispatch",
                "head_branch": "ci/test",
                "actor": {"login": "maintainer"},
                "name": "lite-ci-self-smoke",
                "run_number": 42,
            }
        }
        service.plans = {42: self.plan}
        with tarfile.open(service.archive, "w:gz") as tar:
            for name in (
                "tests/__init__.py",
                "tests/integration_tests/__init__.py",
                "tests/integration_tests/training_cases/dispatch_smoke.py",
                "tests/integration_tests/tools/lite_actions/entrypoint.py",
                "tests/integration_tests/tools/lite_actions/runner.py",
            ):
                tar.add(ROOT / name, arcname=name)
        env = {
            "LITE_ACTIONS_MODEL_REPOS": SELF_REPO,
            "LITE_ACTIONS_SELF_SMOKE_ENABLED": "1",
            "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
            "LITE_ACTIONS_STATE_DIR": "",
        }

        def execute(case, run, **kwargs):
            run["_ci_phase_callback"]("LAUNCHING")
            return completed_execution(root, case, run)

        with (
            patch.dict(os.environ, env),
            patch("ci_core.self_identity.checkout_sha", return_value="a" * 40),
            service.connected(),
            patch("ci_core.router.execute", side_effect=execute),
        ):
            router = Router(root, "blue", service, "reporter")
            yellow = Router(root, "yellow", service, "reporter")
            try:
                for _ in range(20):
                    for worker in (router, yellow):
                        worker.tick()
                        for future, _, _ in list(worker.active.values()):
                            future.result(10)
                    if len(service.results()) == 5:
                        break
                self.assertEqual(
                    len(results(service.comments, self.plan, "reporter")), 5
                )
            finally:
                yellow.close()
                router.close()

    def test_worker_checkout_and_manual_admission_are_sha_bound(self):
        root = Path(self.temp.name) / "checkout"

        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args])

        (root / "ci_core").mkdir(parents=True)
        (root / "ci_core/core.py").write_text("VALUE = 1\n")
        (root / "unified_router.py").write_text("pass\n")
        for name in (
            "tests/__init__.py",
            "tests/integration_tests/__init__.py",
            "tests/integration_tests/tools/lite_actions/entrypoint.py",
        ):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        for args in (
            ["init", "-q"],
            ["add", "."],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "fixture",
            ],
        ):
            git(*args)
        sha = checkout_sha(root)
        # Plain `git status` does not report ignored Python modules, even
        # though they can take precedence over namespace packages at runtime.
        ignored = root / "tests/integration_tests/tools/__init__.py"
        (root / ".git/info/exclude").write_text(
            "tests/integration_tests/tools/__init__.py\n"
        )
        ignored.write_text("raise RuntimeError('ignored source')\n")
        with self.assertRaises(ValueError):
            checkout_sha(root)
        ignored.unlink()
        external = Path(self.temp.name) / "external"
        shutil.copytree(root / "ci_core", external)
        saved = root / "core_saved"
        (root / "ci_core").rename(saved)
        (root / "ci_core").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "linked Worker source"):
            checkout_sha(root)
        (root / "ci_core").unlink()
        saved.rename(root / "ci_core")
        router = Router.__new__(Router)
        router.root, router.self_sha = root, sha
        run = {
            "_ci_repo": SELF_REPO,
            "name": "lite-ci-self-smoke",
            "event": "workflow_dispatch",
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "status": "in_progress",
            "actor": {"login": "maintainer"},
            "head_branch": "ci/test",
            "head_sha": sha,
        }
        self.assertTrue(router.eligible(run))
        for field, value in (
            ("name", "unrelated"),
            ("head_sha", "b" * 40),
        ):
            self.assertFalse(router.eligible({**run, field: value}))
        git("update-index", "--assume-unchanged", "ci_core/core.py")
        (root / "ci_core/core.py").write_text("VALUE = 2\n")
        with self.assertRaises(ValueError):
            router.eligible(run)

    def test_model_only_yellow_discovers_self_run_and_acks_its_own_case(self):
        # Start at the CLI: injecting a ready-made self-repo client hides this bug.
        import unified_router

        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        run = dict(
            id=42,
            run_attempt=1,
            head_sha="a" * 40,
            path=f".github/workflows/{SELF_WORKFLOW}",
            status="in_progress",
            name="Lite Actions self smoke: full",
            display_title="Lite Actions self smoke: full",
            repository={"full_name": SELF_REPO},
        )
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ci-request.json", json.dumps(self.plan))
        reads, writes = [], []

        def request(client, method, path, data=None):
            if method == "POST":
                writes.append((client.repo, path, data["body"]))
                return {"id": 1}
            reads.append((client.repo, path))
            if client.repo == TORCHTITAN_REPO and path.startswith("/actions/runs?"):
                return {"workflow_runs": []}
            if client.repo == SELF_REPO and path.startswith(
                f"/actions/workflows/{SELF_WORKFLOW}/runs?"
            ):
                return {
                    "workflow_runs": [
                        run,
                        dict(run, id=43, head_sha="b" * 40),
                        dict(run, id=44, path=".github/workflows/other.yml"),
                    ]
                }
            if client.repo == SELF_REPO and path.startswith(
                "/actions/runs/42/artifacts?"
            ):
                return {
                    "artifacts": [
                        {
                            "id": 5,
                            "name": "lite-ci-request-1",
                            "workflow_run": {"id": 42},
                        }
                    ]
                }
            raise AssertionError((client.repo, method, path))

        with (
            patch.dict(
                os.environ,
                {
                    "LITE_ACTIONS_MODEL_REPOS": TORCHTITAN_REPO,
                    "LITE_ACTIONS_SELF_SMOKE_ENABLED": "1",
                    "LITE_ACTIONS_RESULT_AUTHOR": "reporter",
                },
                clear=True,
            ),
            patch.object(sys, "argv", ["router", "--worker-id", "yellow", "--once"]),
            patch.object(unified_router, "ROOT", root),
            patch.object(unified_router, "single_agent"),
            patch.object(unified_router, "authorization", return_value="fixture"),
            patch.object(unified_router.signal, "signal"),
            patch("ci_core.self_identity.checkout_sha", return_value="a" * 40),
            patch.object(GitHubClient, "request", request),
            patch.object(
                GitHubClient, "download_artifact", return_value=archive.getvalue()
            ) as download,
            patch.object(
                Router, "pump"
            ),  # stop after durable admission + actual ACK construction
        ):
            unified_router.main()
        self.assertEqual(len(writes), 1)
        repo, path, body = writes[0]
        self.assertEqual(
            (repo, path), (SELF_REPO, "/commits/" + run["head_sha"] + "/comments")
        )
        ack = json.loads(body.removeprefix("Lite-CI-PROGRESS "))
        self.assertEqual(
            (ack["worker"], ack["phase"], ack["case_id"], ack["plan_digest"]),
            ("yellow", "QUEUED", "case_001_worker_os_yellow", self.plan["plan_digest"]),
        )
        download.assert_called_once_with(5, limit=262144)
        self.assertEqual(
            [path for repo, path in reads if repo == SELF_REPO],
            [
                f"/actions/workflows/{SELF_WORKFLOW}/runs?status=in_progress&per_page=100&page=1",
                "/actions/runs/42/artifacts?name=lite-ci-request-1&per_page=10",
            ],
        )

    def test_cli_preserves_disabled_and_explicit_discovery(self):
        import unified_router

        for model_repos, opt_in, gh_on, expected in [
            (TORCHTITAN_REPO, "0", "true", [TORCHTITAN_REPO]),
            (
                f"{TORCHTITAN_REPO},{SELF_REPO}",
                "1",
                "true",
                [TORCHTITAN_REPO, SELF_REPO],
            ),
            (TORCHTITAN_REPO, "1", "false", []),
        ]:
            env = {
                "LITE_ACTIONS_MODEL_REPOS": model_repos,
                "LITE_ACTIONS_SELF_SMOKE_ENABLED": opt_in,
                "LITE_ACTIONS_GITHUB_ENABLED": gh_on,
                "LITE_ACTIONS_GITCODE_REPOS": "cann/torchtitan-npu",
                "LITE_ACTIONS_GITCODE_TOKEN": "fixture",
            }
            with (
                self.subTest(env=(model_repos, opt_in, gh_on)),
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    sys, "argv", ["router", "--worker-id", "yellow", "--once"]
                ),
                patch.object(unified_router, "load"),
                patch.object(unified_router, "single_agent"),
                patch.object(unified_router.signal, "signal"),
                patch.object(unified_router, "Router") as router,
                patch.object(
                    unified_router, "authorization", return_value="fixture"
                ) as auth,
                patch.object(
                    GitHubClient,
                    "request",
                    autospec=True,
                    return_value={"workflow_runs": []},
                ) as http,
            ):
                unified_router.main()
                multi = router.call_args.args[2]
                self.assertEqual(list(multi.gitcode), ["cann/torchtitan-npu"])
                if multi.github:
                    multi.github.running_workflows()
                self.assertEqual(auth.call_count, int(gh_on == "true"))
                # Explicitly subscribed repos still use their existing two-status list.
                self.assertEqual(
                    [c.args[0].repo for c in http.call_args_list],
                    [r for r in expected for _ in range(2)],
                )
                self.assertTrue(
                    all(
                        c.args[2].startswith("/actions/runs?")
                        for c in http.call_args_list
                    )
                )

    def test_explicit_self_smoke_does_not_require_repo_subscription(self):
        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        service = Service([fixture_definition("dummy")], root)
        env = {
            "LITE_ACTIONS_MODEL_REPOS": TORCHTITAN_REPO,
            "LITE_ACTIONS_SELF_SMOKE_ENABLED": "1",
            "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
            "LITE_ACTIONS_STATE_DIR": "",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("ci_core.self_identity.checkout_sha", return_value="a" * 40),
        ):
            router = Router(root, "yellow", service, "reporter")
            try:
                self.assertEqual(router.self_sha, "a" * 40)
            finally:
                router.close()


if __name__ == "__main__":
    unittest.main()
