"""FIFO hardware queue barrier for per-PR smoke workflow runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ci_core.contract import PR_SMOKE_WORKFLOW

DEFAULT_API_BASE = "https://api.github.com"
PREPARE_JOB_NAME = "prepare"
QUEUE_STEP_NAME = "Wait for FIFO hardware queue"
ACTIVE_JOB_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested"}
)
RETRYABLE_HTTP_CODES = frozenset({403, 429, 500, 502, 503, 504})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def queue_sort_key(run: dict[str, Any]) -> tuple[str, int]:
    """Return deterministic FIFO ordering key for a workflow run.

    Initial attempts order by immutable trigger timestamp (`created_at`) then
    `id`. Re-runs (`run_attempt > 1`) use `run_started_at` so re-running an
    older failed workflow queues behind currently active/waiting runs instead
    of jumping ahead of them.
    """
    run_id = int(run["id"])
    attempt = int(run.get("run_attempt") or 1)
    if attempt > 1:
        ts = str(run.get("run_started_at") or run.get("created_at") or "")
    else:
        ts = str(run.get("created_at") or "")
    return (ts, run_id)


def run_holds_hardware(jobs: list[dict[str, Any]]) -> bool:
    """Return True if a workflow run has already passed the FIFO queue barrier."""
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        status = str(job.get("status") or "").lower()
        conclusion = str(job.get("conclusion") or "").lower()
        if name == PREPARE_JOB_NAME:
            if status == "completed" and conclusion == "success":
                return True
            for step in job.get("steps") or ():
                if (
                    isinstance(step, dict)
                    and str(step.get("name") or "") == QUEUE_STEP_NAME
                    and str(step.get("status") or "").lower() == "completed"
                    and str(step.get("conclusion") or "").lower() == "success"
                ):
                    return True
        elif status in ACTIVE_JOB_STATUSES:
            return True
    return False


def _make_api_get(
    *,
    repo: str,
    token: str,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 20.0,
    opener: Any = None,
) -> Callable[[str], dict[str, Any]]:
    base = api_base.rstrip("/") + f"/repos/{repo.strip()}"
    active_opener = (
        opener if opener is not None else urllib.request.build_opener(_NoRedirect())
    )

    def api_get(path: str) -> dict[str, Any]:
        req = urllib.request.Request(
            base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {token}",
                "User-Agent": "lite-actions-pr-queue",
            },
            method="GET",
        )
        with active_opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}

    return api_get


def _list_workflow_runs(
    api_get: Callable[[str], dict[str, Any]],
    *,
    workflow: str,
    status: str,
    my_run_id: int,
    optional: bool = False,
) -> list[dict[str, Any]]:
    wf_encoded = urllib.parse.quote(str(workflow), safe="")
    rows: list[dict[str, Any]] = []
    for page in range(1, 6):
        query = urllib.parse.urlencode(
            {"status": status, "per_page": 100, "page": page}
        )
        try:
            data = api_get(f"/actions/workflows/{wf_encoded}/runs?{query}")
        except urllib.error.HTTPError as exc:
            if optional and exc.code == 422:
                return []
            raise
        page_rows = data.get("workflow_runs") or []
        for run in page_rows:
            if not isinstance(run, dict) or run.get("id") is None:
                continue
            if int(run["id"]) == my_run_id:
                continue
            if str(run.get("status") or "").lower() == "completed":
                continue
            rows.append(run)
        if len(page_rows) < 100:
            break
    return rows


def find_blocking_runs(
    api_get: Callable[[str], dict[str, Any]],
    *,
    my_run_id: int,
    my_key: tuple[str, int],
    workflow: str = PR_SMOKE_WORKFLOW,
) -> tuple[list[dict[str, Any]], str]:
    """Return `(blocking_runs, reason)` for the current run.

    Uses a single `status=in_progress` request while waiting behind an earlier
    active run, and performs a forward state-transition sweep plus job-level
    holder verification before granting the hardware turn.
    """
    in_progress = _list_workflow_runs(
        api_get, workflow=workflow, status="in_progress", my_run_id=my_run_id
    )
    ahead = [r for r in in_progress if queue_sort_key(r) < my_key]
    if ahead:
        ahead.sort(key=queue_sort_key)
        return ahead, "ahead"

    # Forward transition sweep catches runs moving queued -> in_progress or
    # in_progress <-> waiting right before we claim the hardware turn.
    other_by_id: dict[int, dict[str, Any]] = {int(r["id"]): r for r in in_progress}
    for st, optional in (
        ("requested", True),
        ("pending", True),
        ("queued", False),
        ("waiting", False),
        ("in_progress", False),
    ):
        for run in _list_workflow_runs(
            api_get,
            workflow=workflow,
            status=st,
            my_run_id=my_run_id,
            optional=optional,
        ):
            other_by_id[int(run["id"])] = run

    ahead_all = [r for r in other_by_id.values() if queue_sort_key(r) < my_key]
    if ahead_all:
        ahead_all.sort(key=queue_sort_key)
        return ahead_all, "ahead"

    # Never preempt an active run that already exited the FIFO barrier (e.g.,
    # clock skew or re-run ordering edge cases).
    candidates = [
        r
        for r in other_by_id.values()
        if str(r.get("status") or "").lower() in {"in_progress", "waiting"}
    ]
    candidates.sort(key=queue_sort_key)
    for other in candidates:
        other_id = int(other["id"])
        other_attempt = int(other.get("run_attempt") or 1)
        jobs_doc = api_get(
            f"/actions/runs/{other_id}/attempts/{other_attempt}/jobs?per_page=100"
        )
        if run_holds_hardware(jobs_doc.get("jobs") or []):
            return [other], "holder"

    return [], "ready"


def _retry_after_seconds(exc: Exception, default: float) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        header = (exc.headers.get("Retry-After") or "").strip()
        if header.isdigit():
            return max(1.0, min(300.0, float(int(header))))
    return default


def wait_for_turn(
    *,
    repo: str,
    run_id: int,
    token: str,
    workflow: str = PR_SMOKE_WORKFLOW,
    api_base: str = DEFAULT_API_BASE,
    poll_seconds: float = 30.0,
    timeout_seconds: float = 19800.0,
    max_consecutive_errors: int = 10,
    opener: Any = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    step_summary_path: str | None = None,
) -> dict[str, Any]:
    """Block until `run_id` is next in FIFO order and no other run holds hardware."""
    if not repo.strip():
        raise ValueError("GITHUB_REPOSITORY is required")
    if run_id < 1:
        raise ValueError("GITHUB_RUN_ID must be a positive integer")
    if not token.strip():
        raise ValueError("GH_TOKEN or GITHUB_TOKEN is required")

    api_get = _make_api_get(repo=repo, token=token, api_base=api_base, opener=opener)
    started = monotonic_fn()
    deadline = started + max(1.0, float(timeout_seconds))
    self_run: dict[str, Any] = {}
    for _init_attempt in range(5):
        try:
            self_run = api_get(f"/actions/runs/{run_id}")
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (RETRYABLE_HTTP_CODES | {404}) or _init_attempt == 4:
                raise
            sleep_fn(min(max(0.0, poll_seconds), 3.0))
        except (urllib.error.URLError, OSError, TimeoutError):
            if _init_attempt == 4:
                raise
            sleep_fn(min(max(0.0, poll_seconds), 3.0))
    if str(self_run.get("status") or "").lower() == "completed":
        raise RuntimeError(f"Workflow run #{run_id} is already completed")
    my_key = queue_sort_key(self_run)

    polls = 0
    consecutive_errors = 0
    last_blocking_ids: list[int] = []

    while True:
        polls += 1
        try:
            blocking, reason = find_blocking_runs(
                api_get, my_run_id=run_id, my_key=my_key, workflow=workflow
            )
            consecutive_errors = 0
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            TimeoutError,
            ValueError,
        ) as exc:
            is_retryable = (
                not isinstance(exc, urllib.error.HTTPError)
                or exc.code in RETRYABLE_HTTP_CODES
            )
            consecutive_errors += 1
            if not is_retryable or consecutive_errors >= max_consecutive_errors:
                raise RuntimeError(
                    f"FIFO queue check failed for run #{run_id}: {exc}"
                ) from exc
            remaining = deadline - monotonic_fn()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for FIFO hardware queue for run #{run_id} "
                    f"(last error: {exc})"
                ) from exc
            delay = min(_retry_after_seconds(exc, poll_seconds), remaining)
            print(
                f"[pr-queue] Transient API error ({exc}); retrying in {delay:.0f}s "
                f"({consecutive_errors}/{max_consecutive_errors})",
                flush=True,
            )
            sleep_fn(delay)
            continue

        if not blocking:
            waited = max(0.0, monotonic_fn() - started)
            print(
                f"[pr-queue] Acquired FIFO hardware turn for run #{run_id} "
                f"(waited {waited:.0f}s, polls={polls})",
                flush=True,
            )
            if waited > 0 and step_summary_path:
                try:
                    with open(step_summary_path, "a", encoding="utf-8") as fh:
                        fh.write(f"- FIFO hardware queue wait: `{waited:.0f}s`\n")
                except OSError:
                    pass
            return {"run_id": run_id, "waited_seconds": waited, "polls": polls}

        last_blocking_ids = [int(r["id"]) for r in blocking]
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            raise TimeoutError(
                f"Timed out waiting in FIFO hardware queue for run #{run_id}; "
                f"still blocked by runs {last_blocking_ids}"
            )

        sleep_s = min(poll_seconds * 2.0, 60.0) if len(blocking) >= 2 else poll_seconds
        sleep_s = max(0.0, min(sleep_s, remaining))
        summary = ", ".join(f"#{int(r['id'])}({r.get('status')})" for r in blocking[:5])
        if len(blocking) > 5:
            summary += f", +{len(blocking) - 5} more"
        print(
            f"[pr-queue] Waiting ({reason}, ahead={len(blocking)}): "
            f"[{summary}]; polling again in {sleep_s:.0f}s",
            flush=True,
        )
        sleep_fn(sleep_s)


def _build_parser(
    environ: dict[str, str] | os._Environ[str],
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ci_core.pr_queue",
        description="FIFO hardware queue barrier for per-PR smoke runs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_wait = sub.add_parser("wait", help="Wait for FIFO hardware turn")
    p_wait.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_wait.add_argument("--run-id", default=environ.get("GITHUB_RUN_ID", ""))
    p_wait.add_argument(
        "--workflow", default=environ.get("PR_QUEUE_WORKFLOW", PR_SMOKE_WORKFLOW)
    )
    p_wait.add_argument(
        "--api-base", default=environ.get("GITHUB_API_URL", DEFAULT_API_BASE)
    )
    p_wait.add_argument(
        "--poll-seconds",
        type=float,
        default=float(environ.get("PR_QUEUE_POLL_SECONDS") or 30),
    )
    p_wait.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(environ.get("PR_QUEUE_TIMEOUT_SECONDS") or 19800),
    )
    return parser


def main(
    argv: list[str] | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> int:
    parser = _build_parser(environ)
    args = parser.parse_args(argv)
    if args.command == "wait":
        token = (environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or "").strip()
        raw_run_id = str(args.run_id or "").strip()
        if not raw_run_id.isdigit():
            raise SystemExit("GITHUB_RUN_ID must be a positive integer")
        try:
            wait_for_turn(
                repo=args.repo,
                run_id=int(raw_run_id),
                token=token,
                workflow=args.workflow,
                api_base=args.api_base,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
                step_summary_path=environ.get("GITHUB_STEP_SUMMARY") or None,
            )
        except (ValueError, TimeoutError, RuntimeError) as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
