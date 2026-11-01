"""Native/schema-4 boundary isolation and credential-safe artifact transport."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ci_core.contract import TORCHTITAN_REPO, VLLM_REPO
from ci_core.github import GitHubClient, MultiRepoGitHubClient
from ci_core.router import Router
from tests.scenarios import Service, definition, write_workspace


class PullIsolationTests(unittest.TestCase):
    def test_vllm_identity_is_canonical_and_old_alias_rejected(self):
        self.assertEqual(VLLM_REPO, "cann-framework/vllm-ascend")
        self.assertEqual(GitHubClient("fixture", VLLM_REPO).repo_id, 1438625957)
        with self.assertRaises(ValueError):
            GitHubClient("fixture", "torchtitan-npu/vllm-ascend")

    def test_one_repository_403_does_not_block_other_repository(self):
        client = MultiRepoGitHubClient("fixture", (TORCHTITAN_REPO, VLLM_REPO))
        client.clients[TORCHTITAN_REPO].running_workflows = Mock(side_effect=HTTPError(
            "https://api.github.com/fixture", 403, "forbidden", {}, None))
        healthy = {"id": 100, "created_at": "2026-11-01T00:00:00Z", "_ci_repo": VLLM_REPO}
        client.clients[VLLM_REPO].running_workflows = Mock(return_value=[healthy])
        self.assertEqual(client.running_workflows(), [healthy])

    def test_bad_request_does_not_stop_next_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer", "LITE_ACTIONS_STATE_DIR": "",
        }):
            root = Path(tmp)
            write_workspace(root)
            service = Service([definition("os", kind="worker_probe")], root)
            service.add_run(21)
            original = service.ci_request

            def load(run):
                if run["id"] == 20:
                    raise ValueError("bad plan")
                return original(run)

            router = Router(root, "blue", service, "reporter")
            try:
                with service.connected(), patch.object(service, "ci_request", side_effect=load):
                    router.receive(service.running_workflows())
                    self.assertEqual([r["run"]["id"] for r in router.state.read().values()], [21])
            finally:
                router.close()

    def test_unavailable_run_cannot_launch_but_healthy_run_finishes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "LITE_ACTIONS_ALLOWED_ACTORS": "maintainer", "LITE_ACTIONS_STATE_DIR": "",
        }):
            root = Path(tmp)
            write_workspace(root)
            service = Service([definition("os", kind="worker_probe")], root)
            service.add_run(21)
            router = Router(root, "blue", service, "reporter")
            try:
                with service.connected():
                    router.receive(service.running_workflows())
                    original = service.workflow_status

                    def status(run_id):
                        if run_id == 20:
                            raise OSError("one run unavailable")
                        return original(run_id)

                    with patch.object(service, "workflow_status", side_effect=status):
                        router.tick()
                        for future, _, _ in list(router.active.values()):
                            future.result(10)
                        router.pump()
                        router.flush()
                    records = {r["run"]["id"]: r for r in router.state.read().values()}
                    self.assertEqual(records[20]["phase"], "QUEUED")
                    self.assertEqual(records[21]["phase"], "REPORTED")
                    self.assertEqual([r["run_id"] for r in service.results()], [21])
            finally:
                router.close()

    def test_target_pr_keeps_control_sha_separate_from_source_sha(self):
        client = GitHubClient("fixture", VLLM_REPO)
        run = {"id": 1, "run_attempt": 1, "status": "in_progress", "head_sha": "c" * 40,
               "head_branch": "main", "event": "pull_request_target",
               "path": ".github/workflows/lite_actions_dispatcher_smoke.yml",
               "display_title": "lite-a5-pr-7-" + "a" * 40,
               "actor": {"login": "depeng1994"}}
        pull = {"state": "open", "draft": False, "user": {"login": "depeng1994"},
                "base": {"ref": "main", "repo": {"full_name": VLLM_REPO}},
                "head": {"sha": "a" * 40, "repo": {"full_name": "depeng1994/vllm-ascend"}}}

        def request(method, path, data=None):
            if path == "/pulls/7":
                return pull
            return {"workflow_runs": [run] if "event=pull_request_target" in path and "status=in_progress" in path else []}

        with patch.object(client, "request", side_effect=request):
            result = client.running_workflows()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["head_sha"], "a" * 40)
        self.assertEqual(result[0]["_ci_control_sha"], "c" * 40)
        self.assertEqual(run["head_sha"], "c" * 40)


class ArtifactTransportTests(unittest.TestCase):
    def test_signed_download_never_receives_github_authorization(self):
        client = GitHubClient("test-secret-not-real", VLLM_REPO)
        requests = []
        response = io.BytesIO(b"data")
        response.headers = {"Content-Length": "4"}

        def open_request(request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 302, "redirect", {
                    "Location": "https://productionresults.blob.core.windows.net/x?sig=fixture",
                }, None)
            return response

        with patch("urllib.request.build_opener", return_value=Mock(open=open_request)):
            self.assertEqual(client.download_artifact(10, limit=4), b"data")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-secret-not-real")
        self.assertIsNone(requests[1].get_header("Authorization"))

    def test_private_http_userinfo_and_off_domain_redirects_are_rejected(self):
        client = GitHubClient("fixture", VLLM_REPO)
        for destination in (
            "http://productionresults.blob.core.windows.net/x", "https://127.0.0.1/x",
            "https://attacker.invalid/x", "https://user@productionresults.blob.core.windows.net/x",
            "https://productionresults.blob.core.windows.net:8443/x",
        ):
            error = HTTPError("https://api.github.com/fixture", 302, "redirect", {"Location": destination}, None)
            with patch("urllib.request.build_opener", return_value=Mock(open=Mock(side_effect=error))):
                with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "destination"):
                    client.download_artifact(10, limit=100)

    def test_second_redirect_is_not_followed_or_exposed_in_exception(self):
        client = GitHubClient("fixture", VLLM_REPO)
        first = HTTPError("https://api.github.com/fixture", 302, "redirect", {
            "Location": "https://productionresults.blob.core.windows.net/x?sig=DO_NOT_PRINT",
        }, None)
        second = HTTPError("https://productionresults.blob.core.windows.net/x?sig=DO_NOT_PRINT", 302,
                           "redirect", {"Location": "https://attacker.invalid"}, None)
        with patch("urllib.request.build_opener", return_value=Mock(open=Mock(side_effect=[first, second]))):
            with self.assertRaisesRegex(RuntimeError, "artifact storage unavailable") as caught:
                client.download_artifact(10, limit=100)
        self.assertNotIn("DO_NOT_PRINT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
