"""End-to-end ST coverage for PR commit status reconciliation and /pr-test feedback."""

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
import unittest.mock
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path

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
                "body": body,
            }
        )
        if self._responses:
            nxt = self._responses.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return _FakeResponse(nxt)
        return _FakeResponse({"id": 1})


class PrFeedbackEndToEndST(unittest.TestCase):
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

    # =========================================================================
    # ST-1：拦截上游失败或中途取消导致 PR 页面子任务 Commit Status 永久卡在 pending，
    #       以及状态回写接口抖动导致真实 CI 任务被误判失败的故障模式
    # =========================================================================
    def test_st_commit_status_reconciles_upstream_failure_and_cancellation(self):
        # 场景 A：prepare 挂起 pending 后，deploy 中途被取消（blue 已 failure，yellow 仍 pending），
        #        cleanup reconcile 必须收口所有被跳过及本轮仍 pending 的子项，且不覆盖已终态项。
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
                {"id": 1},
                {"id": 2},
                {"id": 3},
                {"id": 4},
                {"id": 5},
                {"id": 6},
                {"id": 7},
                {"id": 8},
                combined_resp,
                {"id": 9},
            ]
        )
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
        self.assertEqual(len(pending), 5)
        self.assertTrue(all(pending.values()))

        opener.calls.clear()
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
            set(updates.keys()),
            {
                "lite-ci-pr-smoke / self-case_a",
                "lite-ci-pr-smoke / self-case_b",
                "lite-ci-pr-smoke / gitcode-torchtitan-smoke",
                "lite-ci-pr-smoke / deploy-yellow",
            },
        )
        self.assertNotIn("lite-ci-pr-smoke / deploy-blue", updates)
        post_calls = [c for c in opener.calls if c["method"] == "POST"]
        self.assertTrue(all(c["body"]["state"] == "error" for c in post_calls))

        # 场景 B：GitHub Status API 返回 500 时，CLI 仍必须 best-effort 返回 0 且不泄露 token。
        secret = "ghs_secret_token_should_not_leak"
        err_buf = io.StringIO()
        http_err = urllib.error.HTTPError(
            url=f"https://api.github.com/repos/{self.REPO}/statuses/{self.SHA}",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"server error"),
        )
        with (
            redirect_stderr(err_buf),
            unittest.mock.patch(
                "urllib.request.build_opener",
                return_value=_RecordingOpener(responses=[http_err]),
            ),
            unittest.mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": secret,
                    "GITHUB_REPOSITORY": self.REPO,
                    "GITHUB_RUN_ID": "5001",
                    "PR_HEAD_SHA": self.SHA,
                    "STATUS_CONTEXT": "lite-ci-pr-smoke / deploy-blue",
                    "JOB_STATUS": "success",
                },
                clear=False,
            ),
        ):
            rc = pr_status.main(["job"])
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, err_buf.getvalue())

    # =========================================================================
    # ST-2：拦截 /pr-test 评论触发后页面静默无反馈的故障模式（通过贴 rocket、
    #       拒绝贴 confused + 原因评论，且 reaction 接口异常不阻断正常触发）
    # =========================================================================
    def _run_resolve_script(
        self,
        *,
        comment_body: str = "/pr-test",
        comment_author: str = "depeng1994",
        pr_author: str = "depeng1994",
        head_repo: str = "depeng1994/lite-actions",
        approved: bool = True,
        fail_reaction: bool = False,
    ):
        wf_path = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/lite_actions_pr_smoke.yml"
        )
        text = wf_path.read_text(encoding="utf-8")
        resolve_section = text.split("        id: resolve\n", 1)[1].split(
            "\n      - name: Register running gate generation\n", 1
        )[0]
        resolve_script = textwrap.dedent(
            resolve_section.split("        run: |\n", 1)[1]
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            calls_log = tmp_path / "gh_calls.jsonl"
            gh_output = tmp_path / "github_output.txt"
            gh_output.write_text("", encoding="utf-8")

            pr_json = {
                "state": "open",
                "head": {"sha": "d" * 40, "repo": {"full_name": head_repo}},
                "user": {"login": pr_author},
            }
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                f"with open({str(calls_log)!r}, 'a', encoding='utf-8') as fh:\n"
                "    fh.write(json.dumps(args) + '\\n')\n"
                "joined = ' '.join(args)\n"
                "if '/actions/runs/' in joined:\n"
                "    print('e' * 40); sys.exit(0)\n"
                "if '/reactions' in joined:\n"
                f"    sys.exit(1 if {fail_reaction!r} else 0)\n"
                "if '/reviews' in joined:\n"
                f"    print(json.dumps([[{{'approved': {approved!r}}}]]))\n"
                "    sys.exit(0)\n"
                "if '/pulls/' in joined:\n"
                f"    print({json.dumps(pr_json)!r}); sys.exit(0)\n"
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
                "    sys.exit(0 if data[0][0].get('approved') else 1)\n"
                "expr = args[-1]\n"
                "if expr == '.state': print(data['state'])\n"
                "elif expr == '.head.sha': print(data['head']['sha'])\n"
                "elif expr == '.user.login': print(data['user']['login'])\n"
                "elif 'head.repo.full_name' in expr:\n"
                "    print((data.get('head', {}).get('repo') or {}).get('full_name') or '')\n"
                "else: sys.exit(2)\n",
                encoding="utf-8",
            )
            fake_jq.chmod(fake_jq.stat().st_mode | stat.S_IEXEC)

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "GITHUB_REPOSITORY": self.REPO,
                "GITHUB_RUN_ID": "12345",
                "GITHUB_OUTPUT": str(gh_output),
                "EVENT_NAME": "issue_comment",
                "INPUT_PR_NUMBER": "",
                "INPUT_GITCODE_REPO": "",
                "INPUT_GITCODE_REF": "",
                "INPUT_PROFILE": "",
                "ISSUE_PR_NUMBER": "63",
                "COMMENT_ID": "987654",
                "COMMENT_BODY": comment_body,
                "COMMENT_AUTHOR": comment_author,
                "REVIEW_PR_NUMBER": "",
                "REVIEW_AUTHOR": "",
                "PR_MAINTAINERS": "depeng1994",
            }
            proc = subprocess.run(
                ["bash", "-c", resolve_script],
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
            return proc, calls

    def test_st_pr_test_comment_gives_immediate_feedback_on_accept_and_reject(self):
        # 正常触发：贴 rocket，不额外刷拒绝评论
        proc, calls = self._run_resolve_script(comment_body="/pr-test")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(
            any("/reactions" in " ".join(c) and "content=rocket" in c for c in calls)
        )
        self.assertFalse(any("/issues/63/comments" in " ".join(c) for c in calls))

        # Reaction API 失败时降级放行，不阻断测试启动
        proc_degraded, _ = self._run_resolve_script(
            comment_body="/pr-test", fail_reaction=True
        )
        self.assertEqual(proc_degraded.returncode, 0, proc_degraded.stderr)

        # 未 Approve 的外部 Fork PR 被拒时：贴 confused 并回帖说明原因
        proc_rej, calls_rej = self._run_resolve_script(
            pr_author="external_contrib",
            comment_author="external_contrib",
            head_repo="external_contrib/lite-actions",
            approved=False,
        )
        self.assertNotEqual(proc_rej.returncode, 0)
        self.assertTrue(
            any(
                "/reactions" in " ".join(c) and "content=confused" in c
                for c in calls_rej
            )
        )
        self.assertTrue(
            any(
                "/issues/63/comments" in " ".join(c)
                and any("Unable to start `/pr-test`:" in a for a in c)
                for c in calls_rej
            )
        )


if __name__ == "__main__":
    unittest.main()
