"""CPU contracts for the self-owned smoke workflow and candidate identity."""

import copy
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, TORCHTITAN_REPO, parse_plan
from ci_core.plan import verify_definition
from ci_core.reporting import body_for, report
from ci_core.router import Router
from ci_core.self_identity import checkout_sha
from ci_core.self_smoke import prepare, results
from tests.integration_tests.tools.lite_actions.entrypoint import definition, launch
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
        with (
            patch(
                "ci_core.self_smoke.CATALOG", ROOT / ".ci/lite_actions/smoke_cases.json"
            ),
            patch("ci_core.self_smoke.PLAN", Path(self.temp.name) / "plan.json"),
        ):
            self.plan = prepare(self.env)

    def test_model_copies_and_default_plan(self):
        files = {
            ".ci/lite_actions/smoke_cases.json": "a8e8c8df6028612c18f463f1ab8fffbfe7f39ea86c536f015823ff07fe2ffb55",
            "tests/integration_tests/tools/lite_actions/dispatch_smoke_worker.py": "543e0d345876dcba96e6fbab08e1d9c4a34b07efdfe54524faf30694e4b8fc69",
        }
        for name, expected in files.items():
            self.assertEqual(
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected
            )
        self.assertEqual(
            [item["test_id"] for item in self.plan["cases"]],
            ["dispatch_smoke_8p", "dispatch_smoke_16p", "dispatch_smoke_8p"],
        )
        self.assertEqual(
            [
                item["resources"]["nodes"] * item["resources"]["devices_per_node"]
                for item in self.plan["cases"]
            ],
            [8, 16, 8],
        )
        self.assertEqual(len({item["case_id"] for item in self.plan["cases"]}), 3)
        run = {"id": 42, "run_attempt": 1, "head_sha": "a" * 40}
        with self.assertRaises(ValueError):
            parse_plan(self.plan, run, SELF_WORKFLOW, repo=TORCHTITAN_REPO)

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
        self.assertEqual(len(results(comments, self.plan, "reporter")), 3)
        self.assertEqual(len(results(comments[:-1], self.plan, "reporter")), 2)
        for mutation in ("sha", "cleanup", "unknown"):
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
                result["sha" if mutation == "sha" else "cleanup_verified"] = (
                    "b" * 40 if mutation == "sha" else False
                )
                bad[-1]["body"] = marker + "\n" + json.dumps(result)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                results(bad, self.plan, "reporter")

    def test_launch_consumes_complete_two_node_lease(self):
        case = definition("dispatch_smoke_16p")
        env = {
            "NODE_IPS": "192.0.2.1,192.0.2.2",
            "NODE_RANK": "1",
            "NNODES": "2",
            "NGPU": "8",
            "LITE_CI_MASTER_PORT": "16001",
            "LITE_CI_HCCL_IF_BASE_PORT": "20128",
            "LITE_CI_HCCL_HOST_PORT_RANGE": "24128-24191",
            "LITE_CI_HCCL_NPU_PORT_RANGE": "28128-28191",
        }
        with patch.dict(os.environ, env, clear=True), patch("subprocess.run") as run:
            launch(case, Path(self.temp.name) / "output")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--node-rank") + 1], "1")
            self.assertEqual(command[command.index("--nproc-per-node") + 1], "8")
            self.assertEqual(
                run.call_args.kwargs["env"]["LITE_SMOKE_EXPECTED_WORLD"], "16"
            )
            del os.environ["LITE_CI_HCCL_NPU_PORT_RANGE"]
            with self.assertRaises(KeyError):
                launch(case, Path(self.temp.name) / "missing")

    def test_pinned_archive_discovers_own_smoke_definitions(self):
        archive = Path(self.temp.name) / "self.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in (
                ".ci/lite_actions/smoke_cases.json",
                "tests/__init__.py",
                "tests/integration_tests/__init__.py",
                "tests/integration_tests/tools/__init__.py",
                "tests/integration_tests/tools/lite_actions/__init__.py",
                "tests/integration_tests/tools/lite_actions/entrypoint.py",
                "tests/integration_tests/tools/lite_actions/dispatch_smoke_worker.py",
            ):
                tar.add(ROOT / name, arcname=name)
        for item in self.plan["cases"]:
            found = verify_definition(
                archive, item, self.plan["defaults"], repo=SELF_REPO
            )
            self.assertEqual(found["test_id"], item["test_id"])

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
        catalog = root / ".ci/lite_actions/smoke_cases.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes((ROOT / ".ci/lite_actions/smoke_cases.json").read_bytes())
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
                "name": "Self-Smoke",
                "run_number": 42,
            }
        }
        service.plans = {42: self.plan}
        with tarfile.open(service.archive, "w:gz") as tar:
            for name in (
                ".ci/lite_actions/smoke_cases.json",
                "tests/__init__.py",
                "tests/integration_tests/__init__.py",
                "tests/integration_tests/tools/__init__.py",
                "tests/integration_tests/tools/lite_actions/__init__.py",
                "tests/integration_tests/tools/lite_actions/entrypoint.py",
                "tests/integration_tests/tools/lite_actions/dispatch_smoke_worker.py",
            ):
                tar.add(ROOT / name, arcname=name)
        env = {
            "LITE_ACTIONS_MODEL_REPOS": SELF_REPO,
            "LITE_ACTIONS_SELF_SMOKE_ENABLED": "1",
            "LITE_ACTIONS_SELF_ACTORS": "maintainer",
            "LITE_ACTIONS_SELF_BRANCHES": "ci/test",
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
            try:
                for _ in range(20):
                    router.tick()
                    for future, _, _ in list(router.active.values()):
                        future.result(10)
                    if len(service.results()) == 3:
                        break
                self.assertEqual(
                    len(results(service.comments, self.plan, "reporter")), 3
                )
            finally:
                router.close()

    def test_worker_checkout_and_manual_admission_are_sha_bound(self):
        root = Path(self.temp.name) / "checkout"
        (root / "ci_core").mkdir(parents=True)
        (root / "ci_core/core.py").write_text("VALUE = 1\n")
        (root / "unified_router.py").write_text("pass\n")
        for name in (
            ".ci/lite_actions/smoke_cases.json",
            ".github/workflows/lite_actions_self_smoke.yml",
            "tests/__init__.py",
            "tests/integration_tests/__init__.py",
            "tests/integration_tests/tools/__init__.py",
            "tests/integration_tests/tools/lite_actions/entrypoint.py",
            "tests/integration_tests/tools/lite_actions/dispatch_smoke_worker.py",
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
            subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True
            )
        sha = checkout_sha(root)
        router = Router.__new__(Router)
        router.root, router.self_sha = root, sha
        run = {
            "_ci_repo": SELF_REPO,
            "event": "workflow_dispatch",
            "path": ".github/workflows/" + SELF_WORKFLOW,
            "status": "in_progress",
            "actor": {"login": "maintainer"},
            "head_branch": "ci/test",
            "head_sha": sha,
        }
        with patch.dict(
            os.environ,
            {
                "LITE_ACTIONS_SELF_ACTORS": "maintainer",
                "LITE_ACTIONS_SELF_BRANCHES": "ci/test",
            },
        ):
            self.assertTrue(router.eligible(run))
            for field, value in (
                ("event", "pull_request"),
                ("head_sha", "b" * 40),
                ("head_branch", "other"),
            ):
                self.assertFalse(router.eligible({**run, field: value}))
            (root / "ci_core/core.py").write_text("VALUE = 2\n")
            with self.assertRaises(ValueError):
                router.eligible(run)


if __name__ == "__main__":
    unittest.main()
