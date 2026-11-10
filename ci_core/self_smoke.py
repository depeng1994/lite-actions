"""Prepare and consume the opt-in Lite Actions repository's own smoke plan."""

import json
import os
import time
from pathlib import Path

from ci_core.contract import SELF_REPO, SELF_WORKFLOW, parse_plan
from ci_core.gitcode import _unique_object, catalog_plan
from ci_core.github import GitHubClient

CATALOG = Path(".ci/lite_actions/smoke_cases.json")
PLAN = Path(".ci-request/ci-request.json")


def prepare(environ=os.environ):
    if environ["GITHUB_REPOSITORY"] != SELF_REPO:
        raise ValueError("unexpected self-smoke repository")
    run = {
        "id": int(environ["GITHUB_RUN_ID"]),
        "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"]),
        "head_sha": environ["GITHUB_SHA"],
        "path": ".github/workflows/" + SELF_WORKFLOW,
    }
    plan = catalog_plan(CATALOG.read_bytes(), run, SELF_REPO)
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
        if not isinstance(result, dict) or any(
            json.dumps(result.get(key), sort_keys=True)
            != json.dumps(value, sort_keys=True)
            for key, value in expected.items()
        ):
            raise ValueError("self-smoke result identity mismatch")
        if (
            result.get("status") != "PASS"
            or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
            or result.get("cleanup_verified") is not True
        ):
            raise ValueError("self-smoke case did not pass with verified cleanup")
        if case_id in found and found[case_id] != result:
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
    while time.monotonic() < deadline:
        found = results(client.commit_comments(plan["sha"]), plan, author)
        for case_id in found.keys() - shown:
            print(f"{case_id}: PASS", flush=True)
        shown.update(found)
        if len(found) == len(plan["cases"]):
            return
        time.sleep(20)
    raise TimeoutError("self-smoke results incomplete")


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["prepare"]:
        prepare()
    elif sys.argv[1:] == ["wait"]:
        wait()
    else:
        raise SystemExit("expected prepare or wait")
