"""Independent GitHub-native Blue source admission, preserving schema-3 wire data.

A separate immutable source artifact binds to the exact plan digest. Legacy A5
consumers continue to ignore Blue-owned cases; no schema-4/App dependency is
introduced on this GitHub-native path.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile

from ci_core.contract import VLLM_REPO
from ci_core.vllm_ascend import BLUE_CASES

REPO_ID = 1438625957
FORK_ID = 1440390070
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


def is_blue_plan(plan):
    return plan.get("repo") == VLLM_REPO and any(
        c.get("test_id") in BLUE_CASES for c in plan.get("cases", [])
    )


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
        "base_ref": "main",
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
    control = source["control_sha"]
    if control not in approved_controls or control != run.get(
        "_ci_control_sha", run["head_sha"]
    ):
        raise ValueError("unapproved native controller release")
    if not source["test_ids"] or any(c not in BLUE_CASES for c in source["test_ids"]):
        raise ValueError("mixed or unapproved Blue source case set")
    if source["mode"] == "merge_result":
        if (
            source["pr_number"] != run.get("_ci_pr_number")
            or source["head_repo_id"] != FORK_ID
        ):
            raise ValueError("native merge PR binding mismatch")
    elif source["mode"] == "commit":
        if (
            source["pr_number"] != 0
            or source["head_repo_id"] != REPO_ID
            or run.get("event") != "workflow_dispatch"
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
        base = client.request("GET", "/branches/main").get("commit", {}).get("sha")
        if (
            pr.get("state") != "open"
            or pr.get("merged")
            or pr.get("draft")
            or pr.get("number") != source["pr_number"]
            or pr.get("user", {}).get("login") != "depeng1994"
            or pr.get("head", {}).get("repo", {}).get("id") != FORK_ID
            or pr["head"]["repo"].get("full_name") != "depeng1994/vllm-ascend"
            or pr["head"].get("sha") != source["head_sha"]
            or pr.get("base", {}).get("repo", {}).get("id") != REPO_ID
            or pr["base"]["repo"].get("full_name") != VLLM_REPO
            or pr["base"].get("ref") != "main"
            or base != source["base_sha"]
        ):
            raise ValueError("STALE_NATIVE_SOURCE: Head/Base/authorization changed")
        labels = {x["name"] for x in pr.get("labels", [])}
        tests = ["vllm_worker_os_blue", "dispatch_smoke_a3_8p"]
        if "lite-a3-16p" in labels:
            tests.append("dispatch_smoke_a3_16p")
        if "lite-a3-blue" not in labels or tests != source["test_ids"]:
            raise ValueError("STALE_NATIVE_SOURCE: case selection changed")
    if check_run:
        run = client.workflow_status(source["run_id"])
        if (
            run.get("status") not in {"queued", "in_progress"}
            or run.get("run_attempt") != source["attempt"]
        ):
            raise ValueError("NATIVE_RUN_CANCELLED_OR_SUPERSEDED")


def prepare_ready(client, run):
    data = client.request(
        "GET",
        f"/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100",
    )
    jobs = data.get("jobs", [])
    if data.get("total_count") != len(jobs) or len(jobs) > 100:
        raise ValueError("Prepare job visibility incomplete")
    prepare = [j for j in jobs if j.get("name") == "prepare"]
    return (
        len(prepare) == 1
        and prepare[0].get("run_id") == run["id"]
        and prepare[0].get("run_attempt") == run.get("run_attempt", 1)
        and prepare[0].get("status") == "completed"
        and prepare[0].get("conclusion") == "success"
    )


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
    controls = set(
        filter(None, os.environ.get("LITE_ACTIONS_VLLM_CONTROL_SHAS", "").split(","))
    )
    validate_source(source, run, plan, controls)
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
