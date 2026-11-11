"""Verify the exact GitHub source used by a model-owned CI plan."""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import zipfile

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


def validate_source(source, run, plan, approved_controls, *, repository_id):
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        raise ValueError("invalid native source structure")
    expected = {
        "schema": 1,
        "provider": "github",
        "repository": run["_ci_repo"],
        "repository_id": repository_id,
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
            or source["head_repo_id"] != repository_id
            or source["base_ref"] != run.get("head_branch")
            or any(
                source[k] != run["head_sha"]
                for k in ("head_sha", "base_sha", "source_sha")
            )
        ):
            raise ValueError("native manual source binding mismatch")
    else:
        raise ValueError("unsupported native source mode")
    return source


def check_current(client, source, *, check_run=False):
    """Fail closed at admission, post-staging launch and result publication."""
    if client.repo != source["repository"]:
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
            or base_repo.get("id") != source["repository_id"]
            or base_repo.get("full_name") != client.repo
            or pr_base.get("ref") != source["base_ref"]
            or base != source["base_sha"]
            or pr.get("mergeable") is False
        ):
            raise ValueError("STALE_NATIVE_SOURCE: Head/Base/authorization changed")
        if pr.get("mergeable") is None or not pr.get("merge_commit_sha"):
            raise RuntimeError("Native merge source is temporarily unavailable")
        if pr["merge_commit_sha"] != source["source_sha"]:
            raise ValueError("STALE_NATIVE_SOURCE: merge source changed")
    if check_run:
        run = client.workflow_status(source["run_id"])
        if (
            run.get("status") not in {"queued", "in_progress"}
            or run.get("run_attempt") != source["attempt"]
        ):
            raise ValueError("NATIVE_RUN_CANCELLED_OR_SUPERSEDED")


def approved_controls(client, run, candidate, *, source=None):
    """Prove the controller belongs to a repository branch, independent of trigger."""
    if not isinstance(candidate, str) or not SHA.fullmatch(candidate):
        raise ValueError("invalid controller SHA")
    if (
        isinstance(source, dict)
        and source.get("mode") == "merge_result"
        and candidate in (source.get("head_sha"), source.get("source_sha"))
    ):
        # PR workflows may themselves be loaded from the verified head or merge.
        return {candidate}
    for branch in dict.fromkeys(
        (
            run.get("head_branch"),
            source.get("base_ref") if isinstance(source, dict) else None,
        )
    ):
        if not isinstance(branch, str) or not branch or len(branch) > 255:
            continue
        try:
            head = (
                client.request(
                    "GET", "/branches/" + urllib.parse.quote(branch, safe="")
                )
                .get("commit", {})
                .get("sha")
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise ValueError("invalid controller branch SHA")
        if head == candidate:
            return {candidate}
        comparison = client.request("GET", f"/compare/{candidate}...{head}?per_page=1")
        if comparison.get("merge_base_commit", {}).get("sha") == candidate:
            return {candidate}
    raise ValueError("controller is not in a trusted repository branch")


def read_source_artifact(client, run):
    name = f"lite-ci-source-{run.get('run_attempt', 1)}"
    listing = client.request(
        "GET", f"/actions/runs/{run['id']}/artifacts?name={name}&per_page=10"
    )
    matches = [
        a
        for a in listing.get("artifacts", [])
        if a.get("name") == name and not a.get("expired")
    ]
    if not matches:
        return None
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
        return json.loads(zipped.read("source.json"))


def bind_request_source(client, run, source):
    """Bind structured source identity to a run before validating its plan."""
    if not isinstance(source, dict) or run.get("head_sha") not in (
        source.get("control_sha"),
        source.get("head_sha"),
        source.get("source_sha"),
    ):
        raise ValueError("source identity differs from workflow run")
    head = source.get("head_sha")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("invalid requested source SHA")
    bound = {**run, "_ci_control_sha": source.get("control_sha"), "head_sha": head}
    if source.get("mode") == "merge_result":
        number = source.get("pr_number")
        if type(number) is not int or number < 1:
            raise ValueError("invalid requested PR")
        pr = client.request("GET", f"/pulls/{number}")
        repo = pr.get("head", {}).get("repo") or {}
        name = repo.get("full_name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name
        ):
            raise ValueError("invalid PR source repository")
        bound.update(
            _ci_pr_number=number,
            _ci_source_repo=name,
            _ci_source_repo_id=repo.get("id"),
            _ci_base_ref=(pr.get("base") or {}).get("ref"),
        )
    elif source.get("mode") != "commit":
        raise ValueError("unsupported requested source")
    return bound


def read_source(client, run, plan, *, source=None):
    if source is None:
        source = read_source_artifact(client, run)
    if source is None:
        return None
    candidate = source.get("control_sha") if isinstance(source, dict) else None
    repository_id = client.request("GET", "").get("id")
    if type(repository_id) is not int or repository_id < 1:
        raise ValueError("invalid GitHub repository identity")
    validate_source(
        source,
        run,
        plan,
        approved_controls(
            client,
            run,
            candidate,
            source=source,
        ),
        repository_id=repository_id,
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
