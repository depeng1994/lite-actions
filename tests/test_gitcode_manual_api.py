"""Documented v8 shapes only: synthetic fixtures, not live verification."""

import copy
import hashlib
import io
import json
import unittest
from unittest.mock import Mock

from ci_core.gitcode_api import GitCodeAPIError, GitCodeClient


class ManualAPITests(unittest.TestCase):
    def setUp(self):
        self.step = dict(
            id="step-1",
            job_run_id="job-1",
            name="capture",
            identifier="capture",
            sequence=1,
            status="COMPLETED",
            start_time=1000,
            end_time=2000,
            inputs=[{"key": "unused", "value": "SECRET"}],
            env=["SECRET"],
        )
        self.job = dict(
            id="job-1",
            identifier="capture_job",
            name="capture",
            sequence=1,
            status="COMPLETED",
            start_time=1000,
            end_time=2000,
            steps=[self.step],
            env=["SECRET"],
            secrets="SECRET",
        )
        self.pages = [
            dict(start_offset=0, end_offset=17, has_more=True, log="alpha\n"),
            dict(start_offset=17, end_offset=30, has_more=False, log="beta\n"),
        ]
        self.calls = []
        self.client = GitCodeClient("owner/repo", "1", transport=self.transport)

    def transport(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path == "/repos/owner/repo":
            return dict(id=1, full_name="owner/repo", default_branch="master")
        if path.endswith("/logs"):
            return copy.deepcopy(self.pages.pop(0))
        if path.endswith("/jobs"):
            return dict(total_count=1, jobs=[copy.deepcopy(self.job)])
        if path.endswith("/jobs/job-1"):
            return copy.deepcopy(self.job)
        raise AssertionError("unexpected path")

    def capture_client(self, *, run_changes=None, job_changes=None):
        run = dict(
            workflow_run_id="run-1",
            attempt=1,
            start_time=1000,
            head_sha="a" * 40,
            head_branch="master",
            workflow_id="workflow-1",
            file_path=".gitcode/capture.yml",
            event="MANUAL",
            status="COMPLETED",
            exist_in_default_branch=True,
        )
        run.update(run_changes or {})
        job = copy.deepcopy(self.job)
        job.update(job_changes or {})
        values = {
            "/repos/owner/repo": dict(
                id=1, full_name="owner/repo", default_branch="master"
            ),
            "/api/v8/repos/owner/repo/actions/runs/run-1": run,
            "/api/v8/repos/owner/repo/actions/runs/run-1/jobs/job-1": job,
        }
        transport = Mock(side_effect=lambda path: copy.deepcopy(values[path]))
        return GitCodeClient("owner/repo", "1", transport=transport), transport

    def test_platform_metadata_distinguishes_missing_null_and_empty_without_values(
        self,
    ):
        self.step.update(
            task="official_pre_step", inputs=None, env=None, condition=None
        )
        value = self.client._job_snapshot(self.job)["steps"][0]
        self.assertEqual(value["inputs_state"], "null")
        self.assertEqual(value["env_state"], "null")
        self.assertEqual(value["condition_state"], "null")
        del self.step["condition"]
        self.step["inputs"] = []
        self.step["env"] = {"secret": "DO_NOT_EXPOSE"}
        value = self.client._job_snapshot(self.job)["steps"][0]
        self.assertEqual(value["inputs_state"], "present")
        self.assertEqual(value["env_state"], "present")
        self.assertEqual(value["condition_state"], "missing")
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(value))

    def test_retained_capture_exact_three_fresh_reads_and_full_safe_metadata(self):
        client, transport = self.capture_client()
        expected_job = client._job_snapshot(self.job)
        for _ in range(2):
            observed = client.action_capture_snapshot("run-1", "job-1")
            self.assertEqual(observed["job"], expected_job)
            self.assertEqual(observed["run"]["workflow_run_id"], "run-1")
            self.assertNotIn("SECRET", json.dumps(observed))
        self.assertEqual(transport.call_count, 6)
        self.assertEqual(
            [c.args[0] for c in transport.call_args_list[:3]],
            [
                "/repos/owner/repo",
                "/api/v8/repos/owner/repo/actions/runs/run-1",
                "/api/v8/repos/owner/repo/actions/runs/run-1/jobs/job-1",
            ],
        )

    def test_retained_capture_bad_path_ids_rejected_before_io(self):
        client, transport = self.capture_client()
        for run, job in (
            ("../run", "job-1"),
            ("run-1", "x/y"),
            (True, "job-1"),
            ("run-1", "x?token=value"),
            ("run-1", "x" * 101),
        ):
            with self.assertRaises(GitCodeAPIError):
                client.action_capture_snapshot(run, job)
        transport.assert_not_called()

    def test_retained_capture_wrong_run_or_job_and_malformed_metadata_refused(self):
        for run_changes, job_changes in (
            ({"workflow_run_id": "other"}, {}),
            ({"exist_in_default_branch": "true"}, {}),
            ({"status": []}, {}),
            ({}, {"id": "other", "steps": []}),
            ({}, {"steps": [{**self.step, "job_run_id": "other"}]}),
            ({}, {"sequence": True}),
        ):
            client, _ = self.capture_client(
                run_changes=run_changes, job_changes=job_changes
            )
            with self.assertRaises(GitCodeAPIError):
                client.action_capture_snapshot("run-1", "job-1")

    def test_scoped_jobs_and_detail_strip_sensitive_fields(self):
        job = self.client.action_job("run-1", "job-1")
        self.assertNotIn("SECRET", json.dumps(job))
        self.assertEqual(job["steps"][0]["job_run_id"], "job-1")
        self.assertIn(
            ("/api/v8/repos/owner/repo/actions/runs/run-1/jobs/job-1", {}), self.calls
        )

    def test_safe_command_summary_hashes_exact_utf8_and_never_returns_script(self):
        script = "printf '私密'\r\n"
        self.step.update(
            task="official_shell",
            inputs=[
                {"key": "script", "value": script},
                {"key": "shell", "value": "bash"},
                {"key": "working-directory", "value": "/workspace"},
            ],
        )
        step = self.client.action_job("run-1", "job-1")["steps"][0]
        self.assertEqual(step["task"], "official_shell")
        self.assertEqual(
            step["script_sha256"], hashlib.sha256(script.encode("utf-8")).hexdigest()
        )
        self.assertEqual(step["shell"], "bash")
        self.assertEqual(step["working_directory"], "/workspace")
        self.assertNotIn("script", step)
        self.assertNotIn("私密", json.dumps(step, ensure_ascii=False))

    def test_command_inputs_duplicates_invalid_types_and_oversize_fail_closed(self):
        for inputs in (
            [{"key": "script", "value": "x"}, {"key": "script", "value": "y"}],
            [{"key": "shell", "value": "bash"}, {"key": "shell", "value": "sh"}],
            [{"key": "script", "value": False}],
            [{"key": "script", "value": "x" * (128 * 1024 + 1)}],
            [{"key": "shell", "value": "bash\n"}],
            [{"key": "working-directory", "value": "x" * 1025}],
        ):
            self.step["inputs"] = inputs
            with self.assertRaises(GitCodeAPIError):
                self.client.action_jobs("run-1")

    def test_missing_script_never_invents_digest(self):
        self.step["inputs"] = [{"key": "shell", "value": "bash"}]
        self.assertNotIn(
            "script_sha256", self.client.action_jobs("run-1")[0]["steps"][0]
        )

    def test_complete_logs_follow_opaque_offsets_not_text_lengths(self):
        value = self.client.action_step_logs("run-1", "job-1", "step-1")
        self.assertEqual(value["log"], "alpha\nbeta\n")
        self.assertEqual(value["end_offset"], 30)
        posts = [
            kwargs["body"] for path, kwargs in self.calls if path.endswith("/logs")
        ]
        self.assertEqual([p["offset"] for p in posts], [0, 17])
        self.assertTrue(
            all(p["sort"] == "asc" and p["step_id"] == "step-1" for p in posts)
        )

    def test_invalid_ids_never_reach_transport(self):
        for value in ("../secret", "x/y", "x?access_token=secret", True, "x" * 101):
            with self.assertRaises(GitCodeAPIError):
                self.client.action_jobs(value)
        self.assertEqual(self.calls, [])

    def test_job_and_step_ownership_fail_closed(self):
        with self.assertRaisesRegex(GitCodeAPIError, "NOT_IN_RUN"):
            self.client.action_job("run-1", "other")
        with self.assertRaisesRegex(GitCodeAPIError, "NOT_IN_JOB"):
            self.client.action_step_logs("run-1", "job-1", "other")
        self.step["job_run_id"] = "different-job"
        with self.assertRaisesRegex(GitCodeAPIError, "OWNERSHIP"):
            self.client.action_jobs("run-1")

    def test_incomplete_and_duplicate_job_lists_refused(self):
        for data in (
            dict(total_count=2, jobs=[self.job]),
            dict(total_count=2, jobs=[self.job, self.job]),
        ):
            self.client._get = Mock(return_value=data)
            self.client.repository = Mock()
            with self.assertRaises(GitCodeAPIError):
                self.client.action_jobs("run-1")

    def test_log_gap_overlap_stall_or_bad_types_rejected(self):
        for change in (
            {"start_offset": 1},
            {"end_offset": 0},
            {"has_more": 1},
            {"end_offset": True},
            {"log": []},
            {"end_offset": -1},
        ):
            self.pages = [
                {
                    **dict(start_offset=0, end_offset=17, has_more=True, log="alpha"),
                    **change,
                }
            ]
            with self.assertRaises(GitCodeAPIError):
                self.client.action_step_logs("run-1", "job-1", "step-1")

    def test_log_size_and_page_budgets_do_not_return_partial_success(self):
        with self.assertRaisesRegex(GitCodeAPIError, "INCOMPLETE"):
            self.client.action_step_logs("run-1", "job-1", "step-1", max_pages=1)
        self.pages = [dict(start_offset=0, end_offset=17, has_more=False, log="abcd")]
        with self.assertRaisesRegex(GitCodeAPIError, "SIZE_LIMIT"):
            self.client.action_step_logs("run-1", "job-1", "step-1", max_bytes=3)

    def test_post_query_is_exact_allowlisted_path_and_uses_header_auth(self):
        client = GitCodeClient("owner/repo", "1", token="synthetic-test-token")
        response = io.BytesIO(
            b'{"has_more":false,"start_offset":0,"end_offset":0,"log":""}'
        )
        client._opener = Mock()
        client._opener.open.return_value = response
        body = dict(step_id="step-1", offset=0, limit=1000, sort="asc")
        client._get(
            "/repos/owner/repo/actions/runs/run-1/jobs/job-1/logs",
            api_version=8,
            _log_query=body,
        )
        req = client._opener.open.call_args.args[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-test-token")
        self.assertNotIn("synthetic-test-token", req.full_url)
        self.assertEqual(json.loads(req.data), body)
        for path in ("/user", "/repos/other/repo/actions/runs/run-1/jobs/job-1/logs"):
            with self.assertRaisesRegex(GitCodeAPIError, "NOT_ALLOWED"):
                client._get(path, api_version=8, _log_query=body)


if __name__ == "__main__":
    unittest.main()
