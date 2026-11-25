"""Unit tests for PR Commit Status reporting, comment reactions, and workflow contracts."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ci_core import pr_status


class _FakeResponse:
    def __init__(self, body: dict | list | str = "{}"):
        self._raw = (
            body.encode("utf-8")
            if isinstance(body, str)
            else json.dumps(body).encode("utf-8")
        )

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingOpener:
    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self._responses = list(responses) if responses is not None else []

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.calls.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": body,
                "timeout": timeout,
            }
        )
        if self._responses:
            nxt = self._responses.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return _FakeResponse(nxt)
        return _FakeResponse({"id": 1})


class PrStatusPayloadAndMappingTest(unittest.TestCase):
    def test_build_status_payload_valid_and_optional_fields(self):
        payload = pr_status.build_status_payload(
            state="PENDING",
            context="lite-ci-pr-smoke / deploy-blue",
            description="Queued in PR smoke run #99",
            target_url="https://github.com/depeng1994/lite-actions/actions/runs/99",
        )
        self.assertEqual(
            payload,
            {
                "state": "pending",
                "context": "lite-ci-pr-smoke / deploy-blue",
                "description": "Queued in PR smoke run #99",
                "target_url": "https://github.com/depeng1994/lite-actions/actions/runs/99",
            },
        )

        minimal = pr_status.build_status_payload(
            state="success",
            context="lite-ci-pr-smoke / deploy-blue",
        )
        self.assertEqual(
            minimal,
            {"state": "success", "context": "lite-ci-pr-smoke / deploy-blue"},
        )

    def test_build_status_payload_truncates_description_and_normalizes_newlines(self):
        raw_desc = "line one\n\tline two " + ("x" * 200)
        payload = pr_status.build_status_payload(
            state="failure",
            context="lite-ci-pr-smoke / deploy-blue",
            description=raw_desc,
        )
        desc = payload["description"]
        self.assertLessEqual(len(desc), pr_status.MAX_DESCRIPTION_LEN)
        self.assertTrue(desc.endswith("..."))
        self.assertNotIn("\n", desc)
        self.assertNotIn("\t", desc)

    def test_build_status_payload_rejects_invalid_state_or_empty_context(self):
        with self.assertRaises(ValueError):
            pr_status.build_status_payload(state="queued", context="ctx")
        with self.assertRaises(ValueError):
            pr_status.build_status_payload(state="success", context="   ")

    def test_map_job_status_all_outcomes(self):
        self.assertEqual(pr_status.map_job_status("success"), "success")
        self.assertEqual(pr_status.map_job_status("SUCCESS"), "success")
        self.assertEqual(pr_status.map_job_status("failure"), "failure")
        self.assertEqual(pr_status.map_job_status("cancelled"), "error")
        self.assertEqual(pr_status.map_job_status("canceled"), "error")
        self.assertEqual(pr_status.map_job_status("skipped"), "error")
        self.assertEqual(pr_status.map_job_status(""), "error")

    def test_planned_contexts_for_full_blue_only_and_cpu_only(self):
        deploy_full = json.dumps(
            {
                "include": [
                    {"case_id": "deploy_blue", "worker": "blue"},
                    {"case_id": "deploy_yellow", "worker": "yellow"},
                ]
            }
        )
        smoke_full = json.dumps(
            {
                "include": [
                    {"case_id": "blue_probe", "worker": "blue-test-id1a1"},
                    {"case_id": "yellow_probe", "worker": "yellow-test-id1a1"},
                ]
            }
        )
        self.assertEqual(
            pr_status.planned_contexts(deploy_full, smoke_full, "full"),
            [
                "lite-ci-pr-smoke / deploy-blue",
                "lite-ci-pr-smoke / deploy-yellow",
                "lite-ci-pr-smoke / self-blue_probe",
                "lite-ci-pr-smoke / self-yellow_probe",
                "lite-ci-pr-smoke / gitcode-torchtitan-smoke",
            ],
        )

        deploy_blue = {"include": [{"case_id": "deploy_blue", "worker": "blue"}]}
        smoke_blue = {"include": [{"case_id": "blue_probe", "worker": "blue-test-id1"}]}
        self.assertEqual(
            pr_status.planned_contexts(deploy_blue, smoke_blue, "blue-only"),
            [
                "lite-ci-pr-smoke / deploy-blue",
                "lite-ci-pr-smoke / self-blue_probe",
            ],
        )

        self.assertEqual(
            pr_status.planned_contexts(deploy_full, smoke_full, "cpu-only"),
            [
                "lite-ci-pr-smoke / deploy-blue",
                "lite-ci-pr-smoke / deploy-yellow",
                "lite-ci-pr-smoke / self-blue_probe",
                "lite-ci-pr-smoke / self-yellow_probe",
            ],
        )


class PrStatusApiAndReactionsTest(unittest.TestCase):
    REPO = "depeng1994/lite-actions"
    SHA = "a" * 40

    def test_post_commit_status_request_and_headers(self):
        opener = _RecordingOpener()
        ok = pr_status.post_commit_status(
            repo=self.REPO,
            sha=self.SHA,
            state="pending",
            context="lite-ci-pr-smoke / deploy-blue",
            description="Queued in PR smoke run #77",
            target_url="https://github.com/depeng1994/lite-actions/actions/runs/77",
            token="ghs_test_secret_token",
            opener=opener,
        )
        self.assertTrue(ok)
        self.assertEqual(len(opener.calls), 1)
        call = opener.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            f"https://api.github.com/repos/{self.REPO}/statuses/{self.SHA}",
        )
        self.assertEqual(
            call["headers"]["authorization"], "Bearer ghs_test_secret_token"
        )
        self.assertEqual(call["headers"]["accept"], "application/vnd.github+json")
        self.assertEqual(call["headers"]["x-github-api-version"], "2022-11-28")
        self.assertEqual(
            call["body"],
            {
                "state": "pending",
                "context": "lite-ci-pr-smoke / deploy-blue",
                "description": "Queued in PR smoke run #77",
                "target_url": "https://github.com/depeng1994/lite-actions/actions/runs/77",
            },
        )

    def test_post_comment_reaction_rocket_and_confused(self):
        opener = _RecordingOpener()
        ok_rocket = pr_status.post_comment_reaction(
            repo=self.REPO,
            comment_id=123456789,
            content="rocket",
            token="ghs_token",
            opener=opener,
        )
        ok_confused = pr_status.post_comment_reaction(
            repo=self.REPO,
            comment_id="123456789",
            content="confused",
            token="ghs_token",
            opener=opener,
        )
        self.assertTrue(ok_rocket)
        self.assertTrue(ok_confused)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(
            opener.calls[0]["url"],
            f"https://api.github.com/repos/{self.REPO}/issues/comments/123456789/reactions",
        )
        self.assertEqual(opener.calls[0]["body"], {"content": "rocket"})
        self.assertEqual(opener.calls[1]["body"], {"content": "confused"})

    def test_post_pr_comment_sends_expected_payload(self):
        opener = _RecordingOpener()
        ok = pr_status.post_pr_comment(
            repo=self.REPO,
            pr_number=63,
            body="Unable to start `/pr-test`: PR #63 is not open (closed)",
            token="ghs_token",
            opener=opener,
        )
        self.assertTrue(ok)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(
            opener.calls[0]["url"],
            f"https://api.github.com/repos/{self.REPO}/issues/63/comments",
        )
        self.assertEqual(
            opener.calls[0]["body"],
            {"body": "Unable to start `/pr-test`: PR #63 is not open (closed)"},
        )

    def test_resolve_token_fallback_order(self):
        self.assertEqual(
            pr_status.resolve_token({"GH_TOKEN": "tok1", "GITHUB_TOKEN": "tok2"}),
            "tok1",
        )
        self.assertEqual(pr_status.resolve_token({"GITHUB_TOKEN": "tok2"}), "tok2")
        self.assertEqual(pr_status.resolve_token({}), "")


class PrStatusBestEffortErrorHandlingTest(unittest.TestCase):
    REPO = "depeng1994/lite-actions"
    SHA = "b" * 40

    def test_http_and_network_errors_return_false_without_leaking_token(self):
        secret = "ghs_super_secret_do_not_log_12345"
        http_err = urllib.error.HTTPError(
            url=f"https://api.github.com/repos/{self.REPO}/statuses/{self.SHA}",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"server error"),
        )
        opener = _RecordingOpener(
            responses=[
                http_err,
                urllib.error.URLError("dns lookup failed"),
                TimeoutError("timed out"),
            ]
        )
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            ok1 = pr_status.post_commit_status(
                repo=self.REPO,
                sha=self.SHA,
                state="success",
                context="lite-ci-pr-smoke / deploy-blue",
                token=secret,
                opener=opener,
            )
            ok2 = pr_status.post_comment_reaction(
                repo=self.REPO,
                comment_id=9876,
                content="rocket",
                token=secret,
                opener=opener,
            )
            ok3 = pr_status.post_pr_comment(
                repo=self.REPO,
                pr_number=63,
                body="rejected",
                token=secret,
                opener=opener,
            )
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        self.assertFalse(ok3)
        stderr_text = err_buf.getvalue()
        self.assertIn("HTTP 500", stderr_text)
        self.assertIn("URLError", stderr_text)
        self.assertIn("TimeoutError", stderr_text)
        self.assertNotIn(secret, stderr_text)

    def test_missing_token_and_invalid_inputs_are_best_effort(self):
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            self.assertFalse(
                pr_status.post_commit_status(
                    repo=self.REPO,
                    sha=self.SHA,
                    state="pending",
                    context="lite-ci-pr-smoke / deploy-blue",
                    environ={},
                )
            )
            self.assertFalse(
                pr_status.post_commit_status(
                    repo="../escape",
                    sha=self.SHA,
                    state="pending",
                    context="lite-ci-pr-smoke / deploy-blue",
                    token="tok",
                )
            )
            self.assertFalse(
                pr_status.post_commit_status(
                    repo=self.REPO,
                    sha="not-a-sha!",
                    state="pending",
                    context="lite-ci-pr-smoke / deploy-blue",
                    token="tok",
                )
            )
            self.assertFalse(
                pr_status.post_comment_reaction(
                    repo=self.REPO,
                    comment_id="abc",
                    content="rocket",
                    token="tok",
                )
            )
            self.assertFalse(
                pr_status.post_comment_reaction(
                    repo=self.REPO,
                    comment_id=42,
                    content="unsupported_emoji",
                    token="tok",
                )
            )
        self.assertIn("::warning::pr_status:", err_buf.getvalue())

    def test_cli_always_returns_zero_even_when_api_fails(self):
        failing_opener = _RecordingOpener(
            responses=[urllib.error.URLError("offline")] * 10
        )
        env = {
            "GH_TOKEN": "ghs_cli_secret",
            "GITHUB_REPOSITORY": self.REPO,
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "404",
            "PR_HEAD_SHA": self.SHA,
            "DEPLOY_MATRIX": json.dumps({"include": [{"worker": "blue"}]}),
            "SMOKE_MATRIX": json.dumps({"include": [{"case_id": "probe_a"}]}),
            "PROFILE": "full",
            "STATUS_CONTEXT": "lite-ci-pr-smoke / deploy-blue",
            "JOB_STATUS": "failure",
            "DEPLOY_RESULT": "failure",
            "SMOKE_SELF_RESULT": "skipped",
            "SMOKE_GITCODE_RESULT": "skipped",
            "COMMENT_ID": "555",
            "PR_NUMBER": "63",
        }
        err_buf = io.StringIO()
        with (
            mock.patch.object(
                pr_status.urllib.request, "build_opener", return_value=failing_opener
            ),
            redirect_stderr(err_buf),
        ):
            self.assertEqual(pr_status.main(["pending"], environ=env), 0)
            self.assertEqual(pr_status.main(["job"], environ=env), 0)
            self.assertEqual(pr_status.main(["reconcile"], environ=env), 0)
            self.assertEqual(
                pr_status.main(["reaction", "--content", "rocket"], environ=env), 0
            )
            self.assertEqual(
                pr_status.main(["comment", "--body", "hi"], environ=env), 0
            )
        self.assertNotIn("ghs_cli_secret", err_buf.getvalue())


class PrStatusLifecycleTransitionsTest(unittest.TestCase):
    REPO = "depeng1994/lite-actions"
    SHA = "c" * 40
    RUN_URL = "https://github.com/depeng1994/lite-actions/actions/runs/5001"
    DEPLOY_MATRIX = json.dumps(
        {
            "include": [
                {"case_id": "deploy_blue", "worker": "blue"},
                {"case_id": "deploy_yellow", "worker": "yellow"},
            ]
        }
    )
    SMOKE_MATRIX = json.dumps(
        {
            "include": [
                {"case_id": "case_a", "worker": "blue-test-id5001a1"},
                {"case_id": "case_b", "worker": "yellow-test-id5001a1"},
            ]
        }
    )

    def test_pending_to_terminal_transitions_for_full_profile(self):
        opener = _RecordingOpener()
        pending = pr_status.publish_pending(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=self.DEPLOY_MATRIX,
            smoke_matrix=self.SMOKE_MATRIX,
            profile="full",
            target_url=self.RUN_URL,
            run_id="5001",
            token="tok",
            opener=opener,
        )
        self.assertEqual(
            list(pending.keys()),
            [
                "lite-ci-pr-smoke / deploy-blue",
                "lite-ci-pr-smoke / deploy-yellow",
                "lite-ci-pr-smoke / self-case_a",
                "lite-ci-pr-smoke / self-case_b",
                "lite-ci-pr-smoke / gitcode-torchtitan-smoke",
            ],
        )
        self.assertTrue(all(pending.values()))
        for call in opener.calls:
            self.assertEqual(call["body"]["state"], "pending")
            self.assertEqual(call["body"]["target_url"], self.RUN_URL)
            self.assertEqual(
                call["body"]["description"], "Queued in PR smoke run #5001"
            )

        opener.calls.clear()
        self.assertTrue(
            pr_status.publish_job_status(
                repo=self.REPO,
                sha=self.SHA,
                context="lite-ci-pr-smoke / deploy-blue",
                job_status="success",
                target_url=self.RUN_URL,
                run_id="5001",
                token="tok",
                opener=opener,
            )
        )
        self.assertTrue(
            pr_status.publish_job_status(
                repo=self.REPO,
                sha=self.SHA,
                context="lite-ci-pr-smoke / self-case_a",
                job_status="failure",
                target_url=self.RUN_URL,
                run_id="5001",
                token="tok",
                opener=opener,
            )
        )
        self.assertTrue(
            pr_status.publish_job_status(
                repo=self.REPO,
                sha=self.SHA,
                context="lite-ci-pr-smoke / gitcode-torchtitan-smoke",
                job_status="cancelled",
                target_url=self.RUN_URL,
                run_id="5001",
                token="tok",
                opener=opener,
            )
        )
        self.assertEqual(
            [(c["body"]["context"], c["body"]["state"]) for c in opener.calls],
            [
                ("lite-ci-pr-smoke / deploy-blue", "success"),
                ("lite-ci-pr-smoke / self-case_a", "failure"),
                ("lite-ci-pr-smoke / gitcode-torchtitan-smoke", "error"),
            ],
        )

    def test_reconcile_skipped_when_deploy_fails_full_profile(self):
        opener = _RecordingOpener()
        updates = pr_status.reconcile_skipped(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=self.DEPLOY_MATRIX,
            smoke_matrix=self.SMOKE_MATRIX,
            profile="full",
            deploy_result="failure",
            smoke_self_result="skipped",
            smoke_gitcode_result="skipped",
            target_url=self.RUN_URL,
            token="tok",
            opener=opener,
        )
        self.assertEqual(
            list(updates.keys()),
            [
                "lite-ci-pr-smoke / self-case_a",
                "lite-ci-pr-smoke / self-case_b",
                "lite-ci-pr-smoke / gitcode-torchtitan-smoke",
            ],
        )
        self.assertTrue(all(updates.values()))
        for call in opener.calls:
            self.assertEqual(call["body"]["state"], "error")
            self.assertEqual(
                call["body"]["description"],
                "Skipped because upstream job did not succeed",
            )
            self.assertEqual(call["body"]["target_url"], self.RUN_URL)

    def test_reconcile_skipped_when_smoke_self_fails_full_profile(self):
        opener = _RecordingOpener()
        updates = pr_status.reconcile_skipped(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=self.DEPLOY_MATRIX,
            smoke_matrix=self.SMOKE_MATRIX,
            profile="full",
            deploy_result="success",
            smoke_self_result="failure",
            smoke_gitcode_result="skipped",
            target_url=self.RUN_URL,
            token="tok",
            opener=opener,
        )
        self.assertEqual(updates, {"lite-ci-pr-smoke / gitcode-torchtitan-smoke": True})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0]["body"]["state"], "error")

    def test_reconcile_skipped_blue_only_never_posts_gitcode_status(self):
        opener = _RecordingOpener()
        updates = pr_status.reconcile_skipped(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=json.dumps({"include": [{"worker": "blue"}]}),
            smoke_matrix=json.dumps({"include": [{"case_id": "case_a"}]}),
            profile="blue-only",
            deploy_result="success",
            smoke_self_result="failure",
            smoke_gitcode_result="skipped",
            target_url=self.RUN_URL,
            token="tok",
            opener=opener,
        )
        self.assertEqual(updates, {})
        self.assertEqual(opener.calls, [])

    def test_reconcile_skipped_all_success_makes_no_api_calls(self):
        opener = _RecordingOpener()
        updates = pr_status.reconcile_skipped(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=self.DEPLOY_MATRIX,
            smoke_matrix=self.SMOKE_MATRIX,
            profile="full",
            deploy_result="success",
            smoke_self_result="success",
            smoke_gitcode_result="success",
            target_url=self.RUN_URL,
            token="tok",
            opener=opener,
        )
        self.assertEqual(updates, {})
        self.assertEqual(opener.calls, [])

    def test_reconcile_cancelled_stage_updates_only_still_pending_for_same_run(self):
        combined_resp = {
            "statuses": [
                {
                    "context": "lite-ci-pr-smoke / deploy-blue",
                    "state": "failure",
                    "target_url": self.RUN_URL,
                },
                {
                    "context": "lite-ci-pr-smoke / deploy-yellow",
                    "state": "pending",
                    "target_url": self.RUN_URL,
                },
            ]
        }
        opener = _RecordingOpener(
            responses=[
                {"id": 1},  # self-case_a skipped -> error
                {"id": 2},  # self-case_b skipped -> error
                {"id": 3},  # gitcode skipped -> error
                combined_resp,  # GET combined status for cancelled deploy
                {"id": 4},  # deploy-yellow pending -> error
            ]
        )
        updates = pr_status.reconcile_skipped(
            repo=self.REPO,
            sha=self.SHA,
            deploy_matrix=self.DEPLOY_MATRIX,
            smoke_matrix=self.SMOKE_MATRIX,
            profile="full",
            deploy_result="cancelled",
            smoke_self_result="skipped",
            smoke_gitcode_result="skipped",
            target_url=self.RUN_URL,
            token="tok",
            opener=opener,
        )
        self.assertEqual(
            list(updates.keys()),
            [
                "lite-ci-pr-smoke / self-case_a",
                "lite-ci-pr-smoke / self-case_b",
                "lite-ci-pr-smoke / gitcode-torchtitan-smoke",
                "lite-ci-pr-smoke / deploy-yellow",
            ],
        )
        self.assertNotIn("lite-ci-pr-smoke / deploy-blue", updates)
        post_calls = [c for c in opener.calls if c["method"] == "POST"]
        self.assertEqual(
            post_calls[-1]["body"]["description"],
            "Cancelled before job completed",
        )


class PrSmokeWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wf_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/lite_actions_pr_smoke.yml"
        )
        cls.workflow_text = wf_path.read_text(encoding="utf-8")

    def _job_section(self, job_name: str, next_job: str | None) -> str:
        start_marker = f"\n  {job_name}:\n"
        self.assertIn(start_marker, self.workflow_text)
        rest = self.workflow_text.split(start_marker, 1)[1]
        if next_job is None:
            return rest
        end_marker = f"\n  {next_job}:\n"
        self.assertIn(end_marker, rest)
        return rest.split(end_marker, 1)[0]

    def test_permissions_include_statuses_and_issues_write(self):
        perms_section = self.workflow_text.split("\npermissions:\n", 1)[1].split(
            "\njobs:\n", 1
        )[0]
        self.assertIn("  contents: read", perms_section)
        self.assertIn("  actions: read", perms_section)
        self.assertIn("  pull-requests: read", perms_section)
        self.assertIn("  statuses: write", perms_section)
        self.assertIn("  issues: write", perms_section)

    def test_prepare_resolve_has_issue_comment_reactions_and_rejection_feedback(self):
        prepare = self._job_section("prepare", "deploy")
        self.assertIn("startsWith(github.event.comment.body, '/pr-test')", prepare)
        self.assertIn("COMMENT_ID: ${{ github.event.comment.id }}", prepare)
        self.assertIn("issues/comments/$COMMENT_ID/reactions", prepare)
        self.assertIn('post_comment_reaction "rocket"', prepare)
        self.assertIn('post_comment_reaction "confused"', prepare)
        self.assertIn("issues/$pr_number/comments", prepare)
        self.assertIn("reject_request", prepare)

    def test_prepare_publishes_pending_statuses_after_plan(self):
        prepare = self._job_section("prepare", "deploy")
        plan_pos = prepare.index("python3 -m ci_core.self_smoke prepare-pr")
        upload_pos = prepare.index("uses: actions/upload-artifact@v4")
        pending_pos = prepare.index("python3 -m ci_core.pr_status pending")
        self.assertLess(plan_pos, upload_pos)
        self.assertLess(upload_pos, pending_pos)
        self.assertIn("PR_HEAD_SHA: ${{ steps.resolve.outputs.pr_head_sha }}", prepare)
        self.assertIn("DEPLOY_MATRIX: ${{ steps.plan.outputs.deploy_matrix }}", prepare)
        self.assertIn("SMOKE_MATRIX: ${{ steps.plan.outputs.smoke_matrix }}", prepare)

    def test_deploy_smoke_self_and_smoke_gitcode_report_always_statuses(self):
        deploy = self._job_section("deploy", "smoke_self")
        self.assertIn("if: always()", deploy)
        self.assertIn(
            "STATUS_CONTEXT: lite-ci-pr-smoke / deploy-${{ matrix.worker }}", deploy
        )
        self.assertIn("JOB_STATUS: ${{ job.status }}", deploy)
        self.assertIn("python3 -m ci_core.pr_status job", deploy)

        smoke_self = self._job_section("smoke_self", "smoke_gitcode")
        self.assertIn("if: always()", smoke_self)
        self.assertIn(
            "STATUS_CONTEXT: lite-ci-pr-smoke / self-${{ matrix.case_id }}",
            smoke_self,
        )
        self.assertIn("JOB_STATUS: ${{ job.status }}", smoke_self)
        self.assertIn("python3 -m ci_core.pr_status job", smoke_self)

        smoke_gitcode = self._job_section("smoke_gitcode", "cleanup")
        self.assertIn("uses: actions/checkout@v4", smoke_gitcode)
        self.assertIn("ref: ${{ needs.prepare.outputs.run_head_sha }}", smoke_gitcode)
        self.assertIn("if: always()", smoke_gitcode)
        self.assertIn(
            "STATUS_CONTEXT: lite-ci-pr-smoke / gitcode-torchtitan-smoke",
            smoke_gitcode,
        )
        self.assertIn("JOB_STATUS: ${{ job.status }}", smoke_gitcode)
        self.assertIn("python3 -m ci_core.pr_status job", smoke_gitcode)

    def test_cleanup_reconciles_skipped_statuses_and_preserves_control_center_gate(
        self,
    ):
        cleanup = self._job_section("cleanup", None)
        reconcile_pos = cleanup.index("python3 -m ci_core.pr_status reconcile")
        report_pos = cleanup.index("Report final PR gate status")
        self.assertLess(reconcile_pos, report_pos)
        self.assertIn(
            "DEPLOY_MATRIX: ${{ needs.prepare.outputs.deploy_matrix }}", cleanup
        )
        self.assertIn(
            "SMOKE_MATRIX: ${{ needs.prepare.outputs.smoke_matrix }}", cleanup
        )
        self.assertIn("DEPLOY_RESULT: ${{ needs.deploy.result }}", cleanup)
        self.assertIn("SMOKE_SELF_RESULT: ${{ needs.smoke_self.result }}", cleanup)
        self.assertIn(
            "SMOKE_GITCODE_RESULT: ${{ needs.smoke_gitcode.result }}", cleanup
        )
        self.assertIn("/api/v1/pr-gate/report", cleanup)
        self.assertNotIn("Lite-Actions-PR-Gate", self.workflow_text)


class PrSmokeResolveScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wf_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/lite_actions_pr_smoke.yml"
        )
        text = wf_path.read_text(encoding="utf-8")
        resolve_section = text.split("        id: resolve\n", 1)[1].split(
            "\n      - name: Register running gate generation\n", 1
        )[0]
        raw_script = resolve_section.split("        run: |\n", 1)[1]
        cls.resolve_script = textwrap.dedent(raw_script)

    def _run_resolve(
        self,
        *,
        event_name: str,
        comment_body: str = "/pr-test",
        comment_author: str = "depeng1994",
        comment_id: str = "987654",
        pr_state: str = "open",
        pr_author: str = "depeng1994",
        head_repo: str = "depeng1994/lite-actions",
        collab_perm: str = "none",
        approved: bool = True,
        fail_reaction: bool = False,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            calls_log = tmp_path / "gh_calls.jsonl"
            gh_output = tmp_path / "github_output.txt"
            gh_output.write_text("", encoding="utf-8")

            pr_json = {
                "state": pr_state,
                "head": {
                    "sha": "d" * 40,
                    "repo": {"full_name": head_repo},
                },
                "user": {"login": pr_author},
            }
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                f"with open({str(calls_log)!r}, 'a', encoding='utf-8') as fh:\n"
                "    fh.write(json.dumps(args) + '\\n')\n"
                "joined = ' '.join(args)\n"
                "if '/actions/runs/' in joined:\n"
                "    print('e' * 40)\n"
                "    sys.exit(0)\n"
                "if '/reactions' in joined:\n"
                f"    sys.exit(1 if {fail_reaction!r} else 0)\n"
                "if '/issues/' in joined and '/comments' in joined:\n"
                "    sys.exit(0)\n"
                "if '/collaborators/' in joined:\n"
                f"    print({collab_perm!r})\n"
                "    sys.exit(0)\n"
                "if '/reviews' in joined:\n"
                f"    print(json.dumps([[{{'approved': {approved!r}}}]]))\n"
                "    sys.exit(0)\n"
                "if '/pulls/' in joined:\n"
                f"    print({json.dumps(pr_json)!r})\n"
                "    sys.exit(0)\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

            fake_jq = bin_dir / "jq"
            fake_jq.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "data = json.loads(sys.stdin.read())\n"
                "if '-e' in args:\n"
                "    ok = bool(data[0][0].get('approved'))\n"
                "    sys.exit(0 if ok else 1)\n"
                "expr = args[-1]\n"
                "if expr == '.state':\n"
                "    print(data['state'])\n"
                "elif expr == '.head.sha':\n"
                "    print(data['head']['sha'])\n"
                "elif expr == '.user.login':\n"
                "    print(data['user']['login'])\n"
                "elif 'head.repo.full_name' in expr:\n"
                "    print((data.get('head', {}).get('repo') or {}).get('full_name') or '')\n"
                "else:\n"
                "    sys.exit(2)\n",
                encoding="utf-8",
            )
            fake_jq.chmod(fake_jq.stat().st_mode | stat.S_IEXEC)

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "GITHUB_REPOSITORY": "depeng1994/lite-actions",
                "GITHUB_RUN_ID": "12345",
                "GITHUB_OUTPUT": str(gh_output),
                "EVENT_NAME": event_name,
                "INPUT_PR_NUMBER": "63" if event_name == "workflow_dispatch" else "",
                "INPUT_GITCODE_REPO": "",
                "INPUT_GITCODE_REF": "",
                "INPUT_PROFILE": "full" if event_name == "workflow_dispatch" else "",
                "ISSUE_PR_NUMBER": "63" if event_name == "issue_comment" else "",
                "COMMENT_ID": comment_id if event_name == "issue_comment" else "",
                "COMMENT_BODY": comment_body if event_name == "issue_comment" else "",
                "COMMENT_AUTHOR": (
                    comment_author if event_name == "issue_comment" else ""
                ),
                "REVIEW_PR_NUMBER": "",
                "REVIEW_AUTHOR": "",
                "PR_MAINTAINERS": "depeng1994",
            }
            proc = subprocess.run(
                ["bash", "-c", self.resolve_script],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = [
                json.loads(line)
                for line in calls_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            outputs = dict(
                line.split("=", 1)
                for line in gh_output.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            return proc, calls, outputs

    def test_valid_issue_comment_posts_rocket_reaction_and_succeeds(self):
        proc, calls, outputs = self._run_resolve(
            event_name="issue_comment", comment_body="/pr-test blue"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(outputs["profile"], "blue-only")
        self.assertEqual(outputs["pr_head_sha"], "d" * 40)
        reaction_calls = [c for c in calls if any("/reactions" in a for a in c)]
        self.assertEqual(len(reaction_calls), 1)
        self.assertIn("content=rocket", reaction_calls[0])
        comment_calls = [c for c in calls if any("/issues/63/comments" in a for a in c)]
        self.assertEqual(comment_calls, [])

    def test_valid_issue_comment_reaction_failure_is_best_effort(self):
        proc, calls, outputs = self._run_resolve(
            event_name="issue_comment",
            comment_body="/pr-test",
            fail_reaction=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(outputs["profile"], "full")
        self.assertIn("::warning::Failed to post 'rocket' reaction", proc.stdout)

    def test_rejected_issue_comments_post_confused_and_pr_comment(self):
        cases = [
            ("unknown_arg", {"comment_body": "/pr-test invalid"}),
            ("closed_pr", {"pr_state": "closed"}),
            (
                "unauthorized_commenter",
                {"comment_author": "random_user", "collab_perm": "read"},
            ),
            (
                "missing_maintainer_approval",
                {
                    "pr_author": "external_contrib",
                    "comment_author": "external_contrib",
                    "head_repo": "external_contrib/lite-actions",
                    "approved": False,
                },
            ),
        ]
        for label, kwargs in cases:
            with self.subTest(label=label):
                proc, calls, _ = self._run_resolve(event_name="issue_comment", **kwargs)
                self.assertNotEqual(proc.returncode, 0)
                reaction_calls = [c for c in calls if any("/reactions" in a for a in c)]
                self.assertEqual(len(reaction_calls), 1)
                self.assertIn("content=confused", reaction_calls[0])
                comment_calls = [
                    c for c in calls if any("/issues/63/comments" in a for a in c)
                ]
                self.assertEqual(len(comment_calls), 1)
                body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
                self.assertIn("Unable to start `/pr-test`:", body_arg)

    def test_workflow_dispatch_rejection_does_not_post_reaction_or_comment(self):
        proc, calls, _ = self._run_resolve(
            event_name="workflow_dispatch", pr_state="closed"
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(any("/reactions" in " ".join(c) for c in calls))
        self.assertFalse(any("/issues/63/comments" in " ".join(c) for c in calls))


if __name__ == "__main__":
    unittest.main()
