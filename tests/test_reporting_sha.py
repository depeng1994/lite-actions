import unittest

from ci_core.reporting import post, progress, report


class FakeClient:
    def __init__(self):
        self.read_shas = []
        self.sent = []

    def commit_comments(self, sha):
        self.read_shas.append(sha)
        return []

    def send_result(self, sha, body):
        self.sent.append((sha, body))
        return {"id": 123}


def sample_run(provider="gitcode", execution_sha="b" * 40):
    return {
        "_ci_provider": provider,
        "_ci_execution_sha": execution_sha,
        "head_sha": "a" * 40,
        "_ci_repo": "cann/torchtitan-npu",
        "path": ".gitcode/workflows/per_pr_alltest.yml",
        "id": "run-1",
        "run_attempt": 1,
    }


def sample_item():
    return {
        "case_id": "case_001_worker_os_yellow",
        "test_id": "worker_os_yellow",
        "worker": "yellow",
        "target_pool": None,
        "docker": None,
        "resources": {"nodes": 0, "devices_per_node": 0},
    }


class ReportingShaTests(unittest.TestCase):
    def test_gitcode_mr_report_post_and_progress_use_pr_head(self):
        run, item = sample_run(), sample_item()
        result = report(run, item, "digest", "PASS", 0, [], True)
        self.assertEqual(result["sha"], "b" * 40)
        client = FakeClient()
        post(client, run, item, result, "reporter")
        self.assertEqual(client.read_shas, ["b" * 40])
        self.assertEqual(client.sent[0][0], "b" * 40)
        progress(client, run, item, "digest", "RUNNING")
        self.assertEqual(client.sent[1][0], "b" * 40)

    def test_github_and_gitcode_manual_keep_run_head_sha(self):
        for provider, run in (
            ("github", sample_run("github", "b" * 40)),
            ("gitcode", {**sample_run(), "_ci_execution_sha": None}),
            (
                "gitcode",
                {k: v for k, v in sample_run().items() if k != "_ci_execution_sha"},
            ),
        ):
            with self.subTest(provider=provider, run=run):
                item = sample_item()
                result = report(run, item, "digest", "PASS", 0, [], True)
                self.assertEqual(result["sha"], "a" * 40)
                client = FakeClient()
                post(client, run, item, result, "reporter")
                self.assertEqual(client.read_shas, ["a" * 40])
                self.assertEqual(client.sent[0][0], "a" * 40)
                progress(client, run, item, "digest", "RUNNING")
                self.assertEqual(client.sent[1][0], "a" * 40)
