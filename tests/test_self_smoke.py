"""CPU contracts for the self-owned smoke workflow and candidate identity."""

import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.contract import (
    PR_SMOKE_WORKFLOW,
    SELF_REPO,
    SELF_WORKFLOW,
    TORCHTITAN_REPO,
    VLLM_REPO,
    configured_repos,
    normalize,
    parse_plan,
)
from ci_core.egress_policy import Egress, Rejected
from ci_core.github import MultiRepoGitHubClient
from ci_core.plan import verify_definition
from ci_core.reporting import body_for, report
from ci_core.resources import PhysicalResourceCoordinator
from ci_core.router import Router
from ci_core.self_smoke import prepare, prepare_pr, prepare_pr_cleanup, results, wait
from ci_core.test_broker import TestBrokerServer
from ci_core.test_supervisor import build_systemd_command
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
                "dispatch_smoke_yellow_a3_8p",
            ],
        )
        self.assertEqual(
            [
                item["resources"]["nodes"] * item["resources"]["devices_per_node"]
                for item in self.plan["cases"]
            ],
            [0, 0, 8, 16, 8, 8],
        )
        self.assertEqual(len({item["case_id"] for item in self.plan["cases"]}), 6)
        yellow = self.plan["cases"][1]
        self.assertEqual((yellow["kind"], yellow["worker"]), ("worker_probe", "yellow"))
        self.assertEqual((yellow["target_pool"], yellow["docker"]), (None, None))
        yellow_a3 = self.plan["cases"][5]
        self.assertEqual(
            (yellow_a3["worker"], yellow_a3["target_pool"], yellow_a3["docker"]),
            ("yellow", "a3_shared_yellow", "torchtitan_ci_a3_r2"),
        )
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
        self.assertEqual(len(json.loads(result.stdout)), 6)

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
        self.assertEqual(len(results(comments, self.plan, "reporter")), 6)
        # Four Blue results cannot satisfy the missing Yellow instance.
        blue_only = comments[:1] + comments[2:5]
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
        for profile, size in (("full", 6), ("cpu-only", 1)):
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

        yellow_node = copy.deepcopy(pool["nodes"][0])
        yellow_node["host_id"] = "192.0.2.30:22"
        yellow_node["ssh"] = "192.0.2.30:22"
        yellow_node["node_ip"] = "192.0.2.30"
        yellow_node["device_ids"] = list(range(8))
        yellow_node["allocation_groups"] = [
            {"devices": list(range(8)), "verified": True}
        ]
        yellow_node["environments"] = {
            "torchtitan_ci_a3_r2": {
                "container_id": "c" * 64,
                "image_id": "sha256:" + "d" * 64,
                "image_ref": "torchtitan_ci_a3_r2:fixture",
            }
        }
        pools["a3_shared_yellow"] = {
            "owner": "yellow",
            "type": "ascend-a3",
            "enabled": True,
            "nodes": [yellow_node],
            "process_indexing": "card-die",
            "max_jobs_per_host": 1,
            "verified_multinode_allocations": [],
        }
        path.write_text(json.dumps(pools))

        workers_path = root / "config/workers.json"
        workers = json.loads(workers_path.read_text())
        workers["yellow"]["case_environments"]["torchtitan_ci_a3_r2"] = {
            "ASCEND_SET_ENV_PATH": [
                "/fixture/cann",
                "/usr/local/Ascend/ascend-toolkit/set_env.sh",
            ],
            "HF_ASSETS_PATH": ["/fixture/model"],
            "CKPT_INIT_LOAD_PATH": ["/fixture/checkpoint"],
        }
        workers_path.write_text(json.dumps(workers))

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
                "head_branch": "main",
                "actor": {"login": "maintainer"},
                "name": "Lite Actions self smoke: full",
                "workflow_id": 50,
                "run_number": 42,
            }
        }
        service.plans = {42: self.plan}
        with tarfile.open(service.archive, "w:gz") as tar:
            tar.add(ROOT / ".ci/lite-actions.json", arcname=".ci/lite-actions.json")
            for name in (
                "tests/__init__.py",
                "tests/integration_tests/__init__.py",
                "tests/integration_tests/training_cases/dispatch_smoke.py",
                "tests/integration_tests/tools/lite_actions/entrypoint.py",
                "tests/integration_tests/tools/lite_actions/runner.py",
            ):
                tar.add(ROOT / name, arcname=name)
        env = {
            "LITE_ACTIONS_MODEL_REPOS": f"{VLLM_REPO},{SELF_REPO}",
            "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
            "LITE_ACTIONS_STATE_DIR": "",
        }

        def execute(case, run, **kwargs):
            run["_ci_phase_callback"]("LAUNCHING")
            return completed_execution(root, case, run)

        with (
            patch.dict(os.environ, env),
            service.connected(),
            patch("ci_core.router.execute", side_effect=execute),
        ):
            # Exercise normal multi-repo discovery, not an injected self-smoke run.
            client = MultiRepoGitHubClient("fixture", configured_repos())

            def self_api(method, path, data=None, **kwargs):
                if path == "/actions/workflows/50":
                    return {"name": "lite-ci-self-smoke"}
                return service.request(method, path, data, **kwargs)

            own = self.enterContext(
                patch.object(
                    client.for_repo(SELF_REPO), "request", side_effect=self_api
                )
            )
            model = self.enterContext(
                patch.object(
                    client.for_repo(VLLM_REPO),
                    "request",
                    return_value={"workflow_runs": []},
                )
            )
            router = Router(root, "blue", client, "reporter")
            with patch.dict(
                os.environ, LITE_ACTIONS_STATE_DIR=str(root / "yellow-state")
            ):
                yellow = Router(root, "yellow", client, "reporter")
            try:
                for _ in range(20):
                    for worker in (router, yellow):
                        worker.tick()
                        for future, _, _ in list(worker.active.values()):
                            future.result(10)
                    if len(service.results()) == 6:
                        break
                accepted = results(service.comments, self.plan, "reporter")
                self.assertEqual(len(accepted), 6)
                self.assertTrue(
                    all(
                        r["status"] == "PASS"
                        and r["exit_code"] == 0
                        and r["cleanup_verified"] is True
                        for r in accepted.values()
                    )
                )
                self.assertEqual(
                    [r["item"]["case_id"] for r in yellow.state.read().values()],
                    [
                        "case_001_worker_os_yellow",
                        "case_005_dispatch_smoke_yellow_a3_8p",
                    ],
                )
                queued = ("GET", "/actions/runs?status=queued&per_page=100&page=1")
                own.assert_any_call(*queued)
                model.assert_any_call(*queued)
            finally:
                yellow.close()
                router.close()

    def test_self_smoke_requires_main_branch_at_admission_and_prestart(self):
        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        service = Service([fixture_definition("dummy")], root)
        service.repo = SELF_REPO
        main_sha = "a" * 40
        older_sha = "b" * 40

        def request(method, path, payload=None, **kwargs):
            if path == "/branches/main":
                return {"commit": {"sha": main_sha}}
            if path == f"/compare/{older_sha}...{main_sha}?per_page=1":
                return {"merge_base_commit": {"sha": older_sha}}
            if path.startswith("/compare/"):
                return {"merge_base_commit": {"sha": "0" * 40}}
            return service.request(method, path, payload, **kwargs)

        run = {
            "_ci_repo": SELF_REPO,
            "id": 42,
            "run_attempt": 1,
            "name": "lite-ci-self-smoke",
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "status": "in_progress",
            "actor": {"login": "maintainer"},
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": main_sha,
            "created_at": "2026-01-01T00:00:00Z",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "LITE_ACTIONS_MODEL_REPOS": SELF_REPO,
                    "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                    "LITE_ACTIONS_STATE_DIR": "",
                    "LITE_ACTIONS_RELEASE_SHA": main_sha,
                },
            ),
            patch.object(service, "request", side_effect=request),
            patch("ci_core.router.probe_worker") as probe,
        ):
            router = Router(root, "blue", service, "reporter")
            try:
                self.assertEqual(router.control_view()["sha"], main_sha)
                self.assertTrue(router.eligible(run))
                self.assertTrue(router.eligible({**run, "head_sha": older_sha}))
                self.assertFalse(router.eligible({**run, "head_branch": "pr-branch"}))
                self.assertFalse(router.eligible({**run, "event": "pull_request"}))
                with self.assertRaisesRegex(ValueError, "not on main"):
                    router.eligible({**run, "head_sha": "c" * 40})

                # Pre-queued self-smoke on non-main ref must be rejected at work() pre-start.
                bad_run = {**run, "head_branch": "pr-branch"}
                item = self.plan["cases"][0]
                key = "bad-prequeued-self-smoke"
                router.state.admit(
                    [
                        (
                            key,
                            {
                                "phase": "QUEUED",
                                "repo": SELF_REPO,
                                "run": bad_run,
                                "item": item,
                                "digest": self.plan["plan_digest"],
                                "defaults": self.plan["defaults"],
                                "queued_at": 1.0,
                            },
                        )
                    ]
                )
                with patch.object(router, "finish") as finish:
                    router.work(key, {}, None, Mock(is_set=lambda: False), Mock())
                probe.assert_not_called()
                self.assertEqual(finish.call_args.args[1], "INFRA_ERROR")
            finally:
                router.close()

    def test_yellow_throttles_only_shared_discovery(self):
        # A waiting job must still notice cancellation and pending receipts between discoveries.
        for worker, rounds in (("yellow", 2), ("blue", 4)):
            with self.subTest(worker=worker):
                r = Mock(
                    worker=worker, next_discovery=0.0, active={}, release_logs=None
                )
                run = {"id": 42}
                r.state.read.return_value = {
                    "job": {"run": run, "phase": "QUEUED", "progress_id": 1}
                }
                status = r.client_for.return_value.workflow_status
                status.return_value = {"status": "in_progress"}
                with patch(
                    "ci_core.router.time.monotonic", side_effect=(100, 110, 159, 160)
                ):
                    for _ in range(4):
                        Router.tick(r)
                self.assertEqual(r.client.running_workflows.call_count, rounds)
                self.assertEqual(status.call_count, 4)
                self.assertEqual(r.pump.call_count, 4)
                self.assertEqual(r.flush.call_count, 8)

    def test_control_view_reports_release_sha_for_unsubscribed_workers(self):
        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        service = Service([fixture_definition("dummy")], root)
        release_sha = "d" * 40
        env = {
            "LITE_ACTIONS_MODEL_REPOS": TORCHTITAN_REPO,
            "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
            "LITE_ACTIONS_STATE_DIR": "",
            "LITE_ACTIONS_RELEASE_SHA": release_sha,
        }
        with patch.dict(os.environ, env, clear=True):
            for worker in ("yellow", "blue"):
                with self.subTest(worker=worker):
                    router = Router(root, worker, service, "reporter")
                    try:
                        self.assertEqual(router.control_view()["sha"], release_sha)
                    finally:
                        router.close()

    def test_prepare_pr_and_cleanup_plans_and_wait_selectors(self):
        pr_head = "b" * 40
        run_sha = "c" * 40
        for profile, expected_smoke in (("full", 6), ("cpu-only", 1)):
            with self.subTest(profile=profile):
                out = Path(self.temp.name) / f"{profile}.out"
                plan_path = Path(self.temp.name) / f"{profile}.plan.json"
                cleanup_path = Path(self.temp.name) / f"{profile}.cleanup.json"
                env = {
                    "GITHUB_REPOSITORY": SELF_REPO,
                    "GITHUB_RUN_ID": "900",
                    "GITHUB_RUN_ATTEMPT": "2",
                    "GITHUB_SHA": "d" * 40,
                    "SELF_SMOKE_SHA": run_sha,
                    "PR_NUMBER": "52",
                    "PR_HEAD_SHA": pr_head,
                    "PR_GATE_GENERATION": "4",
                    "SELF_SMOKE_PROFILE": profile,
                    "SELF_SMOKE_PLAN": str(plan_path),
                    "GITHUB_OUTPUT": str(out),
                }
                plan = prepare_pr(env)
                self.assertEqual(plan["workflow"], PR_SMOKE_WORKFLOW)
                self.assertEqual(plan["sha"], run_sha)
                self.assertEqual(plan["cases"][0]["worker"], "blue")
                self.assertEqual(plan["cases"][0]["kind"], "test_worker_deploy")
                self.assertEqual(plan["cases"][0]["lifecycle"]["generation"], 4)
                self.assertEqual(plan["cases"][1]["worker"], "yellow")
                self.assertEqual(plan["cases"][1]["kind"], "test_worker_deploy")
                smoke_cases = plan["cases"][2:]
                self.assertEqual(len(smoke_cases), expected_smoke)
                self.assertTrue(
                    all(c["worker"].endswith("-test-id900a2") for c in smoke_cases)
                )
                outputs = dict(
                    line.split("=", 1)
                    for line in out.read_text().splitlines()
                    if "=" in line
                )
                self.assertEqual(outputs["target_test_id"], "id900a2")
                self.assertEqual(
                    len(json.loads(outputs["deploy_matrix"])["include"]), 2
                )
                self.assertEqual(
                    len(json.loads(outputs["smoke_matrix"])["include"]), expected_smoke
                )

                cleanup_plan = prepare_pr_cleanup(
                    {**env, "SELF_SMOKE_PLAN": str(cleanup_path)}
                )
                self.assertEqual(
                    [c["kind"] for c in cleanup_plan["cases"]],
                    ["test_worker_cleanup", "test_worker_cleanup"],
                )
                self.assertEqual(
                    [c["lifecycle"]["target_worker"] for c in cleanup_plan["cases"]],
                    ["blue-test-id900a2", "yellow-test-id900a2"],
                )

                run_meta = {
                    "id": 900,
                    "run_attempt": 2,
                    "head_sha": run_sha,
                    "path": f".github/workflows/{PR_SMOKE_WORKFLOW}",
                    "_ci_repo": SELF_REPO,
                }
                comments = [
                    {
                        "user": {"login": "reporter"},
                        "body": body_for(
                            run_meta,
                            item,
                            report(
                                run_meta,
                                item,
                                cleanup_plan["plan_digest"],
                                "PASS",
                                0,
                                [f"Cleaned up {item['lifecycle']['target_worker']}"],
                                True,
                            ),
                        ),
                    }
                    for item in cleanup_plan["cases"]
                ]
                client = Mock()
                client.commit_comments.return_value = comments
                with patch("ci_core.self_smoke.GitHubClient", return_value=client):
                    wait(
                        {
                            **env,
                            "SELF_SMOKE_PLAN": str(cleanup_path),
                            "CI_RESULT_AUTHOR": "reporter",
                            "GH_TOKEN": "fixture",
                        },
                        once=True,
                    )

    def test_verify_definition_allows_same_zone_test_worker_and_blocks_cross_zone(self):
        archive = Path(self.temp.name) / "catalog.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(ROOT / ".ci/lite-actions.json", arcname=".ci/lite-actions.json")
        defaults = {"worker": "", "target_pool": "", "docker": ""}
        blue_item = normalize(
            SimpleNamespace(
                test_name="dispatch_smoke_8p",
                test_descr="Lite Actions dispatch smoke: 1 node(s), 8P HCCL/DP handshake",
                ci_kind="npu_smoke",
                ci_overrides={
                    "docker": "cann_master_torchtitan_master_xyj",
                    "target_pool": "a3_shared_blue",
                    "worker": "blue-test-id42a1",
                },
                nnodes=1,
                ngpu=8,
                timeout=75,
            ),
            defaults,
            0,
        )
        verified = verify_definition(archive, blue_item, defaults, repo=SELF_REPO)
        self.assertEqual(verified["test_id"], "dispatch_smoke_8p")

        cross_zone = {**blue_item, "worker": "yellow-test-id42a1"}
        with self.assertRaisesRegex(ValueError, "zone mismatch"):
            verify_definition(archive, cross_zone, defaults, repo=SELF_REPO)

    def test_pr_smoke_router_eligibility_cleanup_and_broker_isolation(self):
        root = Path(self.temp.name) / "worker"
        write_workspace(root)
        pools = json.loads((root / "config/pools.json").read_text())
        pool = pools.pop("pool_blue")
        for node in pool["nodes"]:
            node["environments"]["cann_master_torchtitan_master_xyj"] = node[
                "environments"
            ].pop("training")
        pools["a3_shared_blue"] = pool
        (root / "config/pools.json").write_text(json.dumps(pools))

        env = {
            "GITHUB_REPOSITORY": SELF_REPO,
            "GITHUB_RUN_ID": "77",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "e" * 40,
            "SELF_SMOKE_SHA": "a" * 40,
            "PR_NUMBER": "52",
            "PR_HEAD_SHA": "a" * 40,
            "SELF_SMOKE_PROFILE": "cpu-only",
            "SELF_SMOKE_PLAN": str(Path(self.temp.name) / "pr_plan.json"),
        }
        pr_plan = prepare_pr(env)
        cleanup_plan = prepare_pr_cleanup(
            {**env, "SELF_SMOKE_PLAN": str(Path(self.temp.name) / "pr_cleanup.json")}
        )
        run = {
            "_ci_provider": "github",
            "_ci_repo": SELF_REPO,
            "id": 77,
            "run_attempt": 1,
            "name": "lite-ci-pr-smoke",
            "path": f".github/workflows/{PR_SMOKE_WORKFLOW}",
            "status": "in_progress",
            "event": "pull_request",
            "head_branch": "feat/per-pr-hardware-test",
            "head_sha": "a" * 40,
            "head_repository": {"full_name": SELF_REPO},
            "actor": {"login": "depeng1994"},
            "created_at": "2026-01-01T00:00:00Z",
        }
        client = SimpleNamespace(
            repo=SELF_REPO,
            ci_request=lambda _run: pr_plan,
            ci_cleanup_request=lambda _run: cleanup_plan,
        )
        with patch.dict(
            os.environ,
            {
                "LITE_ACTIONS_MODEL_REPOS": SELF_REPO,
                "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                "LITE_ACTIONS_STATE_DIR": "",
                "LITE_ACTIONS_RELEASE_SHA": "f" * 40,
            },
        ):
            router = Router(root, "blue", client, "reporter")
            try:
                self.assertTrue(router.eligible(run))
                self.assertFalse(
                    router.eligible(
                        {
                            **run,
                            "head_repository": {"full_name": "external/lite-actions"},
                        }
                    )
                )
                self.assertTrue(
                    router.eligible(
                        {**run, "event": "workflow_dispatch", "head_branch": "main"}
                    )
                )
                self.assertFalse(
                    router.eligible(
                        {**run, "event": "workflow_dispatch", "head_branch": "other"}
                    )
                )
                self.assertTrue(
                    router.eligible(
                        {**run, "event": "pull_request_target", "head_branch": "main"}
                    )
                )
                self.assertFalse(
                    router.eligible(
                        {**run, "event": "pull_request_target", "head_branch": "other"}
                    )
                )
                with patch.object(router, "transition"):
                    router.receive([run])
                keys = sorted(router.state.read().keys())
                self.assertEqual(
                    keys,
                    [
                        f"{SELF_REPO}:77:1:case_000_cleanup_blue",
                        f"{SELF_REPO}:77:1:case_000_deploy_blue",
                    ],
                )
            finally:
                router.close()

        coord = PhysicalResourceCoordinator(Path(self.temp.name) / "broker-locks")
        broker = TestBrokerServer(
            socket_path=Path(self.temp.name) / "broker.sock",
            worker_id="blue-test-id77a1",
            main_root=root,
            state_dir=Path(self.temp.name) / "tw-state",
            pools_path=root / "config/pools.json",
            pools=pools,
            pools_revision=1,
            workers=json.loads((root / "config/workers.json").read_text()),
            author="reporter",
            address="192.0.2.10",
            release_sha="a" * 40,
            baseline_sha="f" * 40,
            client=client,
            coordinator=coord,
            scope={
                "repo": SELF_REPO,
                "source_run_id": 77,
                "pr_number": 52,
                "pr_head_sha": "a" * 40,
            },
            enforce_peercred=False,
        )
        fetched = broker._handle_fetch_plan({"run": run})
        self.assertTrue(fetched["found"])
        self.assertEqual(fetched["plan"]["plan_digest"], pr_plan["plan_digest"])
        self.assertEqual(fetched["run"]["head_repository"], {"full_name": SELF_REPO})

    def test_yellow_egress_allows_cleanup_artifact_and_proxied_test_worker(self):
        cfg = {
            "state_dir": self.temp.name,
            "daily_upload_bytes": 1_000_000,
            "artifact_hosts": ["productionresultssa16.blob.core.windows.net"],
        }
        policy = Egress("yellow", cfg, clock=lambda: 1_800_000_000.0)
        req_cleanup = urllib.request.Request(
            f"https://api.github.com/repos/{SELF_REPO}/actions/runs/77/artifacts?name=lite-ci-cleanup-1&per_page=10",
            method="GET",
        )
        policy.before(req_cleanup, repo=SELF_REPO, provider="github")

        run = {
            "id": 77,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "path": PR_SMOKE_WORKFLOW,
            "_ci_repo": SELF_REPO,
        }
        item = {
            "case_id": "case_000_worker_os_yellow",
            "test_id": "worker_os_yellow",
            "worker": "yellow-test-id77a1",
            "target_pool": None,
            "docker": None,
            "resources": {"nodes": 0, "devices_per_node": 0},
        }
        res = report(run, item, "b" * 64, "PASS", 0, ["OK"], True)
        body = body_for(run, item, res, local_worker="yellow-test-id77a1")
        req_ok = urllib.request.Request(
            f"https://api.github.com/repos/{SELF_REPO}/commits/{'a' * 40}/comments",
            data=json.dumps({"body": body}, separators=(",", ":")).encode(),
            method="POST",
        )
        policy.before(req_ok, repo=SELF_REPO, provider="github")

        req_blue = urllib.request.Request(
            f"https://api.github.com/repos/{SELF_REPO}/commits/{'a' * 40}/comments",
            data=json.dumps(
                {
                    "body": body.replace(
                        '"worker":"yellow-test-id77a1"', '"worker":"blue-test-id77a1"'
                    )
                },
                separators=(",", ":"),
            ).encode(),
            method="POST",
        )
        with self.assertRaisesRegex(Rejected, "YELLOW_IDENTITY_MISMATCH"):
            policy.before(req_blue, repo=SELF_REPO, provider="github")

    def test_build_systemd_command_short_user_and_separate_binds(self):
        cmd = build_systemd_command(
            unit_name="lite-actions-test-yellow-test-id45448326169a1",
            zone="yellow",
            worker_id="yellow-test-id45448326169a1",
            user_name="latw-0123456789ab",
            state_dir=Path("/var/lib/lite-actions/yellow-test-workers/0123/state"),
            broker_sock=Path("/run/lite-actions-broker/b-0123/b.sock"),
            ready_file=Path(
                "/var/lib/lite-actions/yellow-test-workers/0123/state/worker-ready.json"
            ),
            pools_file=Path(
                "/var/lib/lite-actions/yellow-test-workers/0123/state/pools-active.json"
            ),
            code_dir=Path("/var/lib/lite-actions/yellow-test-workers/0123/code"),
            python_bin="/opt/python312/bin/python3",
        )
        self.assertIn("User=latw-0123456789ab", cmd)
        self.assertIn("Group=latw-0123456789ab", cmd)
        self.assertIn("ReadWritePaths=/run/lite-actions-broker/b-0123", cmd)
        ro_binds = [arg for arg in cmd if arg.startswith("BindReadOnlyPaths=")]
        self.assertEqual(len(ro_binds), 2)
        self.assertFalse(any(":" in b.split("=", 1)[1] for b in ro_binds))


if __name__ == "__main__":
    unittest.main()
