"""End-to-end ST tests for per-PR smoke FIFO hardware queueing."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ci_core import pr_queue


class _FakeGitHubActionsState:
    """Thread-safe in-memory GitHub Actions REST state for end-to-end queue ST."""

    def __init__(self, repo: str = "depeng1994/lite-actions"):
        self.repo = repo
        self.lock = threading.Lock()
        self.runs: dict[int, dict] = {}
        self.jobs: dict[tuple[int, int], list[dict]] = {}
        self.inject_errors: list[tuple[int, dict[str, str]]] = []
        self.requests: list[str] = []

    def put_run(self, run: dict, jobs: list[dict] | None = None) -> None:
        with self.lock:
            run_id = int(run["id"])
            attempt = int(run.get("run_attempt") or 1)
            self.runs[run_id] = dict(run)
            if jobs is not None:
                self.jobs[(run_id, attempt)] = list(jobs)

    def set_status(
        self, run_id: int, status: str, conclusion: str | None = None
    ) -> None:
        with self.lock:
            self.runs[run_id]["status"] = status
            self.runs[run_id]["conclusion"] = conclusion

    def set_jobs(self, run_id: int, attempt: int, jobs: list[dict]) -> None:
        with self.lock:
            self.jobs[(run_id, attempt)] = list(jobs)


def _start_fake_github(state: _FakeGitHubActionsState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _send_json(
            self, code: int, payload: dict, headers: dict[str, str] | None = None
        ):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            prefix = f"/repos/{state.repo}"
            rel = (
                parsed.path[len(prefix) :]
                if parsed.path.startswith(prefix)
                else parsed.path
            )

            with state.lock:
                state.requests.append(f"{rel}?{parsed.query}" if parsed.query else rel)
                if state.inject_errors and "/actions/workflows/" in rel:
                    err_code, err_headers = state.inject_errors.pop(0)
                    self._send_json(
                        err_code, {"message": "injected transient"}, err_headers
                    )
                    return

                parts = [p for p in rel.split("/") if p]
                # /actions/runs/{run_id}
                if len(parts) == 3 and parts[0] == "actions" and parts[1] == "runs":
                    run_id = int(parts[2])
                    run = state.runs.get(run_id)
                    if not run:
                        self._send_json(404, {"message": "not found"})
                        return
                    self._send_json(200, dict(run))
                    return

                # /actions/workflows/{workflow}/runs?status=...
                if (
                    len(parts) == 4
                    and parts[0] == "actions"
                    and parts[1] == "workflows"
                    and parts[3] == "runs"
                ):
                    want_status = (qs.get("status") or [""])[0]
                    matched = [
                        dict(r)
                        for r in state.runs.values()
                        if not want_status or r.get("status") == want_status
                    ]
                    matched.sort(key=lambda r: int(r["id"]), reverse=True)
                    self._send_json(
                        200, {"total_count": len(matched), "workflow_runs": matched}
                    )
                    return

                # /actions/runs/{run_id}/attempts/{attempt}/jobs
                if (
                    len(parts) == 6
                    and parts[0] == "actions"
                    and parts[1] == "runs"
                    and parts[3] == "attempts"
                    and parts[5] == "jobs"
                ):
                    run_id = int(parts[2])
                    attempt = int(parts[4])
                    jobs = list(state.jobs.get((run_id, attempt), []))
                    self._send_json(200, {"total_count": len(jobs), "jobs": jobs})
                    return

            self._send_json(404, {"message": f"unexpected path {rel}"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


class PrQueueEndToEndST(unittest.TestCase):
    REPO = "depeng1994/lite-actions"

    def setUp(self):
        self.state = _FakeGitHubActionsState(self.REPO)
        self.server, self.api_base = _start_fake_github(self.state)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _env_for(
        self, run_id: int, *, poll: str = "0.05", timeout: str = "15"
    ) -> dict[str, str]:
        return {
            "GH_TOKEN": "test-token",
            "GITHUB_REPOSITORY": self.REPO,
            "GITHUB_RUN_ID": str(run_id),
            "GITHUB_API_URL": self.api_base,
            "PR_QUEUE_WORKFLOW": "lite_actions_pr_smoke.yml",
            "PR_QUEUE_POLL_SECONDS": poll,
            "PR_QUEUE_TIMEOUT_SECONDS": timeout,
        }

    def test_st_three_pr_runs_serialize_fifo_unaffected_by_issue_comments(self):
        """ST-1: 3 concurrent PR smoke runs + noisy Issue #49 comments execute strictly FIFO."""
        # Populate 120 completed/skipped Issue #49 comment runs that would have
        # cancelled pending runs under a global concurrency group or overflowed
        # an unfiltered runs listing.
        for idx in range(1, 121):
            self.state.put_run(
                {
                    "id": 9000 + idx,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "skipped",
                    "created_at": "2026-04-20T12:05:00Z",
                    "display_title": "Lite Actions PR smoke: #49",
                }
            )

        # Three real PR smoke runs triggered back-to-back: PR #64, PR #60, PR #61.
        # Run 101 starts in `queued` (runner booting slowly) while 102 and 103 are
        # already `in_progress` inside prepare, proving 102/103 do not jump ahead.
        self.state.put_run(
            {
                "id": 101,
                "run_attempt": 1,
                "status": "queued",
                "created_at": "2026-04-20T12:00:00Z",
            },
            jobs=[
                {"name": "prepare", "status": "queued", "conclusion": None, "steps": []}
            ],
        )
        self.state.put_run(
            {
                "id": 102,
                "run_attempt": 1,
                "status": "in_progress",
                "created_at": "2026-04-20T12:00:01Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [
                        {
                            "name": pr_queue.QUEUE_STEP_NAME,
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    ],
                }
            ],
        )
        self.state.put_run(
            {
                "id": 103,
                "run_attempt": 1,
                "status": "in_progress",
                "created_at": "2026-04-20T12:00:02Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [
                        {
                            "name": pr_queue.QUEUE_STEP_NAME,
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    ],
                }
            ],
        )

        # Also add one transient Issue #49 comment run that briefly appears queued
        # before GitHub skips it.
        self.state.put_run(
            {
                "id": 9999,
                "run_attempt": 1,
                "status": "queued",
                "created_at": "2026-04-20T12:00:03Z",
            }
        )

        order: list[int] = []
        active_holders: list[int] = []
        max_concurrent = 0
        rc_by_run: dict[int, int] = {}
        order_lock = threading.Lock()

        def run_worker(run_id: int, delay_before_start: float = 0.0):
            nonlocal max_concurrent
            if delay_before_start:
                time.sleep(delay_before_start)
                self.state.set_status(run_id, "in_progress")
            rc = pr_queue.main(["wait"], environ=self._env_for(run_id))
            rc_by_run[run_id] = rc
            if rc != 0:
                return
            with order_lock:
                order.append(run_id)
                active_holders.append(run_id)
                max_concurrent = max(max_concurrent, len(active_holders))
            # Simulate hardware execution including `waiting` (environment gate)
            # and `cleanup` before completion.
            self.state.set_jobs(
                run_id,
                1,
                [
                    {
                        "name": "prepare",
                        "status": "completed",
                        "conclusion": "success",
                        "steps": [],
                    },
                    {
                        "name": "gitcode-torchtitan-smoke",
                        "status": "waiting",
                        "conclusion": None,
                    },
                ],
            )
            self.state.set_status(run_id, "waiting")
            time.sleep(0.15)
            self.state.set_status(run_id, "in_progress")
            self.state.set_jobs(
                run_id,
                1,
                [
                    {
                        "name": "prepare",
                        "status": "completed",
                        "conclusion": "success",
                        "steps": [],
                    },
                    {
                        "name": "cleanup-and-report",
                        "status": "in_progress",
                        "conclusion": None,
                    },
                ],
            )
            time.sleep(0.1)
            with order_lock:
                active_holders.remove(run_id)
            self.state.set_status(run_id, "completed", "success")

        # Boot 103 and 102 FIRST while 101 is still queued, and skip Issue #49 comment.
        t3 = threading.Thread(target=run_worker, args=(103,))
        t2 = threading.Thread(target=run_worker, args=(102,))
        t1 = threading.Thread(target=run_worker, args=(101, 0.15))
        t3.start()
        t2.start()
        t1.start()
        time.sleep(0.08)
        self.state.set_status(9999, "completed", "skipped")

        for t in (t1, t2, t3):
            t.join(timeout=15)
            self.assertFalse(t.is_alive())

        self.assertEqual(rc_by_run, {101: 0, 102: 0, 103: 0})
        self.assertEqual(order, [101, 102, 103])
        self.assertEqual(max_concurrent, 1)

    def test_st_rerun_of_older_run_never_preempts_active_hardware_holder(self):
        """ST-2: Re-running old run #50 (attempt 2) waits behind active run #150 even under clock skew."""
        # Run 150 already completed the queue step and is running register/deploy.
        self.state.put_run(
            {
                "id": 150,
                "run_attempt": 1,
                "status": "in_progress",
                "created_at": "2026-04-20T12:00:05Z",
                "run_started_at": "2026-04-20T12:00:06Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [
                        {
                            "name": pr_queue.QUEUE_STEP_NAME,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                }
            ],
        )
        # Re-run of an older run (#50) whose run_started_at has 2s clock skew
        # earlier than run #150: job-level barrier check must still block it!
        self.state.put_run(
            {
                "id": 50,
                "run_attempt": 2,
                "status": "in_progress",
                "created_at": "2026-04-19T08:00:00Z",
                "run_started_at": "2026-04-20T12:00:03Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [
                        {
                            "name": pr_queue.QUEUE_STEP_NAME,
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    ],
                }
            ],
        )

        acquired_at: list[float] = []
        rc_holder: list[int] = []

        def wait_rerun():
            rc_holder.append(pr_queue.main(["wait"], environ=self._env_for(50)))
            acquired_at.append(time.monotonic())

        t = threading.Thread(target=wait_rerun)
        t.start()
        time.sleep(0.25)
        # Run 50 must still be waiting while Run 150 holds hardware!
        self.assertTrue(t.is_alive())
        release_time = time.monotonic()
        self.state.set_status(150, "completed", "success")
        t.join(timeout=10)

        self.assertFalse(t.is_alive())
        self.assertEqual(rc_holder, [0])
        self.assertGreaterEqual(acquired_at[0], release_time)

    def test_st_transient_github_api_errors_recover_without_aborting_queue_wait(self):
        """ST-3: Transient 503/429 responses during queue polling retry cleanly until turn is ready."""
        self.state.put_run(
            {
                "id": 201,
                "run_attempt": 1,
                "status": "in_progress",
                "created_at": "2026-04-20T12:00:00Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [],
                }
            ],
        )
        self.state.put_run(
            {
                "id": 202,
                "run_attempt": 1,
                "status": "in_progress",
                "created_at": "2026-04-20T12:00:05Z",
            },
            jobs=[
                {
                    "name": "prepare",
                    "status": "in_progress",
                    "conclusion": None,
                    "steps": [],
                }
            ],
        )
        self.state.inject_errors.extend([(503, {}), (429, {"Retry-After": "0"})])

        def complete_first():
            time.sleep(0.2)
            self.state.set_status(201, "completed", "success")

        releaser = threading.Thread(target=complete_first)
        releaser.start()
        rc = pr_queue.main(
            ["wait"], environ=self._env_for(202, poll="0.05", timeout="10")
        )
        releaser.join(timeout=5)

        self.assertEqual(rc, 0)
        self.assertEqual(self.state.inject_errors, [])


if __name__ == "__main__":
    unittest.main()
