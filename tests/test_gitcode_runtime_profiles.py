"""Repository-scoped runtime routing; synthetic clients, no live operations."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ci_core.gitcode_contract import MODEL_REPOSITORIES
from ci_core.gitcode_runtime import GitCodeRouterAdapter, RuntimeError4
from ci_core.gitcode_state import StateError


class RuntimeProfilesTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = Mock()
        self.store = Mock(root="shared-store")
        self.brokers = {}
        for repo_id in (1210038933, 1245430804):
            policy = SimpleNamespace(
                github_repo_id=repo_id,
                control_sha="a" * 40,
                workflow_id=777,
                enabled=True,
            )
            self.brokers[repo_id] = Mock(
                reader=SimpleNamespace(policy=policy, github=Mock()),
                coordinator=self.coordinator,
                store=self.store,
            )
        self.runtime = GitCodeRouterAdapter(
            list(self.brokers.values()), executor=Mock()
        )
        self.runtime.delivery = Mock()

    def run_record(self, repo_id):
        repo = MODEL_REPOSITORIES[repo_id][0]
        return {
            "id": 100,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "workflow_id": 777,
            "repository": {"id": repo_id, "full_name": repo},
            "_ci_repo": repo,
            "event": "workflow_dispatch",
            "display_title": "lite-gitcode:nonce",
            "status": "in_progress",
        }

    def plan(self, repo_id):
        return {
            "github": {
                "repo_id": repo_id,
                "run_id": 100,
                "run_attempt": 1,
                "workflow_id": 777,
            },
            "control": {"github_repo_id": repo_id, "control_sha": "a" * 40},
            "plan_digest": "b" * 64,
            "cases": [],
        }

    def test_same_release_and_run_ids_route_to_exact_repository(self):
        for repo_id, broker in self.brokers.items():
            run = self.run_record(repo_id)
            self.assertTrue(self.runtime.handles(run))
            self.assertIs(self.runtime.broker(self.plan(repo_id)), broker)
            self.coordinator.request_reference.side_effect = StateError(
                "UNKNOWN_RETAINED_REQUEST"
            )
            broker.admit.return_value = None
            self.runtime.receive(run)
            self.coordinator.request_reference.assert_called_with(f"{repo_id}/100/1")
            broker.admit.assert_called_once_with(100)

    def test_mismatched_path_id_or_unconfigured_release_never_handled(self):
        for field in ("_ci_repo", "repository", "head_sha", "workflow_id"):
            run = self.run_record(1245430804)
            run[field] = (
                {"id": 1210038933, "full_name": run["_ci_repo"]}
                if field == "repository"
                else "wrong"
            )
            self.assertFalse(self.runtime.handles(run))
            with self.assertRaises(RuntimeError4):
                self.runtime.receive(run)
        self.brokers[1245430804].admit.assert_not_called()

    def test_cancel_uses_exact_repository_client_and_workflow(self):
        payload = {
            "repo_id": 1245430804,
            "run_id": 100,
            "run_attempt": 1,
            "control_sha": "a" * 40,
            "workflow_id": 777,
        }
        stage = self.brokers[1245430804].reader.github
        stage.request.return_value = self.run_record(1245430804)
        self.runtime.cancel_run(payload)
        stage.request.assert_any_call("POST", "/actions/runs/100/cancel")
        self.brokers[1210038933].reader.github.request.assert_not_called()
        stage.reset_mock()
        stage.request.return_value = self.run_record(1210038933)
        with self.assertRaisesRegex(RuntimeError4, "IDENTITY_CHANGED"):
            self.runtime.cancel_run(payload)
        self.assertEqual(stage.request.call_count, 1)

    def test_unapproved_cancel_workflow_never_reads_or_writes(self):
        with self.assertRaises(RuntimeError4):
            self.runtime.cancel_run(
                {
                    "repo_id": 1245430804,
                    "run_id": 100,
                    "control_sha": "a" * 40,
                    "workflow_id": 999,
                }
            )
        for broker in self.brokers.values():
            broker.reader.github.request.assert_not_called()

    def test_cross_repository_plan_projection_and_retained_alias_rejected(self):
        with self.assertRaisesRegex(RuntimeError4, "RUN_PLAN_IDENTITY"):
            self.runtime.enqueue(self.plan(1245430804), {}, self.run_record(1210038933))
        self.coordinator.request_reference.side_effect = None
        self.coordinator.request_reference.return_value = {"plan_digest": "b" * 64}
        self.store.load_plan.return_value = SimpleNamespace(plan=self.plan(1245430804))
        with self.assertRaisesRegex(RuntimeError4, "RETAINED_REQUEST_ID"):
            self.runtime.resolve_request("1210038933/100/1")
        bad = copy.deepcopy(self.plan(1245430804))
        bad["control"]["github_repo_id"] = 1210038933
        with self.assertRaisesRegex(RuntimeError4, "PLAN_REPOSITORY"):
            self.runtime.broker(bad)

    def test_multiple_coordinators_rejected(self):
        self.brokers[1245430804].coordinator = Mock()
        with self.assertRaisesRegex(RuntimeError4, "SINGLE_COORDINATOR"):
            GitCodeRouterAdapter(list(self.brokers.values()))


if __name__ == "__main__":
    unittest.main()
