"""Best-effort GitHub PR Commit Status and comment reaction helper."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_SERVER_URL = "https://github.com"
CONTEXT_PREFIX = "lite-ci-pr-smoke"
GITCODE_SMOKE_CONTEXT = f"{CONTEXT_PREFIX} / gitcode-torchtitan-smoke"
VALID_STATUS_STATES = frozenset({"pending", "success", "failure", "error"})
VALID_REACTIONS = frozenset(
    {"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"}
)
MAX_DESCRIPTION_LEN = 140

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SKIPPED_UPSTREAM_DESCRIPTION = "Skipped because upstream job did not succeed"
_CANCELLED_PENDING_DESCRIPTION = "Cancelled before job completed"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _warn(message: str) -> None:
    print(f"::warning::pr_status: {message}", file=sys.stderr)


def resolve_token(environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    return (environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or "").strip()


def resolve_api_base(
    api_base: str | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> str:
    raw = (api_base or environ.get("GITHUB_API_URL") or DEFAULT_API_BASE).strip()
    return raw.rstrip("/")


def build_run_url(
    repo: str,
    run_id: str,
    server_url: str = DEFAULT_SERVER_URL,
) -> str:
    repo = (repo or "").strip()
    run_id = str(run_id or "").strip()
    base = (server_url or DEFAULT_SERVER_URL).strip().rstrip("/")
    if not repo or not run_id:
        return ""
    return f"{base}/{repo}/actions/runs/{run_id}"


def format_description(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= MAX_DESCRIPTION_LEN:
        return cleaned
    return cleaned[: MAX_DESCRIPTION_LEN - 3].rstrip() + "..."


def deploy_context(worker: str) -> str:
    return f"{CONTEXT_PREFIX} / deploy-{str(worker).strip()}"


def smoke_self_context(case_id: str) -> str:
    return f"{CONTEXT_PREFIX} / self-{str(case_id).strip()}"


def map_job_status(job_status: str) -> str:
    normalized = str(job_status or "").strip().lower()
    if normalized == "success":
        return "success"
    if normalized == "failure":
        return "failure"
    return "error"


def default_job_description(job_status: str, run_id: str = "") -> str:
    normalized = str(job_status or "").strip().lower()
    suffix = f" in PR smoke run #{run_id}" if str(run_id or "").strip() else ""
    if normalized == "success":
        return format_description(f"Passed{suffix}")
    if normalized == "failure":
        return format_description(f"Failed{suffix}")
    if normalized in {"cancelled", "canceled"}:
        return format_description(f"Cancelled{suffix}")
    if normalized == "skipped":
        return format_description(f"Skipped{suffix}")
    label = normalized or "unknown"
    return format_description(f"Ended with status '{label}'{suffix}")


def parse_matrix_entries(raw: Any) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            _warn("Failed to parse matrix JSON")
            return []
    if isinstance(parsed, dict):
        items = parsed.get("include")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def planned_contexts_by_stage(
    deploy_matrix: Any,
    smoke_matrix: Any,
    profile: str,
) -> dict[str, list[str]]:
    deploy_contexts: list[str] = []
    for entry in parse_matrix_entries(deploy_matrix):
        worker = str(entry.get("worker") or "").strip()
        if worker:
            ctx = deploy_context(worker)
            if ctx not in deploy_contexts:
                deploy_contexts.append(ctx)

    smoke_contexts: list[str] = []
    for entry in parse_matrix_entries(smoke_matrix):
        case_id = str(entry.get("case_id") or "").strip()
        if case_id:
            ctx = smoke_self_context(case_id)
            if ctx not in smoke_contexts:
                smoke_contexts.append(ctx)

    gitcode_contexts = (
        [GITCODE_SMOKE_CONTEXT] if str(profile or "").strip().lower() == "full" else []
    )
    return {
        "deploy": deploy_contexts,
        "smoke_self": smoke_contexts,
        "smoke_gitcode": gitcode_contexts,
    }


def planned_contexts(
    deploy_matrix: Any,
    smoke_matrix: Any,
    profile: str,
) -> list[str]:
    stages = planned_contexts_by_stage(deploy_matrix, smoke_matrix, profile)
    result: list[str] = []
    for key in ("deploy", "smoke_self", "smoke_gitcode"):
        for ctx in stages[key]:
            if ctx not in result:
                result.append(ctx)
    return result


def build_status_payload(
    *,
    state: str,
    context: str,
    description: str = "",
    target_url: str = "",
) -> dict[str, str]:
    state_val = str(state or "").strip().lower()
    if state_val not in VALID_STATUS_STATES:
        raise ValueError(f"invalid commit status state: {state!r}")
    context_val = str(context or "").strip()
    if not context_val:
        raise ValueError("commit status context is required")
    payload: dict[str, str] = {
        "state": state_val,
        "context": context_val,
    }
    desc = format_description(description)
    if desc:
        payload["description"] = desc
    url_val = str(target_url or "").strip()
    if url_val:
        payload["target_url"] = url_val
    return payload


def _validate_repo(repo: str) -> str:
    val = str(repo or "").strip()
    if not _REPO_RE.match(val):
        raise ValueError("invalid GitHub repository")
    return val


def _validate_sha(sha: str) -> str:
    val = str(sha or "").strip()
    if not _SHA_RE.match(val):
        raise ValueError("invalid commit SHA")
    return val


def _validate_positive_int(value: int | str, label: str) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"invalid {label}")
    return int(raw)


def _github_request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
    opener: Any = None,
) -> dict[str, Any] | None:
    if not url.startswith(("https://", "http://")):
        _warn("Refusing GitHub API request with invalid URL scheme")
        return None
    body = (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "lite-actions-pr-status",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    active_opener = (
        opener if opener is not None else urllib.request.build_opener(_NoRedirect())
    )
    try:
        with active_opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as exc:
        _warn(f"GitHub API {method} failed with HTTP {exc.code}")
        return None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _warn(f"GitHub API {method} failed: {type(exc).__name__}")
        return None


def post_commit_status(
    *,
    repo: str,
    sha: str,
    state: str,
    context: str,
    description: str = "",
    target_url: str = "",
    token: str | None = None,
    api_base: str | None = None,
    timeout: int = 15,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> bool:
    try:
        repo_val = _validate_repo(repo)
        sha_val = _validate_sha(sha)
        payload = build_status_payload(
            state=state,
            context=context,
            description=description,
            target_url=target_url,
        )
    except ValueError as exc:
        _warn(f"Skipping commit status: {exc}")
        return False
    auth = (token if token is not None else resolve_token(environ)).strip()
    if not auth:
        _warn(f"Missing GitHub token; skipping status '{payload['context']}'")
        return False
    base = resolve_api_base(api_base, environ)
    url = f"{base}/repos/{repo_val}/statuses/{sha_val}"
    resp = _github_request(
        "POST", url, token=auth, payload=payload, timeout=timeout, opener=opener
    )
    return resp is not None


def post_comment_reaction(
    *,
    repo: str,
    comment_id: int | str,
    content: str,
    token: str | None = None,
    api_base: str | None = None,
    timeout: int = 15,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> bool:
    try:
        repo_val = _validate_repo(repo)
        cid = _validate_positive_int(comment_id, "comment_id")
    except ValueError as exc:
        _warn(f"Skipping comment reaction: {exc}")
        return False
    reaction = str(content or "").strip()
    if reaction not in VALID_REACTIONS:
        _warn(f"Skipping unsupported reaction '{reaction}'")
        return False
    auth = (token if token is not None else resolve_token(environ)).strip()
    if not auth:
        _warn("Missing GitHub token; skipping comment reaction")
        return False
    base = resolve_api_base(api_base, environ)
    url = f"{base}/repos/{repo_val}/issues/comments/{cid}/reactions"
    resp = _github_request(
        "POST",
        url,
        token=auth,
        payload={"content": reaction},
        timeout=timeout,
        opener=opener,
    )
    return resp is not None


def post_pr_comment(
    *,
    repo: str,
    pr_number: int | str,
    body: str,
    token: str | None = None,
    api_base: str | None = None,
    timeout: int = 15,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> bool:
    try:
        repo_val = _validate_repo(repo)
        num = _validate_positive_int(pr_number, "pr_number")
    except ValueError as exc:
        _warn(f"Skipping PR comment: {exc}")
        return False
    text = str(body or "").strip()
    if not text:
        _warn("Skipping empty PR comment")
        return False
    auth = (token if token is not None else resolve_token(environ)).strip()
    if not auth:
        _warn("Missing GitHub token; skipping PR comment")
        return False
    base = resolve_api_base(api_base, environ)
    url = f"{base}/repos/{repo_val}/issues/{num}/comments"
    resp = _github_request(
        "POST",
        url,
        token=auth,
        payload={"body": text},
        timeout=timeout,
        opener=opener,
    )
    return resp is not None


def get_combined_status(
    *,
    repo: str,
    sha: str,
    token: str | None = None,
    api_base: str | None = None,
    timeout: int = 15,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> dict[str, Any] | None:
    try:
        repo_val = _validate_repo(repo)
        sha_val = _validate_sha(sha)
    except ValueError as exc:
        _warn(f"Skipping combined status lookup: {exc}")
        return None
    auth = (token if token is not None else resolve_token(environ)).strip()
    if not auth:
        _warn("Missing GitHub token; skipping combined status lookup")
        return None
    base = resolve_api_base(api_base, environ)
    url = f"{base}/repos/{repo_val}/commits/{sha_val}/status"
    return _github_request("GET", url, token=auth, timeout=timeout, opener=opener)


def publish_pending(
    *,
    repo: str,
    sha: str,
    deploy_matrix: Any,
    smoke_matrix: Any,
    profile: str,
    target_url: str = "",
    run_id: str = "",
    token: str | None = None,
    api_base: str | None = None,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> dict[str, bool]:
    contexts = planned_contexts(deploy_matrix, smoke_matrix, profile)
    rid = str(run_id or "").strip()
    description = f"Queued in PR smoke run #{rid}" if rid else "Queued in PR smoke run"
    results: dict[str, bool] = {}
    for ctx in contexts:
        results[ctx] = post_commit_status(
            repo=repo,
            sha=sha,
            state="pending",
            context=ctx,
            description=description,
            target_url=target_url,
            token=token,
            api_base=api_base,
            opener=opener,
            environ=environ,
        )
    return results


def publish_job_status(
    *,
    repo: str,
    sha: str,
    context: str,
    job_status: str,
    description: str = "",
    target_url: str = "",
    run_id: str = "",
    token: str | None = None,
    api_base: str | None = None,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> bool:
    state = map_job_status(job_status)
    desc = (
        description.strip()
        if str(description or "").strip()
        else default_job_description(job_status, run_id)
    )
    return post_commit_status(
        repo=repo,
        sha=sha,
        state=state,
        context=context,
        description=desc,
        target_url=target_url,
        token=token,
        api_base=api_base,
        opener=opener,
        environ=environ,
    )


def reconcile_skipped(
    *,
    repo: str,
    sha: str,
    deploy_matrix: Any,
    smoke_matrix: Any,
    profile: str,
    deploy_result: str,
    smoke_self_result: str,
    smoke_gitcode_result: str,
    target_url: str = "",
    token: str | None = None,
    api_base: str | None = None,
    opener: Any = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> dict[str, bool]:
    stages = planned_contexts_by_stage(deploy_matrix, smoke_matrix, profile)
    stage_results = {
        "deploy": str(deploy_result or "").strip().lower(),
        "smoke_self": str(smoke_self_result or "").strip().lower(),
        "smoke_gitcode": str(smoke_gitcode_result or "").strip().lower(),
    }
    updates: dict[str, bool] = {}
    cancelled_contexts: list[str] = []

    for stage_name in ("deploy", "smoke_self", "smoke_gitcode"):
        contexts = stages[stage_name]
        if not contexts:
            continue
        res = stage_results[stage_name]
        if res == "skipped":
            for ctx in contexts:
                updates[ctx] = post_commit_status(
                    repo=repo,
                    sha=sha,
                    state="error",
                    context=ctx,
                    description=_SKIPPED_UPSTREAM_DESCRIPTION,
                    target_url=target_url,
                    token=token,
                    api_base=api_base,
                    opener=opener,
                    environ=environ,
                )
        elif res in {"cancelled", "canceled"}:
            cancelled_contexts.extend(contexts)

    if cancelled_contexts:
        combined = get_combined_status(
            repo=repo,
            sha=sha,
            token=token,
            api_base=api_base,
            opener=opener,
            environ=environ,
        )
        statuses = combined.get("statuses") if isinstance(combined, dict) else None
        if isinstance(statuses, list):
            by_context = {
                str(item.get("context") or ""): item
                for item in statuses
                if isinstance(item, dict)
            }
            for ctx in cancelled_contexts:
                current = by_context.get(ctx)
                if not current:
                    continue
                if str(current.get("state") or "").lower() != "pending":
                    continue
                if target_url and str(current.get("target_url") or "") != target_url:
                    continue
                updates[ctx] = post_commit_status(
                    repo=repo,
                    sha=sha,
                    state="error",
                    context=ctx,
                    description=_CANCELLED_PENDING_DESCRIPTION,
                    target_url=target_url,
                    token=token,
                    api_base=api_base,
                    opener=opener,
                    environ=environ,
                )

    return updates


def _default_target_url(
    target_url: str,
    repo: str,
    run_id: str,
    environ: dict[str, str] | os._Environ[str],
) -> str:
    if str(target_url or "").strip():
        return str(target_url).strip()
    server_url = environ.get("GITHUB_SERVER_URL") or DEFAULT_SERVER_URL
    return build_run_url(repo, run_id, server_url=server_url)


def _build_parser(
    environ: dict[str, str] | os._Environ[str],
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ci_core.pr_status",
        description="Best-effort GitHub PR Commit Status and reaction publisher.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pending = sub.add_parser(
        "pending", help="Publish pending statuses for all planned PR smoke jobs"
    )
    p_pending.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_pending.add_argument("--sha", default=environ.get("PR_HEAD_SHA", ""))
    p_pending.add_argument("--deploy-matrix", default=environ.get("DEPLOY_MATRIX", ""))
    p_pending.add_argument("--smoke-matrix", default=environ.get("SMOKE_MATRIX", ""))
    p_pending.add_argument("--profile", default=environ.get("PROFILE", "full"))
    p_pending.add_argument("--run-id", default=environ.get("GITHUB_RUN_ID", ""))
    p_pending.add_argument("--target-url", default="")

    p_job = sub.add_parser("job", help="Publish terminal status for a single job")
    p_job.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_job.add_argument("--sha", default=environ.get("PR_HEAD_SHA", ""))
    p_job.add_argument("--context", default=environ.get("STATUS_CONTEXT", ""))
    p_job.add_argument("--job-status", default=environ.get("JOB_STATUS", ""))
    p_job.add_argument("--description", default="")
    p_job.add_argument("--run-id", default=environ.get("GITHUB_RUN_ID", ""))
    p_job.add_argument("--target-url", default="")

    p_reconcile = sub.add_parser(
        "reconcile", help="Update skipped or cancelled pending statuses in cleanup"
    )
    p_reconcile.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_reconcile.add_argument("--sha", default=environ.get("PR_HEAD_SHA", ""))
    p_reconcile.add_argument(
        "--deploy-matrix", default=environ.get("DEPLOY_MATRIX", "")
    )
    p_reconcile.add_argument("--smoke-matrix", default=environ.get("SMOKE_MATRIX", ""))
    p_reconcile.add_argument("--profile", default=environ.get("PROFILE", "full"))
    p_reconcile.add_argument(
        "--deploy-result", default=environ.get("DEPLOY_RESULT", "")
    )
    p_reconcile.add_argument(
        "--smoke-self-result", default=environ.get("SMOKE_SELF_RESULT", "")
    )
    p_reconcile.add_argument(
        "--smoke-gitcode-result", default=environ.get("SMOKE_GITCODE_RESULT", "")
    )
    p_reconcile.add_argument("--run-id", default=environ.get("GITHUB_RUN_ID", ""))
    p_reconcile.add_argument("--target-url", default="")

    p_reaction = sub.add_parser("reaction", help="Post a reaction on an issue comment")
    p_reaction.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_reaction.add_argument("--comment-id", default=environ.get("COMMENT_ID", ""))
    p_reaction.add_argument("--content", required=True)

    p_comment = sub.add_parser("comment", help="Post a comment on a pull request")
    p_comment.add_argument("--repo", default=environ.get("GITHUB_REPOSITORY", ""))
    p_comment.add_argument("--pr-number", default=environ.get("PR_NUMBER", ""))
    p_comment.add_argument("--body", required=True)

    return parser


def main(
    argv: list[str] | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> int:
    try:
        parser = _build_parser(environ)
        args = parser.parse_args(argv)
        if args.command == "pending":
            target_url = _default_target_url(
                args.target_url, args.repo, args.run_id, environ
            )
            publish_pending(
                repo=args.repo,
                sha=args.sha,
                deploy_matrix=args.deploy_matrix,
                smoke_matrix=args.smoke_matrix,
                profile=args.profile,
                target_url=target_url,
                run_id=args.run_id,
                environ=environ,
            )
        elif args.command == "job":
            target_url = _default_target_url(
                args.target_url, args.repo, args.run_id, environ
            )
            publish_job_status(
                repo=args.repo,
                sha=args.sha,
                context=args.context,
                job_status=args.job_status,
                description=args.description,
                target_url=target_url,
                run_id=args.run_id,
                environ=environ,
            )
        elif args.command == "reconcile":
            target_url = _default_target_url(
                args.target_url, args.repo, args.run_id, environ
            )
            reconcile_skipped(
                repo=args.repo,
                sha=args.sha,
                deploy_matrix=args.deploy_matrix,
                smoke_matrix=args.smoke_matrix,
                profile=args.profile,
                deploy_result=args.deploy_result,
                smoke_self_result=args.smoke_self_result,
                smoke_gitcode_result=args.smoke_gitcode_result,
                target_url=target_url,
                environ=environ,
            )
        elif args.command == "reaction":
            post_comment_reaction(
                repo=args.repo,
                comment_id=args.comment_id,
                content=args.content,
                environ=environ,
            )
        elif args.command == "comment":
            post_pr_comment(
                repo=args.repo,
                pr_number=args.pr_number,
                body=args.body,
                environ=environ,
            )
    except SystemExit:
        raise
    except Exception as exc:
        _warn(f"Unexpected error: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
