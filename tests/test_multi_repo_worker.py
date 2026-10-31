"""Multi-repository worker and A5 multinode rootinfo fail-fast regressions."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.contract import (
    TORCHTITAN_REPO,
    VLLM_REPO,
    configured_repos,
    create_plan,
    normalize,
)
from ci_core.executor import check_a5_multinode_rootinfo, task_dir
from ci_core.github import GitHubClient, MultiRepoGitHubClient
from ci_core.router import Router


def make_case(test_id, worker="yellow"):
    nodes = 4 if test_id.endswith("_32p") else 1
    defaults = dict(
        worker=worker, target_pool="a5_shared_yellow", docker="vllm-kimi-wangjinyin"
    )
    test = SimpleNamespace(
        test_name=test_id,
        test_descr="A5 smoke",
        nnodes=nodes,
        ngpu=8,
        ci_kind="npu_smoke",
        ci_overrides={},
        timeout=120,
    )
    return normalize(test, defaults, 0), defaults


class MultiRepoWorkerTests(unittest.TestCase):
    def test_env_modes_reject_duplicate_and_unsupported(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_repos(), (TORCHTITAN_REPO,))
        with patch.dict(os.environ, {"LITE_ACTIONS_MODEL_REPO": VLLM_REPO}, clear=True):
            self.assertEqual(configured_repos(), (VLLM_REPO,))
        with patch.dict(
            os.environ,
            {"LITE_ACTIONS_MODEL_REPOS": TORCHTITAN_REPO + "," + VLLM_REPO},
            clear=True,
        ):
            self.assertEqual(configured_repos(), (TORCHTITAN_REPO, VLLM_REPO))
        for invalid in (TORCHTITAN_REPO + "," + TORCHTITAN_REPO, "unknown/x", ""):
            with patch.dict(
                os.environ, {"LITE_ACTIONS_MODEL_REPOS": invalid}, clear=True
            ):
                with self.assertRaises(ValueError):
                    configured_repos()

    def test_clients_use_repo_specific_api_and_queue_identity(self):
        first = GitHubClient("fake", TORCHTITAN_REPO)
        second = GitHubClient("fake", VLLM_REPO)
        self.assertNotEqual(first.api, second.api)
        clients = MultiRepoGitHubClient("fake", (TORCHTITAN_REPO, VLLM_REPO))
        self.assertEqual(set(clients.clients), {TORCHTITAN_REPO, VLLM_REPO})
        self.assertIs(clients.for_repo(VLLM_REPO), clients.clients[VLLM_REPO])

    def test_router_receives_two_repos_into_one_state(self):
        class FakeClient:
            def __init__(self, repo, run, plan):
                self.repo, self.run, self.plan = repo, run, plan
                self.posted = []

            def ci_request(self, run):
                return self.plan

            def send_result(self, sha, body):
                self.posted.append((sha, body))
                return {"id": len(self.posted)}

        class Combined:
            def __init__(self, clients):
                self.clients = clients

            def for_repo(self, repo):
                return self.clients[repo]

        sha = "a" * 40
        clients = {}
        runs = []
        for repo, event, test_id in (
            (TORCHTITAN_REPO, "pull_request", "dispatch_smoke_a5_8p"),
            (VLLM_REPO, "pull_request_target", "dispatch_smoke_a5_32p"),
        ):
            case, defaults = make_case(test_id)
            run = {
                "id": 33,
                "run_attempt": 1,
                "head_sha": sha,
                "_ci_repo": repo,
                "_ci_pr_number": 4,
                "path": ".github/workflows/lite_actions_dispatcher_smoke.yml",
                "actor": {"login": "depeng1994"},
                "event": event,
                "status": "in_progress",
                "created_at": "2026-10-31T00:00:00Z",
            }
            plan = create_plan(
                [case],
                run_id=33,
                attempt=1,
                sha=sha,
                workflow="lite_actions_dispatcher_smoke.yml",
                defaults=defaults,
                repo=repo,
            )
            clients[repo] = FakeClient(repo, run, plan)
            runs.append(run)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "ci_core.router.load",
                    return_value=(
                        {"yellow": {"enabled": True}},
                        {"a5_shared_yellow": {"nodes": []}},
                    ),
                ),
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_TORCHTITAN_ALLOWED_ACTORS": "depeng1994",
                        "LITE_ACTIONS_VLLM_ALLOWED_ACTORS": "depeng1994",
                    },
                ),
            ):
                router = Router(Path(tmp), "yellow", Combined(clients), "depeng1994")
                try:
                    router.receive(runs)
                    stored = router.state.read()
                    self.assertEqual(len(stored), 2)
                    self.assertEqual(
                        {x["repo"] for x in stored.values()},
                        {TORCHTITAN_REPO, VLLM_REPO},
                    )
                    self.assertEqual(len(set(stored)), 2)
                    self.assertEqual(
                        {x["item"]["target_pool"] for x in stored.values()},
                        {"a5_shared_yellow"},
                    )
                    for client in clients.values():
                        self.assertEqual(len(client.posted), 1)
                finally:
                    router.close()

    def test_repo_source_paths_isolated(self):
        run = {"created_at": "2026-10-31T00:00:00Z", "id": 33, "run_attempt": 1}
        tt = task_dir({**run, "_ci_repo": TORCHTITAN_REPO}, Path("/tmp/ci"))
        vllm = task_dir({**run, "_ci_repo": VLLM_REPO}, Path("/tmp/ci"))
        self.assertNotEqual(tt, vllm)
        self.assertIn("vllm-ascend", str(vllm))


class RootinfoTests(unittest.TestCase):
    @staticmethod
    def fixture(nodes=2):
        selected = [
            {"host_id": f"node{i}", "devices": list(range(8))} for i in range(nodes)
        ]
        pools = {
            "a5_shared_yellow": {
                "type": "ascend-a5",
                "nodes": [
                    {
                        "host_id": x["host_id"],
                        "environments": {
                            "vllm-kimi-wangjinyin": {"container_id": "a" * 64}
                        },
                    }
                    for x in selected
                ],
            }
        }
        return {
            "pool": "a5_shared_yellow",
            "docker": "vllm-kimi-wangjinyin",
            "allocation": selected,
        }, pools

    def test_single_node_does_not_require_rootinfo(self):
        case, pools = self.fixture(1)
        with patch(
            "ci_core.config.execution_node",
            side_effect=AssertionError("must not be called"),
        ):
            check_a5_multinode_rootinfo(case, pools)

    def test_all_nodes_checked_and_missing_rootinfo_fails_with_remediation(self):
        case, pools = self.fixture()
        calls = []

        def fake_run(host, command, **_):
            calls.append((host, command))
            if "node1" in host:
                return SimpleNamespace(
                    returncode=1, stderr="FileNotFoundError: /etc/hccl_rootinfo.json"
                )
            return SimpleNamespace(returncode=0, stderr="")

        with (
            patch(
                "ci_core.config.execution_node",
                side_effect=lambda node, environment: {
                    "ssh": node["host_id"] + "/pinned-container"
                },
            ),
            patch("ci_core.transport.run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(
                ValueError, "A5_MULTI_NODE_ROOTINFO_INVALID"
            ) as e:
                check_a5_multinode_rootinfo(case, pools)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all("/etc/hccl_rootinfo.json" in command for _, command in calls)
        )
        self.assertTrue(all("rank_count" in command for _, command in calls))
        self.assertIn("node1", str(e.exception))
        self.assertIn("container=", str(e.exception))
        self.assertIn("FIX:", str(e.exception))

    def test_valid_all_nodes_pass(self):
        case, pools = self.fixture(4)
        with (
            patch(
                "ci_core.config.execution_node",
                side_effect=lambda node, environment: {
                    "ssh": node["host_id"] + "/pinned-container"
                },
            ),
            patch(
                "ci_core.transport.run",
                return_value=SimpleNamespace(returncode=0, stderr=""),
            ) as run,
        ):
            check_a5_multinode_rootinfo(case, pools)
        self.assertEqual(run.call_count, 4)


if __name__ == "__main__":
    unittest.main()
