"""Prepare and consume the opt-in Lite Actions repository's own smoke plan."""

import json
import os
import time
from pathlib import Path

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, parse_plan
from ci_core.gitcode import _unique_object, source_plan
from ci_core.github import GitHubClient
from ci_core.worker_probe import safe_probe_line
from tests.integration_tests.tools.lite_actions.entrypoint import inspect_profile

PLAN = Path(".ci-request/ci-request.json")
CPU_TITLE = "Lite Actions self smoke: cpu-only"


def case_passed(result):
    return (
        result.get("status") == "PASS"
        and type(result.get("exit_code")) is int
        and result["exit_code"] == 0
        and result.get("cleanup_verified") is True
    )


def expected_plan(run):
    # The Worker selects the suite from GitHub's run, not the submitted artifact.
    cpu_only = run.get("display_title") == CPU_TITLE
    rows = inspect_profile("cpu-only" if cpu_only else "dispatch_smoke")
    if cpu_only and any(row["kind"] != "worker_probe" for row in rows):
        raise ValueError("cpu-only cannot schedule device jobs")
    return source_plan(rows, run, SELF_REPO)


def prepare(environ=os.environ):
    if environ["GITHUB_REPOSITORY"] != SELF_REPO:
        raise ValueError("unexpected self-smoke repository")
    profile = environ.get("SELF_SMOKE_PROFILE", "full")
    if profile not in {"full", "cpu-only"}:
        raise ValueError("unknown self-smoke profile")
    run = {
        "id": int(environ["GITHUB_RUN_ID"]),
        "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"]),
        "head_sha": environ["GITHUB_SHA"],
        "path": ".github/workflows/" + SELF_WORKFLOW,
        "display_title": CPU_TITLE if profile == "cpu-only" else "",
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


def results(comments, plan, author, *, only=None):
    if not author:
        raise ValueError("result author is required")
    cases = {item["case_id"]: item for item in plan["cases"]}
    prefix = f"Lite-CI-V3 {plan['run_id']}:{plan['attempt']}:"
    found = {}
    for comment in comments:
        body = comment.get("body", "")
        if comment.get("user", {}).get("login") != author or not body.startswith(
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
    plan = json.loads(PLAN.read_text(), object_pairs_hook=_unique_object)
    if (
        plan["repo"] != environ["GITHUB_REPOSITORY"]
        or plan["sha"] != environ["GITHUB_SHA"]
        or plan["run_id"] != int(environ["GITHUB_RUN_ID"])
        or plan["attempt"] != int(environ["GITHUB_RUN_ATTEMPT"])
    ):
        raise ValueError("self-smoke plan identity mismatch")
    parse_plan(
        plan,
        {"id": plan["run_id"], "run_attempt": plan["attempt"], "head_sha": plan["sha"]},
        SELF_WORKFLOW,
        repo=SELF_REPO,
    )
    author = environ["CI_RESULT_AUTHOR"]
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
    deadline = time.monotonic() + (
        540 if environ.get("SELF_SMOKE_PROFILE") == "cpu-only" else 10680
    )
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
        elif sys.argv[1:] == ["wait"]:
            wait()
        elif sys.argv[1:] == ["wait-case"]:
            wait(only=os.environ["SELF_SMOKE_CASE_ID"])
        elif sys.argv[1:] == ["verify"]:
            wait(once=True)
        else:
            raise SystemExit("expected prepare, wait, wait-case or verify")
    except (ValueError, TimeoutError) as exc:
        raise SystemExit(str(exc)) from exc
