"""Fixed, zero-device Linux Worker probe; never run on the Actions waiter."""

import os
import platform
import subprocess
import sys
import time

# Also used by the self-smoke waiter to display only approved receipt fields.
FIELDS = frozenset(
    {
        "WORKER_HOSTNAME",
        "WORKER_ARCH",
        "WORKER_KERNEL",
        "PRETTY_NAME",
        "WORKER_PYTHON",
        "WORKER_CPU_ONLINE",
        "WORKER_CPU_BUSY_PCT_1S",
        "WORKER_LOADAVG_1M_5M_15M",
    }
)


def safe_probe_line(line):
    if not isinstance(line, str) or len(line) > 180 or not line.isprintable():
        return False
    key, _, value = line.partition("=")
    return key in FIELDS and bool(value) and "::" not in line and "##[" not in line


def cpu_ticks():
    with open("/proc/stat", encoding="ascii") as f:
        label, *fields = f.readline().split()
    if label != "cpu" or len(fields) < 8:
        raise ValueError("missing aggregate Linux CPU counters")
    user, nice, system, idle, iowait, irq, softirq, steal = map(int, fields[:8])
    # guest/guest_nice are already accounted for in user/nice.
    busy = user + nice + system + irq + softirq
    return busy, busy + idle + iowait + steal


def snapshot():
    b0, t0 = cpu_ticks()
    time.sleep(1)
    b1, t1 = cpu_ticks()
    if t1 <= t0 or not 0 <= b1 - b0 <= t1 - t0:
        raise ValueError("invalid CPU counter sample")
    return {
        "WORKER_HOSTNAME": subprocess.check_output(
            ["hostname", "-f"], text=True
        ).strip(),
        "WORKER_ARCH": platform.machine(),
        "WORKER_KERNEL": f"{platform.system()} {platform.release()}",
        "PRETTY_NAME": platform.freedesktop_os_release()["PRETTY_NAME"],
        "WORKER_PYTHON": f"Python {platform.python_version()}",
        "WORKER_CPU_ONLINE": str(os.sysconf("SC_NPROCESSORS_ONLN")),
        "WORKER_CPU_BUSY_PCT_1S": f"{100 * (b1 - b0) / (t1 - t0):.1f}",
        "WORKER_LOADAVG_1M_5M_15M": " ".join(f"{n:.2f}" for n in os.getloadavg()),
    }


def probe():
    return subprocess.run(
        [sys.executable, "-I", __file__, "snapshot"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
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


def main(args):
    if args == ["wait"]:
        return wait(os.environ)
    if args != ["snapshot"]:
        raise SystemExit("expected snapshot or wait")
    try:
        for key, value in snapshot().items():
            line = f"{key}={value}"
            if not safe_probe_line(line):
                raise ValueError("invalid probe field")
            print(line)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        sys.exit(f"Worker OS/CPU snapshot failed: {type(exc).__name__}")


if __name__ == "__main__":
    main(sys.argv[1:])
