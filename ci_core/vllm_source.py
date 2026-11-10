"""Verify the exact GitHub source used by a model-owned smoke plan."""

from __future__ import annotations

import io
import json
import os
import re
import urllib.parse
import zipfile

from ci_core.contract import VLLM_REPO
from ci_core.vllm_ascend import CASES as LEGACY_A5_CASES

REPO_ID = 1438625957
SOURCE_KEYS = {
    "schema",
    "provider",
    "repository",
    "repository_id",
    "run_id",
    "attempt",
    "plan_digest",
    "control_sha",
    "head_sha",
    "base_ref",
    "test_ids",
    "mode",
    "pr_number",
    "head_repo_id",
    "base_sha",
    "source_sha",
    "tree_sha",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def requires_source(plan):
    if plan.get("repo") != VLLM_REPO:
        return False
    legacy = [c.get("test_id") in LEGACY_A5_CASES for c in plan.get("cases", [])]
    if any(legacy) and not all(legacy):
        raise ValueError("legacy A5 and model-owned smoke cannot share a plan")
    return bool(legacy) and not any(legacy)


def validate_source(source, run, plan, approved_controls):
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        raise ValueError("invalid native source structure")
    expected = {
        "schema": 1,
        "provider": "github",
        "repository": VLLM_REPO,
        "repository_id": REPO_ID,
        "run_id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "plan_digest": plan["plan_digest"],
        "head_sha": plan["sha"],
        "test_ids": [c["test_id"] for c in plan["cases"]],
    }
    if any(source.get(k) != v for k, v in expected.items()):
        raise ValueError("native source/plan identity mismatch")
    for key in (
        "schema",
        "repository_id",
        "run_id",
        "attempt",
        "pr_number",
        "head_repo_id",
    ):
        if type(source[key]) is not int:
            raise ValueError("native source integer type mismatch")
    for key in ("control_sha", "head_sha", "base_sha", "source_sha", "tree_sha"):
        if not isinstance(source[key], str) or not SHA.fullmatch(source[key]):
            raise ValueError("native source SHA invalid")
    if (
        not isinstance(source["base_ref"], str)
        or not source["base_ref"]
        or len(source["base_ref"]) > 255
        or any(ord(c) < 32 for c in source["base_ref"])
    ):
        raise ValueError("invalid native base ref")
    control = source["control_sha"]
    if control not in approved_controls or control != run.get(
        "_ci_control_sha", run["head_sha"]
    ):
        raise ValueError("unapproved native controller release")
    if not source["test_ids"]:
        raise ValueError("empty native source case set")
    if source["mode"] == "merge_result":
        if (
            source["pr_number"] != run.get("_ci_pr_number")
            or source["head_repo_id"] != run.get("_ci_source_repo_id")
            or source["head_repo_id"] < 1
            or source["base_ref"] != run.get("_ci_base_ref")
        ):
            raise ValueError("native merge PR binding mismatch")
    elif source["mode"] == "commit":
        if (
            source["pr_number"] != 0
            or source["head_repo_id"] != REPO_ID
            or run.get("event") != "workflow_dispatch"
            or source["base_ref"] != run.get("head_branch")
            or any(source[k] != control for k in ("head_sha", "base_sha", "source_sha"))
        ):
            raise ValueError("native manual source binding mismatch")
    else:
        raise ValueError("unsupported native source mode")
    return source


def check_current(client, source, *, check_run=False):
    """Fail closed at admission, post-staging launch and result publication."""
    if client.repo != VLLM_REPO:
        raise ValueError("wrong native source repository client")
    if source["mode"] == "merge_result":
        pr = client.request("GET", f"/pulls/{source['pr_number']}")
        pr_head = pr.get("head") or {}
        pr_base = pr.get("base") or {}
        head_repo = pr_head.get("repo") or {}
        base_repo = pr_base.get("repo") or {}
        base = (
            client.request(
                "GET", "/branches/" + urllib.parse.quote(source["base_ref"], safe="")
            )
            .get("commit", {})
            .get("sha")
        )
        if (
            pr.get("state") != "open"
            or pr.get("merged")
            or pr.get("number") != source["pr_number"]
            or head_repo.get("id") != source["head_repo_id"]
            or pr_head.get("sha") != source["head_sha"]
            or base_repo.get("id") != REPO_ID
            or base_repo.get("full_name") != VLLM_REPO
            or pr_base.get("ref") != source["base_ref"]
            or base != source["base_sha"]
            or pr.get("mergeable") is False
            or pr.get("merge_commit_sha") != source["source_sha"]
        ):
            raise ValueError("STALE_NATIVE_SOURCE: Head/Base/authorization changed")
    if check_run:
        run = client.workflow_status(source["run_id"])
        if (
            run.get("status") not in {"queued", "in_progress"}
            or run.get("run_attempt") != source["attempt"]
        ):
            raise ValueError("NATIVE_RUN_CANCELLED_OR_SUPERSEDED")


def prepare_ready(client, run):
    jobs, total = [], None
    for page in range(1, 11):
        data = client.request(
            "GET",
            f"/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100&page={page}",
        )
        count, batch = data.get("total_count"), data.get("jobs")
        if (
            type(count) is not int
            or not 0 <= count <= 1000
            or not isinstance(batch, list)
            or len(batch) > 100
        ):
            raise ValueError("invalid Prepare job listing")
        if total is not None and count != total:
            return False  # Jobs are still being added; poll a fresh listing.
        total = count
        jobs.extend(batch)
        if len(jobs) == total:
            break
        if not batch or len(jobs) > total:
            return False
    else:
        raise ValueError("Prepare job visibility incomplete")
    prepare = [j for j in jobs if j.get("name") == "prepare"]
    return (
        len(prepare) == 1
        and prepare[0].get("run_id") == run["id"]
        and prepare[0].get("run_attempt") == run.get("run_attempt", 1)
        and prepare[0].get("status") == "completed"
        and prepare[0].get("conclusion") == "success"
    )


def approved_controls(client, run, candidate=None, *, merge_source=False):
    """Resolve the live trusted workflow branch without pinning every commit."""
    if run.get("event") == "pull_request_target":
        repo = client.request("GET", "")
        if repo.get("id") != REPO_ID:
            raise ValueError("unexpected controller repository")
        branch = repo.get("default_branch")
        explicit = set()
    elif run.get("event") == "workflow_dispatch":
        branch = run.get("head_branch")
        allowed = set(
            os.environ.get("LITE_ACTIONS_VLLM_ALLOWED_BRANCHES", "main").split(",")
        )
        if branch not in allowed:
            raise ValueError("unapproved controller branch")
        explicit = set(
            filter(
                None, os.environ.get("LITE_ACTIONS_VLLM_CONTROL_SHAS", "").split(",")
            )
        )
    else:
        raise ValueError("unapproved controller event")
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise ValueError("invalid controller branch")
    head = (
        client.request("GET", "/branches/" + urllib.parse.quote(branch, safe=""))
        .get("commit", {})
        .get("sha")
    )
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("invalid controller branch SHA")
    is_pr_control = run.get("event") == "pull_request_target" or (
        run.get("event") == "workflow_dispatch"
        and merge_source
        and type(run.get("_ci_pr_number")) is int
    )
    if is_pr_control and candidate != head:
        if not isinstance(candidate, str) or not SHA.fullmatch(candidate):
            raise ValueError("invalid controller SHA")
        comparison = client.request("GET", f"/compare/{candidate}...{head}?per_page=1")
        if comparison.get("merge_base_commit", {}).get("sha") != candidate:
            raise ValueError("controller is not in trusted default branch history")
        explicit.add(candidate)
    return explicit | {head}


def read_source(client, run, plan):
    if not prepare_ready(client, run):
        return None
    name = f"lite-ci-source-{run.get('run_attempt', 1)}"
    listing = client.request(
        "GET", f"/actions/runs/{run['id']}/artifacts?name={name}&per_page=10"
    )
    matches = [
        a
        for a in listing.get("artifacts", [])
        if a.get("name") == name and not a.get("expired")
    ]
    if len(matches) != 1 or matches[0].get("size_in_bytes", 0) > 131072:
        raise ValueError("missing/ambiguous/oversized native source artifact")
    artifact = matches[0]
    if artifact.get("workflow_run", {}).get("id", run["id"]) != run["id"]:
        raise ValueError("native source artifact belongs to another run")
    payload = client.download_artifact(artifact["id"], limit=131072)
    with zipfile.ZipFile(io.BytesIO(payload)) as zipped:
        if (
            zipped.namelist() != ["source.json"]
            or zipped.getinfo("source.json").file_size > 16384
        ):
            raise ValueError("invalid native source archive")
        source = json.loads(zipped.read("source.json"))
    candidate = source.get("control_sha") if isinstance(source, dict) else None
    validate_source(
        source,
        run,
        plan,
        approved_controls(
            client,
            run,
            candidate,
            merge_source=isinstance(source, dict)
            and source.get("mode") == "merge_result",
        ),
    )
    commit = client.request("GET", "/git/commits/" + source["source_sha"])
    if (
        commit.get("sha") != source["source_sha"]
        or commit.get("tree", {}).get("sha") != source["tree_sha"]
    ):
        raise ValueError("native source commit/tree proof mismatch")
    if source["mode"] == "merge_result" and [
        p.get("sha") for p in commit.get("parents", [])
    ] != [source["base_sha"], source["head_sha"]]:
        raise ValueError("native source merge parents mismatch")
    check_current(client, source, check_run=True)
    return source
