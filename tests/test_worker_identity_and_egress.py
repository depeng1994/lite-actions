"""Unit tests for zone vs test-instance identity, lifecycle schemas, and Yellow egress."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.config import identify_worker, runtime_case
from ci_core.contract import (
    PR_SMOKE_WORKFLOW,
    SELF_REPO,
    create_plan,
    is_valid_worker_id,
    normalize,
    parse_plan,
    parse_worker_identity,
)
from ci_core.egress_policy import Deferred, Egress, Rejected
from ci_core.reporting import body_for, report
from ci_core.router import Router
from tests.scenarios import completed_execution, write_workspace
from unified_router import _worker_id_arg


class WorkerIdentityAndEgressTests(unittest.TestCase):
    def test_valid_and_rejected_worker_identities_across_cli_contract_and_egress(self):
        for valid in (
            "blue",
            "yellow",
            "blue-test-id231241244a1",
            "yellow-test-id9999999999a12",
        ):
            with self.subTest(valid=valid):
                self.assertTrue(is_valid_worker_id(valid))
                parsed = parse_worker_identity(valid)
                self.assertEqual(parsed["worker_id"], valid)
                self.assertEqual(_worker_id_arg(valid), valid)

        invalid_ids = (
            "blue-test-",
            "green-test-id1a1",
            "blue-test-id1",
            "blue-test-id1;sh",
            "blue-test-id0a1",
            "blue-test-id1a0",
            "yellow-test-id1a1\n",
            "",
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"state_dir": tmp, "daily_upload_bytes": 100000}
            for bad in invalid_ids:
                with self.subTest(bad=bad):
                    self.assertFalse(is_valid_worker_id(bad))
                    with self.assertRaises(ValueError):
                        parse_worker_identity(bad)
                    with self.assertRaises(argparse.ArgumentTypeError):
                        _worker_id_arg(bad)
                    with self.assertRaises(Rejected):
                        Egress(bad, cfg)
                    with self.assertRaises(ValueError):
                        normalize(
                            SimpleNamespace(
                                test_name="smoke_case",
                                test_descr="smoke",
                                nnodes=1,
                                ngpu=8,
                                ci_kind="npu_smoke",
                                ci_overrides={"worker": bad},
                                timeout=60,
                            ),
                            {
                                "worker": "blue",
                                "target_pool": "pool_blue",
                                "docker": None,
                            },
                            0,
                        )

    def test_lifecycle_schema_validation_and_workflow_restriction(self):
        deploy_meta = {
            "pr_number": 77,
            "pr_head_sha": "a" * 40,
            "target_test_id": "id555a2",
            "target_worker": "blue-test-id555a2",
            "github_run_id": 555,
            "github_attempt": 2,
            "generation": 3,
            "ttl_seconds": 14400,
        }
        cleanup_meta = {
            "pr_number": 77,
            "pr_head_sha": "a" * 40,
            "target_test_id": "id555a2",
            "target_worker": "yellow-test-id555a2",
            "github_run_id": 555,
            "github_attempt": 2,
        }
        defaults = {"worker": "blue", "target_pool": "a3_shared_blue", "docker": "img"}
        cases = [
            normalize(
                SimpleNamespace(
                    test_name="deploy_blue",
                    test_descr="Deploy Blue test worker",
                    nnodes=0,
                    ngpu=0,
                    ci_kind="test_worker_deploy",
                    ci_overrides={"worker": "blue"},
                    timeout=600,
                    lifecycle=deploy_meta,
                ),
                defaults,
                0,
            ),
            normalize(
                SimpleNamespace(
                    test_name="cleanup_yellow",
                    test_descr="Cleanup Yellow test worker",
                    nnodes=0,
                    ngpu=0,
                    ci_kind="test_worker_cleanup",
                    ci_overrides={"worker": "yellow"},
                    timeout=600,
                    lifecycle=cleanup_meta,
                ),
                defaults,
                1,
            ),
        ]
        plan = create_plan(
            cases,
            run_id=555,
            attempt=2,
            sha="b" * 40,
            workflow=PR_SMOKE_WORKFLOW,
            defaults=defaults,
            repo=SELF_REPO,
        )
        parsed = parse_plan(
            plan,
            {"id": 555, "run_attempt": 2, "head_sha": "b" * 40},
            PR_SMOKE_WORKFLOW,
            repo=SELF_REPO,
        )
        self.assertEqual(len(parsed), 2)

        with self.assertRaisesRegex(ValueError, "restricted to PR smoke"):
            create_plan(
                cases,
                run_id=555,
                attempt=2,
                sha="b" * 40,
                workflow="lite_actions_self_smoke.yml",
                defaults=defaults,
                repo=SELF_REPO,
            )

        with self.assertRaisesRegex(ValueError, "main zone worker"):
            normalize(
                SimpleNamespace(
                    test_name="deploy_bad",
                    test_descr="bad",
                    nnodes=0,
                    ngpu=0,
                    ci_kind="test_worker_deploy",
                    ci_overrides={"worker": "blue-test-id555a2"},
                    timeout=600,
                    lifecycle=deploy_meta,
                ),
                defaults,
                0,
            )

    def test_zone_ipv4_validation_rejects_mismatched_host(self):
        assigned = {"blue": "192.0.2.10", "yellow": "198.51.100.20"}
        self.assertEqual(
            identify_worker({"192.0.2.10"}, assigned, "blue-test-id42a1"),
            "blue-test-id42a1",
        )
        self.assertEqual(
            identify_worker({"198.51.100.20"}, assigned, "yellow-test-id42a1"),
            "yellow-test-id42a1",
        )
        with self.assertRaisesRegex(ValueError, "WORKER_IDENTITY_MISMATCH"):
            identify_worker({"198.51.100.20"}, assigned, "blue-test-id42a1")
        with self.assertRaisesRegex(ValueError, "WORKER_IDENTITY_MISMATCH"):
            identify_worker({"192.0.2.10"}, assigned, "yellow-test-id42a1")

    def test_yellow_shared_zone_egress_budget_and_identity_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zone_dir = root / "yellow-zone"
            test_dir = root / "yellow-test-state"
            zone_dir.mkdir()
            test_dir.mkdir()
            prod_cfg = {
                "state_dir": str(zone_dir),
                "zone_state_dir": str(zone_dir),
                "daily_upload_bytes": 1500,
                "control_center": {
                    "url": "https://control.example",
                    "ci_reserve_bytes": 0,
                },
            }
            test_cfg = {
                "state_dir": str(test_dir),
                "zone_state_dir": str(zone_dir),
                "daily_upload_bytes": 1500,
                "control_center": {
                    "url": "https://control.example",
                    "ci_reserve_bytes": 0,
                },
            }
            now = 1_800_000_000.0
            prod = Egress("yellow", prod_cfg, clock=lambda: now)
            test = Egress("yellow-test-id100a1", test_cfg, clock=lambda: now)

            sha = "a" * 40
            run = {
                "id": 100,
                "run_attempt": 1,
                "head_sha": sha,
                "path": ".github/workflows/smoke.yml",
                "_ci_repo": SELF_REPO,
            }
            item = {
                "case_id": "case_000_dispatch_smoke_yellow_a3_8p",
                "test_id": "dispatch_smoke_yellow_a3_8p",
                "worker": "yellow-test-id100a1",
                "target_pool": "a3_shared_yellow",
                "docker": None,
                "resources": {"nodes": 1, "devices_per_node": 8},
            }
            result = report(run, item, "b" * 64, "PASS", 0, ["line 1"], True)
            body = body_for(run, item, result, local_worker="yellow-test-id100a1")
            payload = json.dumps({"body": body}, separators=(",", ":")).encode()

            req_prod = urllib.request.Request(
                f"https://api.github.com/repos/{SELF_REPO}/actions/runs?status=in_progress&page=1",
                method="GET",
            )
            prod.before(req_prod, repo=SELF_REPO, provider="github")

            req_test = urllib.request.Request(
                f"https://api.github.com/repos/{SELF_REPO}/commits/{sha}/comments",
                data=payload,
                method="POST",
            )
            test.before(req_test, repo=SELF_REPO, provider="github")

            # Remaining combined daily budget is too low for another write.
            req_test_2 = urllib.request.Request(
                f"https://api.github.com/repos/{SELF_REPO}/commits/{sha}/comments",
                data=payload,
                method="POST",
            )
            with self.assertRaisesRegex(Deferred, "YELLOW_BUDGET_EXHAUSTED"):
                test.before(req_test_2, repo=SELF_REPO, provider="github")

            # Spoofing worker field inside body is rejected.
            forged_payload = json.dumps(
                {
                    "body": body.replace(
                        '"worker":"yellow-test-id100a1"', '"worker":"yellow"'
                    )
                },
                separators=(",", ":"),
            ).encode()
            req_forged = urllib.request.Request(
                f"https://api.github.com/repos/{SELF_REPO}/commits/{sha}/comments",
                data=forged_payload,
                method="POST",
            )
            with self.assertRaisesRegex(Rejected, "YELLOW_IDENTITY_MISMATCH"):
                test.before(req_forged, repo=SELF_REPO, provider="github")

            # Test worker cannot access main-worker control endpoints directly.
            req_hb = urllib.request.Request(
                "https://control.example/api/v1/worker/heartbeat",
                data=b'{"w":"yellow-test-id100a1"}',
                method="POST",
            )
            with self.assertRaisesRegex(Rejected, "UNTRUSTED_CONTROL_DESTINATION"):
                test.before(req_hb, repo="control-center", provider="control")

    def test_test_worker_resolves_default_docker_and_zone_case_environments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worker"
            write_workspace(root)
            pools = json.loads((root / "config/pools.json").read_text())
            pool = pools["pool_blue"]
            for node in pool["nodes"]:
                node["environments"] = {
                    "torchtitan_ci_a3_r2": node["environments"]["training"]
                }
            pool["owner"] = "yellow"
            pools = {"a3_shared_yellow": pool}
            (root / "config/pools.json").write_text(json.dumps(pools))

            workers = json.loads((root / "config/workers.json").read_text())
            workers["yellow"]["case_environments"] = {}
            (root / "config/workers.json").write_text(json.dumps(workers))

            worker_id = "yellow-test-id42a1"
            item = {
                "case_id": "case_000_dispatch_smoke_yellow_a3_8p",
                "order": 0,
                "test_id": "dispatch_smoke_yellow_a3_8p",
                "label": "Yellow A3 smoke",
                "kind": "npu_smoke",
                "worker": worker_id,
                "target_pool": "a3_shared_yellow",
                "resources": {"nodes": 1, "devices_per_node": 8},
                "docker": None,
                "execution_timeout_seconds": 75,
                "env_vars": {},
            }
            case = runtime_case(item, pools, worker_id)
            self.assertEqual(case["docker"], "torchtitan_ci_a3_r2")

            deploy_cfg = {
                "state_dir": str(root / "test-state"),
                "zone_state_dir": str(root / "yellow-state"),
                "address": "198.51.100.10",
                "case_environments": {
                    "torchtitan_ci_a3_r2": {
                        "ASCEND_SET_ENV_PATH": [
                            "/usr/local/Ascend/ascend-toolkit/set_env.sh"
                        ],
                    }
                },
            }
            captured = {}

            def fake_execute(exec_case, subrun, **kwargs):
                captured["verified_env"] = exec_case["verified_env"]
                captured["zone"] = subrun.get("_ci_zone")
                return completed_execution(root, exec_case, subrun)

            run = {
                "id": 42,
                "run_attempt": 1,
                "head_sha": "a" * 40,
                "path": ".gitcode/workflows/dispatcher_smoke.yml",
                "_ci_provider": "gitcode",
                "_ci_repo": "cann/torchtitan-npu",
                "created_at": "2026-01-01T00:00:00Z",
            }
            with (
                patch.dict(
                    "os.environ",
                    {
                        "LITE_ACTIONS_STATE_DIR": str(root / "test-state"),
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                        "LITE_ACTIONS_MODEL_REPOS": "cann-framework/vllm-ascend",
                    },
                ),
                patch("ci_core.router.execute", side_effect=fake_execute),
                patch.object(Router, "transition"),
                patch.object(Router, "finish"),
            ):
                router = Router(
                    root,
                    worker_id,
                    Mock(validate_current=lambda _run: None),
                    "depeng1994",
                    deployment_config=deploy_cfg,
                )
                try:
                    router.state.admit(
                        [
                            (
                                "k1",
                                {
                                    "phase": "QUEUED",
                                    "repo": "cann/torchtitan-npu",
                                    "run": run,
                                    "item": item,
                                    "digest": "d" * 64,
                                    "defaults": {},
                                    "queued_at": 1.0,
                                },
                            )
                        ]
                    )
                    lease = Mock()
                    router.work(
                        "k1",
                        {
                            **case,
                            "allocation": [
                                {"host_id": "192.0.2.10:22", "devices": [0, 1]}
                            ],
                        },
                        lease,
                        Mock(is_set=lambda: False),
                        Mock(),
                    )
                    lease.close.assert_called_once()
                finally:
                    router.close()
            self.assertEqual(
                captured["verified_env"],
                {"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh"},
            )
            self.assertEqual(captured["zone"], "yellow")


if __name__ == "__main__":
    unittest.main()
