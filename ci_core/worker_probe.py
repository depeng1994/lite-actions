"""Local operating-system probe, independent of NPU capacity."""

import subprocess

PROBE = """set -eu
printf 'WORKER_HOSTNAME='; hostname -f
printf 'WORKER_ARCH='; uname -m
printf 'WORKER_KERNEL='; uname -sr
printf 'OS_RELEASE_BEGIN\\n'
cat /etc/os-release
printf 'OS_RELEASE_END\\n'
printf 'WORKER_PYTHON='; python3 --version
"""


def probe():
    return subprocess.run(
        ["bash", "-lc", PROBE], capture_output=True, text=True, timeout=90, check=False
    )


ACTION = "worker-os/v1"
PROBE_NAME = "infra_worker_os_v1"
JOB_NAME = "Lite Actions worker-os-v1"
TEMPLATE = "depeng1994/lite-actions/.github/workflows/worker-cpu-probe.yml@main"


def fixed_item(worker):
    from types import SimpleNamespace

    from ci_core.contract import normalize

    if worker not in {"yellow", "blue"}:
        raise ValueError("unknown probe deployment")
    return normalize(
        SimpleNamespace(
            test_name=PROBE_NAME,
            test_descr=ACTION,
            ci_kind="worker_probe",
            ci_overrides={"worker": worker},
            timeout=90,
        ),
        {},
        0,
    )


def github_call(client, run, rule):
    """A name is never authority. Check platform-owned run-attempt, called revision and active job.

    Only manual calls from the exact operator-reviewed controller commit are enabled here.
    A PR/re-run with changed inputs must not inherit approval. Never trusts a caller artifact.
    """
    import re

    repo = client.repo
    ident = run["id"]
    attempt = run.get("run_attempt", 1)
    head = run["head_sha"]
    if (
        run.get("_ci_provider", "github") != "github"
        or repo != run.get("_ci_repo")
        or run.get("event") != "workflow_dispatch"
        or run.get("path") != rule["workflow"]
        or rule["repo"] != repo
        or type(ident) is not int
        or type(attempt) is not int
        or head not in rule["approved_caller_shas"]
    ):
        raise ValueError("INFRA_CALLER_NOT_APPROVED")
    detail = client.request("GET", f"/actions/runs/{ident}/attempts/{attempt}")
    if (
        detail.get("repository", {}).get("full_name") != repo
        or detail.get("id") != ident
        or detail.get("run_attempt") != attempt
        or detail.get("head_sha") != head
        or detail.get("head_branch") != rule["branch"]
        or detail.get("event") != "workflow_dispatch"
        or detail.get("path") != rule["workflow"]
        or detail.get("status") != "in_progress"
    ):
        raise ValueError("INFRA_STALE_RUN")
    refs = [
        v
        for v in detail.get("referenced_workflows", [])
        if v.get("path")
        in (
            TEMPLATE,
            TEMPLATE.removesuffix("@main") + "@refs/heads/main",
            TEMPLATE.removesuffix("@main") + "@" + str(v.get("sha")),
        )
    ]
    if (
        len(refs) != 1
        or not re.fullmatch(r"[a-f0-9]{40}", str(refs[0].get("sha")))
        or refs[0]["sha"] not in rule["approved_template_shas"]
    ):
        raise ValueError("INFRA_TEMPLATE_NOT_PROVEN")
    listing = client.request(
        "GET", f"/actions/runs/{ident}/attempts/{attempt}/jobs?per_page=100"
    )
    if type(listing.get("total_count")) is not int or listing["total_count"] != len(
        listing.get("jobs", [])
    ):
        raise ValueError("INFRA_JOB_LIST_INCOMPLETE")
    jobs = [
        j for j in listing["jobs"] if j.get("name", "").split(" / ")[-1] == JOB_NAME
    ]
    if (
        len(jobs) != 1
        or jobs[0].get("status") != "in_progress"
        or jobs[0].get("run_id") != ident
        or jobs[0].get("head_sha") != head
        or type(jobs[0].get("id")) is not int
        or jobs[0]["id"] <= 0
    ):
        raise ValueError("INFRA_JOB_NOT_ACTIVE")
    return dict(
        action=ACTION,
        repo=repo,
        workflow=rule["workflow"],
        provider="github",
        run_id=ident,
        attempt=attempt,
        sha=head,
        template_sha=refs[0]["sha"],
        job_id=jobs[0]["id"],
    )


def wait(environ):
    """Hosted runner: read comments on its own run, never run a host probe here."""
    import json
    import re
    import time

    from ci_core.contract import digest
    from ci_core.github import GitHubClient
    from ci_core.registry import _unique

    repo = environ["GITHUB_REPOSITORY"]
    client = GitHubClient(environ["GH_TOKEN"], repo)
    rid = int(environ["GITHUB_RUN_ID"])
    attempt = int(environ["GITHUB_RUN_ATTEMPT"])
    head = environ["GITHUB_SHA"]
    workflow = environ["CALLER_REF"].removeprefix(repo + "/").split("@", 1)[0]
    author = environ["RESULT_AUTHOR"]
    template = environ["CALLEE_SHA"]
    if not author or not re.fullmatch("[a-f0-9]{40}", template):
        raise ValueError("missing trusted probe configuration")
    rule = {
        "repo": repo,
        "workflow": workflow,
        "branch": environ["GITHUB_REF_NAME"],
        "approved_caller_shas": [head],
        "approved_template_shas": [template],
    }
    run = {
        "id": rid,
        "run_attempt": attempt,
        "head_sha": head,
        "path": workflow,
        "_ci_repo": repo,
        "event": environ["GITHUB_EVENT_NAME"],
    }
    proof = github_call(client, run, rule)
    dispatch = f"os-{proof['job_id']}"
    case = "case_000_" + PROBE_NAME
    prefix = f"Lite-CI-V3 {rid}:{attempt}:{dispatch}:{case}\n"
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        matches = [
            json.loads(c["body"][len(prefix) :], object_pairs_hook=_unique)
            for c in client.commit_comments(head)
            if c.get("user", {}).get("login") == author
            and c.get("body", "").startswith(prefix)
        ]
        if matches:
            r = matches[0]
            expected = {
                "schema": 3,
                "repo": repo,
                "sha": head,
                "run_id": rid,
                "attempt": attempt,
                "case_id": case,
                "test_id": PROBE_NAME,
                "dispatch_id": dispatch,
                "workflow": workflow.split("/")[-1],
                "target_pool": None,
                "docker": None,
                "resources": {"nodes": 0, "devices_per_node": 0},
                "plan_digest": digest(proof),
            }
            if (
                any(x != r for x in matches)
                or any(r.get(k) != v for k, v in expected.items())
                or r.get("worker") not in ("blue", "yellow")
                or r.get("status") != "PASS"
                or type(r.get("exit_code")) is not int
                or r["exit_code"] != 0
                or r.get("cleanup_verified") is not True
                or not isinstance(r.get("log_locator"), dict)
            ):
                raise ValueError("unverified or failed host probe")
            # Escaped JSON (not executable shell or multiline Actions commands).
            print(
                json.dumps(
                    {
                        "worker": r["worker"],
                        "log_locator": r["log_locator"],
                        "diagnostics": r.get("last_20_lines", []),
                    },
                    ensure_ascii=True,
                )
            )
            return
        time.sleep(10)
    raise TimeoutError("no authenticated host probe result on this attempt")


if __name__ == "__main__":
    import os

    wait(os.environ)
