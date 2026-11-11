"""Prepare and consume the opt-in Lite Actions repository's own smoke plan."""

import json
import os
import time
from pathlib import Path

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, parse_plan
from ci_core.gitcode import _unique_object, source_plan
from ci_core.github import GitHubClient
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
    rows = inspect_profile()
    # This title comes from GitHub's run, not from the submitted artifact.
    if run.get("display_title") == CPU_TITLE:
        rows = [row for row in rows if row["test_id"] == "worker_os_blue"]
        if len(rows) != 1 or rows[0]["kind"] != "worker_probe":
            raise ValueError("expected exactly one pinned OS probe")
        # source_plan separately rejects a probe with nonzero nodes or devices.
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
    return plan


def results(comments, plan, author):
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


def wait(environ=os.environ):
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
    deadline = time.monotonic() + 10800
    shown = set()
    found = {}
    while time.monotonic() < deadline:
        found = results(client.commit_comments(plan["sha"]), plan, author)
        for case_id in found.keys() - shown:
            result = found[case_id]
            outcome = (
                "PASS"
                if case_passed(result)
                else f"status={result.get('status')!r}, exit_code={result.get('exit_code')!r}, "
                f"cleanup_verified={result.get('cleanup_verified')!r}"
            )
            print(f"{case_id}: {outcome}", flush=True)
        shown.update(found)
        failures = [
            case_id for case_id, result in found.items() if not case_passed(result)
        ]
        if failures:
            raise ValueError("self-smoke cases failed: " + ", ".join(sorted(failures)))
        if len(found) == len(plan["cases"]):
            return
        time.sleep(20)
    missing = {item["case_id"] for item in plan["cases"]} - found.keys()
    raise TimeoutError("self-smoke results incomplete: " + ", ".join(sorted(missing)))


if __name__ == "__main__":
    import sys

    try:
        if sys.argv[1:] == ["prepare"]:
            prepare()
        elif sys.argv[1:] == ["wait"]:
            wait()
        else:
            raise SystemExit("expected prepare or wait")
    except (ValueError, TimeoutError) as exc:
        raise SystemExit(str(exc)) from exc
