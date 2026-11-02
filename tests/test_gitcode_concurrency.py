"""CPU dependency-DAG tests; no performance or platform success claim."""

import threading
import time
import unittest

from ci_core.gitcode_api import GitCodeAPIError
from ci_core.gitcode_concurrency import read_pair
from tests import test_gitcode_manual_api as manual_fixtures
from tests.test_gitcode_api import FixtureTransport, GitCodeClient


class ReadPairTests(unittest.TestCase):
    def test_two_reads_overlap_and_return_in_bound_order(self):
        gate = threading.Barrier(2)

        def read(value):
            gate.wait(timeout=2)
            return value

        self.assertEqual(
            read_pair(lambda: read("left"), lambda: read("right"), enabled=True),
            ("left", "right"),
        )

    def test_failure_joins_slow_sibling_before_returning(self):
        finished = threading.Event()

        def fail():
            raise ValueError("fixed failure")

        def slow():
            time.sleep(0.02)
            finished.set()

        with self.assertRaises(ValueError):
            read_pair(fail, slow, enabled=True)
        self.assertTrue(finished.is_set())

    def test_unapproved_transport_is_serial(self):
        calls = []
        self.assertEqual(
            read_pair(lambda: calls.append(1), lambda: calls.append(2), enabled=False),
            (None, None),
        )
        self.assertEqual(calls, [1, 2])

    def test_authority_parallel_branches_preserve_both_brackets(self):
        fixture = FixtureTransport()
        gate = threading.Barrier(2)

        def transport(path):
            if "/branches/" in path:
                self.assertEqual(fixture.pr_reads, 1)
                gate.wait(timeout=2)
            return fixture(path)

        transport.parallel_reads_safe = True
        client = GitCodeClient(fixture.repo, "9075802", transport=transport)
        result = client.authority_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
        )
        self.assertEqual(result["identity"]["base_sha"], "b" * 40)
        self.assertEqual(fixture.pr_reads, 2)
        self.assertEqual(fixture.calls[0], "/repos/" + fixture.repo)
        self.assertEqual(fixture.calls[-1], "/repos/" + fixture.repo)
        fixture.change_second_read = True
        fixture.pr_reads = 0
        with self.assertRaisesRegex(GitCodeAPIError, "CHANGED_DURING_READ"):
            client.authority_snapshot(
                7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
            )

    def test_same_branch_is_still_bound_to_two_roles(self):
        fixture = FixtureTransport()
        fixture.pr["head"]["ref"] = "master"
        fixture.pr["head"]["sha"] = "b" * 40
        fixture.parallel_reads_safe = True
        client = GitCodeClient(fixture.repo, "9075802", transport=fixture)
        result = client.authority_snapshot(
            7, allowed_base_refs=("master",), allowed_actors=("depeng1994",)
        )
        self.assertEqual(result["identity"]["head_sha"], result["identity"]["base_sha"])
        self.assertEqual(
            fixture.calls.count("/repos/" + fixture.repo + "/branches/master"), 2
        )

    def test_retained_capture_repo_precedes_parallel_run_and_job(self):
        fixture = manual_fixtures.ManualAPITests()
        fixture.setUp()
        client, transport = fixture.capture_client()
        gate = threading.Barrier(2)
        repo_seen = threading.Event()

        def concurrent(path):
            if "/api/v8/" in path:
                self.assertTrue(repo_seen.is_set())
                gate.wait(timeout=2)
            result = transport(path)
            if path == "/repos/owner/repo":
                repo_seen.set()
            return result

        concurrent.parallel_reads_safe = True
        client._transport = concurrent
        value = client.action_capture_snapshot("run-1", "job-1")
        self.assertEqual(value["run"]["workflow_run_id"], "run-1")
        self.assertEqual(value["job"]["id"], "job-1")
        original = concurrent

        def changed_job(path):
            value = original(path)
            if path.endswith("/jobs/job-1"):
                value["id"] = "wrong-job"
            return value

        changed_job.parallel_reads_safe = True
        client._transport = changed_job
        with self.assertRaises(GitCodeAPIError):
            client.action_capture_snapshot("run-1", "job-1")
