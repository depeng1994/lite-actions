"""Prepare and consume the opt-in Lite Actions repository's own smoke plan."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from ci_core.contract import (
    PR_SMOKE_WORKFLOW,
    SELF_REPO,
    SELF_WORKFLOW,
    create_plan,
    normalize,
    parse_plan,
)
from ci_core.gitcode import _unique_object, source_plan
from ci_core.github import GitHubClient
from ci_core.worker_probe import safe_probe_line
from tests.integration_tests.tools.lite_actions.entrypoint import inspect_profile

PLAN = Path(".ci-request/ci-request.json")
CLEANUP_PLAN = Path(".ci-cleanup/ci-request.json")
CPU_TITLE = "Lite Actions self smoke: cpu-only"
YELLOW_A3_TITLE = "Lite Actions self smoke: yellow-a3"


def case_passed(result):
    return (
        result.get("status") == "PASS"
        and type(result.get("exit_code")) is int
        and result["exit_code"] == 0
        and result.get("cleanup_verified") is True
    )


def expected_plan(run):
    # The Worker selects the suite from GitHub's run, not the submitted artifact.
    title = run.get("display_title")
    if title == CPU_TITLE:
        profile = "cpu-only"
    elif title == YELLOW_A3_TITLE:
        profile = "yellow-a3"
    else:
        profile = "dispatch_smoke"
    rows = inspect_profile(profile)
    if profile == "cpu-only" and any(row["kind"] != "worker_probe" for row in rows):
        raise ValueError("cpu-only cannot schedule device jobs")
    return source_plan(rows, run, SELF_REPO)


def prepare(environ=os.environ):
    if environ["GITHUB_REPOSITORY"] != SELF_REPO:
        raise ValueError("unexpected self-smoke repository")
    profile = environ.get("SELF_SMOKE_PROFILE", "full")
    if profile not in {"full", "cpu-only", "yellow-a3"}:
        raise ValueError("unknown self-smoke profile")
    run = {
        "id": int(environ["GITHUB_RUN_ID"]),
        "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"]),
        "head_sha": environ["GITHUB_SHA"],
        "path": ".github/workflows/" + SELF_WORKFLOW,
        "display_title": (
            CPU_TITLE
            if profile == "cpu-only"
            else YELLOW_A3_TITLE
            if profile == "yellow-a3"
            else ""
        ),
    }
    plan = expected_plan(run)
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, separators=(",", ":")) + "\n")
    if environ.get("GITHUB_OUTPUT"):
        with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(
                "matrix="
                + json.dumps({"include": plan["cases"]}, separators=(",", ":"))
                + "\n"
            )
    return plan


def _pr_identity(environ):
    if environ["GITHUB_REPOSITORY"] != SELF_REPO:
        raise ValueError("unexpected self-smoke repository")
    run_id = int(environ["GITHUB_RUN_ID"])
    attempt = int(environ["GITHUB_RUN_ATTEMPT"])
    run_sha = environ.get("SELF_SMOKE_SHA") or environ["GITHUB_SHA"]
    target_test_id = f"id{run_id}a{attempt}"
    return run_id, attempt, run_sha, target_test_id


def prepare_pr(environ=os.environ):
    run_id, attempt, run_sha, target_test_id = _pr_identity(environ)
    pr_number = int(environ["PR_NUMBER"])
    pr_head_sha = environ["PR_HEAD_SHA"]
    profile = environ.get("SELF_SMOKE_PROFILE", "full")
    if profile not in {"full", "cpu-only"}:
        raise ValueError("unknown PR self-smoke profile")
    raw_gen = (environ.get("PR_GATE_GENERATION") or "").strip()
    generation = int(raw_gen) if raw_gen else None
    ttl_seconds = int(environ.get("TEST_WORKER_TTL_SECONDS") or 14400)
    defaults = {"worker": "", "target_pool": "", "docker": ""}
    cases = []
    deploy_cases = []
    for zone in ("blue", "yellow"):
        lifecycle = {
            "pr_number": pr_number,
            "pr_head_sha": pr_head_sha,
            "target_test_id": target_test_id,
            "target_worker": f"{zone}-test-{target_test_id}",
            "github_run_id": run_id,
            "github_attempt": attempt,
            "ttl_seconds": ttl_seconds,
        }
        if generation is not None:
            lifecycle["generation"] = generation
        case = normalize(
            SimpleNamespace(
                test_name=f"deploy_{zone}",
                test_descr=f"Deploy {zone} test worker {target_test_id}",
                ci_kind="test_worker_deploy",
                ci_overrides={"worker": zone},
                nnodes=0,
                ngpu=0,
                timeout=600,
                lifecycle=lifecycle,
            ),
            defaults,
            len(cases),
        )
        cases.append(case)
        deploy_cases.append({"case_id": case["case_id"], "worker": zone})

    smoke_rows = inspect_profile(
        "cpu-only" if profile == "cpu-only" else "dispatch_smoke"
    )
    smoke_cases = []
    for row in smoke_rows:
        base_worker = row["ci_overrides"]["worker"]
        target_worker = f"{base_worker}-test-{target_test_id}"
        overrides = (
            {"worker": target_worker}
            if row["kind"] == "worker_probe"
            else {**row["ci_overrides"], "worker": target_worker}
        )
        case = normalize(
            SimpleNamespace(
                test_name=row["test_id"],
                test_descr=row["label"],
                ci_kind=row["kind"],
                ci_overrides=overrides,
                nnodes=row["nnodes"],
                ngpu=row["ngpu"],
                timeout=row["timeout"],
            ),
            defaults,
            len(cases),
        )
        cases.append(case)
        smoke_cases.append(
            {
                "case_id": case["case_id"],
                "worker": target_worker,
                "target_pool": case["target_pool"] or "cpu_probe",
            }
        )

    plan = create_plan(
        cases,
        run_id=run_id,
        attempt=attempt,
        sha=run_sha,
        workflow=PR_SMOKE_WORKFLOW,
        defaults=defaults,
        repo=SELF_REPO,
    )
    plan_path = (
        Path(environ["SELF_SMOKE_PLAN"]) if environ.get("SELF_SMOKE_PLAN") else PLAN
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, separators=(",", ":")) + "\n")
    if environ.get("GITHUB_OUTPUT"):
        with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(
                "deploy_matrix="
                + json.dumps({"include": deploy_cases}, separators=(",", ":"))
                + "\n"
            )
            output.write(
                "smoke_matrix="
                + json.dumps({"include": smoke_cases}, separators=(",", ":"))
                + "\n"
            )
            output.write(f"target_test_id={target_test_id}\n")
    return plan


def prepare_pr_cleanup(environ=os.environ):
    run_id, attempt, run_sha, target_test_id = _pr_identity(environ)
    defaults = {"worker": "", "target_pool": "", "docker": ""}
    cases = []
    for zone in ("blue", "yellow"):
        lifecycle = {
            "target_test_id": target_test_id,
            "target_worker": f"{zone}-test-{target_test_id}",
            "github_run_id": run_id,
            "github_attempt": attempt,
        }
        if environ.get("PR_NUMBER"):
            lifecycle["pr_number"] = int(environ["PR_NUMBER"])
        if environ.get("PR_HEAD_SHA"):
            lifecycle["pr_head_sha"] = environ["PR_HEAD_SHA"]
        cases.append(
            normalize(
                SimpleNamespace(
                    test_name=f"cleanup_{zone}",
                    test_descr=f"Cleanup {zone} test worker {target_test_id}",
                    ci_kind="test_worker_cleanup",
                    ci_overrides={"worker": zone},
                    nnodes=0,
                    ngpu=0,
                    timeout=600,
                    lifecycle=lifecycle,
                ),
                defaults,
                len(cases),
            )
        )
    plan = create_plan(
        cases,
        run_id=run_id,
        attempt=attempt,
        sha=run_sha,
        workflow=PR_SMOKE_WORKFLOW,
        defaults=defaults,
        repo=SELF_REPO,
    )
    plan_path = (
        Path(environ["SELF_SMOKE_PLAN"])
        if environ.get("SELF_SMOKE_PLAN")
        else CLEANUP_PLAN
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, separators=(",", ":")) + "\n")
    return plan


def results(comments, plan, author, *, only=None):
    authors = {name.strip() for name in str(author or "").split(",") if name.strip()}
    if not authors:
        raise ValueError("result author is required")
    cases = {item["case_id"]: item for item in plan["cases"]}
    prefix = f"Lite-CI-V3 {plan['run_id']}:{plan['attempt']}:"
    found = {}
    for comment in comments:
        body = comment.get("body", "")
        if comment.get("user", {}).get("login") not in authors or not body.startswith(
            prefix
        ):
            continue
        marker, separator, payload = body.partition("\n")
        case_id = marker[len(prefix) :]
        if not separator or case_id not in cases:
            raise ValueError("unknown self-smoke result")
        if only is not None and only != case_id:
            continue  # Other matrix jobs check their own receipts; the gate checks all.
        result = json.loads(payload, object_pairs_hook=_unique_object)
        item = cases[case_id]
        expected = {
            "schema": 3,
            "repo": plan["repo"],
            "workflow": plan["workflow"],
            "run_id": plan["run_id"],
            "attempt": plan["attempt"],
            "sha": plan["sha"],
            "plan_digest": plan["plan_digest"],
            **{
                key: item[key]
                for key in (
                    "case_id",
                    "test_id",
                    "worker",
                    "target_pool",
                    "docker",
                    "resources",
                )
            },
        }
        if not isinstance(result, dict) or json.dumps(
            {key: result.get(key) for key in expected}, sort_keys=True
        ) != json.dumps(expected, sort_keys=True):
            raise ValueError("self-smoke result identity mismatch")
        if case_id in found and json.dumps(
            found[case_id], sort_keys=True
        ) != json.dumps(result, sort_keys=True):
            raise ValueError("conflicting self-smoke results")
        found[case_id] = result
    return found


def print_probe_receipt(result):
    # Never evaluate the payload as shell/Actions commands or sample the hosted runner.
    lines = result.get("last_20_lines")
    if not isinstance(lines, list) or len(lines) > 20:
        raise ValueError("invalid Worker probe receipt lines")
    for line in lines:
        if safe_probe_line(line):
            print(line, flush=True)


def wait(environ=os.environ, *, only=None, once=False):
    plan_path = (
        Path(environ["SELF_SMOKE_PLAN"]) if environ.get("SELF_SMOKE_PLAN") else PLAN
    )
    plan = json.loads(plan_path.read_text(), object_pairs_hook=_unique_object)
    expected_sha = environ.get("SELF_SMOKE_SHA") or environ["GITHUB_SHA"]
    workflow = plan.get("workflow")
    if (
        plan["repo"] != environ["GITHUB_REPOSITORY"]
        or plan["sha"] != expected_sha
        or plan["run_id"] != int(environ["GITHUB_RUN_ID"])
        or plan["attempt"] != int(environ["GITHUB_RUN_ATTEMPT"])
        or workflow not in {SELF_WORKFLOW, PR_SMOKE_WORKFLOW}
    ):
        raise ValueError("self-smoke plan identity mismatch")
    parse_plan(
        plan,
        {"id": plan["run_id"], "run_attempt": plan["attempt"], "head_sha": plan["sha"]},
        workflow,
        repo=SELF_REPO,
    )
    author = environ.get("CI_RESULT_AUTHOR") or environ.get("CI_RESULT_AUTHORS")
    client = GitHubClient(environ["GH_TOKEN"], SELF_REPO)
    selected = {
        c["case_id"]: c for c in plan["cases"] if only is None or c["case_id"] == only
    }
    if not selected:
        raise ValueError("unknown self-smoke case selector")
    if only is not None:
        print(
            f"Waiting for {selected[only]['worker']} Worker receipt: {only} on {plan['sha']} "
            f"(run {plan['run_id']}, attempt {plan['attempt']}). "
            "Check worker-ready.release_sha and the self-smoke subscription if no receipt arrives.",
            flush=True,
        )
    raw_timeout = (environ.get("SELF_SMOKE_WAIT_SECONDS") or "").strip()
    timeout_s = (
        int(raw_timeout)
        if raw_timeout
        else (540 if environ.get("SELF_SMOKE_PROFILE") == "cpu-only" else 10680)
    )
    deadline = time.monotonic() + timeout_s
    shown = set()
    while True:
        found = results(client.commit_comments(plan["sha"]), plan, author, only=only)
        for case_id in sorted(found.keys() - shown):
            result = found[case_id]
            outcome = (
                "PASS"
                if case_passed(result)
                else f"status={result.get('status')!r}, exit_code={result.get('exit_code')!r}, "
                f"cleanup_verified={result.get('cleanup_verified')!r}"
            )
            print(f"{case_id}: {outcome}", flush=True)
            if only is not None and selected[case_id]["kind"] == "worker_probe":
                print_probe_receipt(result)
        shown.update(found)
        failures = [
            case_id for case_id, result in found.items() if not case_passed(result)
        ]
        if failures:
            raise ValueError("self-smoke cases failed: " + ", ".join(sorted(failures)))
        missing = selected.keys() - found.keys()
        if not missing:
            return
        if once or time.monotonic() >= deadline:
            raise TimeoutError(
                "self-smoke results incomplete: " + ", ".join(sorted(missing))
            )
        time.sleep(20)


if __name__ == "__main__":
    import sys

    try:
        if sys.argv[1:] == ["prepare"]:
            prepare()
        elif sys.argv[1:] == ["prepare-pr"]:
            prepare_pr()
        elif sys.argv[1:] == ["prepare-pr-cleanup"]:
            prepare_pr_cleanup()
        elif sys.argv[1:] == ["wait"]:
            wait()
        elif sys.argv[1:] == ["wait-case"]:
            wait(only=os.environ["SELF_SMOKE_CASE_ID"])
        elif sys.argv[1:] == ["verify"]:
            wait(once=True)
        else:
            raise SystemExit(
                "expected prepare, prepare-pr, prepare-pr-cleanup, wait, wait-case or verify"
            )
    except (ValueError, TimeoutError) as exc:
        raise SystemExit(str(exc)) from exc
