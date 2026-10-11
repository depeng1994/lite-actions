#!/usr/bin/env python3
"""Trusted GitHub Actions poller; reporting is owned by dispatcher Python."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time

from ci_core.config import load_config
from ci_core.github import GitHubClient, authorization
from ci_core.job_state import JobState
from ci_core.reporting import NAMES, job_key
from ci_core.protocol import parse_plan
from ci_core.dispatcher import task_key
from ci_core.resources import single_agent
from ci_core.dispatcher import run_and_report
from ci_core.registry import workflow_label

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "system" / "unified-agent.log"
# Explicit deployment-time allowlist. No maintainer account in repository code.
ALLOWED_ACTORS = frozenset(
    actor.strip() for actor in os.environ.get("LITE_ACTIONS_ALLOWED_ACTORS", "").split(",")
    if actor.strip()
)
BRANCHES = set(filter(None, os.environ.get("LITE_ACTIONS_ALLOWED_BRANCHES", "master").split(",")))
# Independent training workers may run on disjoint leased NPUs/hosts.
# Local smoke has a dedicated worker; cross-process resource and port leases
# prevent conflicting NPU jobs, including distributed multi-host allocations.
MAX_WORKERS = 5
TRAINING_WORKERS = 4
LIGHTWEIGHT_WORKERS = MAX_WORKERS - TRAINING_WORKERS
INTERVAL = int(os.environ.get("LITE_ACTIONS_POLL_SECONDS", "60"))
STATUSES = {case: labels[0] for case, labels in NAMES.items()}


def eligible(run: dict, case: dict) -> bool:
    name = run.get("name")
    known_names = NAMES.get(case["name"], ())
    authorized_name = name in known_names
    if not authorized_name and case["runner"]["kind"] == "ssh":
        try:
            authorized_name = workflow_label(run) in known_names
        except ValueError:
            authorized_name = False
    manual = (run.get("event") == "workflow_dispatch"
              and run.get("head_branch") in BRANCHES
              and run.get("actor", {}).get("login") in ALLOWED_ACTORS)
    pr = (run.get("event") == "pull_request"
          and type(run.get("_ci_pr_number")) is int
          and run["_ci_pr_number"] > 0)
    return (case["enabled"]
            and (manual or pr)
            and run.get("status") == "in_progress"
            and run.get("path") == ".github/workflows/" + case["workflow"]
            and authorized_name
            and isinstance(run.get("id"), int)
            and type(run.get("run_number")) is int and run["run_number"] > 0
            and type(run.get("run_attempt", 1)) is int and run.get("run_attempt", 1) > 0
            and bool(re.fullmatch("[0-9a-f]{40}", run.get("head_sha", ""))))



def requested_devices(case: dict) -> int:
    """Hardware priority comes from trusted configured topology, never PR input."""
    resources = case.get("resources")
    if not resources:
        return sum(len(item["devices"]) for item in case.get("allocation", []))
    return resources["nodes"] * resources["devices_per_node"]


def prioritize_runs(runs: list[dict], workflows: dict[str, dict]) -> list[dict]:
    """Large eligible workflows first; stable FIFO among equal-size requests.

    A5 remains disabled and is excluded from workflows before this function.
    Resource admission, not GitHub workflow status, remains authoritative.
    """
    def rank(run):
        path = (run.get("path") or "").rsplit("/", 1)[-1]
        case = workflows.get(path)
        return (-requested_devices(case) if case else 0,
                run.get("id", 0))
    return sorted(runs, key=rank)


def prune_completed_unadmitted(state: JobState, client: GitHubClient,
                               inflight: dict, active_runs: list[dict]) -> None:
    """Release priority intents for GitHub Runs that completed before admission.

    Otherwise a canceled, never-launched 16P can reserve devices forever and
    starve new 8P Cases after Dispatcher restart. Never infer cancellation
    from a possibly paginated list: verify the exact Run ID before cleanup.
    """
    active_ids = {run["id"] for run in active_runs}
    for key, record in state.read().items():
        if (key in inflight or record.get("phase") not in ("queued", "waiting_resources")
                or key.count(":") < 2):
            continue
        prefix = key.split(":", 1)[0]
        if not prefix.isdecimal():
            continue
        run_id = int(prefix)
        if run_id in active_ids:
            continue
        try:
            current = client.workflow_status(run_id)
        except Exception:
            continue  # fail closed when GitHub status is unavailable
        if current.get("status") == "completed":
            state.update(key, phase="reported", result="ABANDONED",
                         reason="GitHub Run completed before resource admission")
            logging.info("reconciled abandoned unadmitted CI case %s", key)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Poll only once")
    parser.add_argument("--validate", action="store_true", help="Offline config check")
    args = parser.parse_args()
    cases = load_config(ROOT)
    if args.validate:
        print("Configured CI channels (validation passed):")
        for name, case in sorted(cases.items()):
            chips = (sum(len(n["devices"]) for n in case["allocation"])
                     if "allocation" in case else
                     case["resources"]["nodes"] * case["resources"]["devices_per_node"])
            status = "ENABLED" if case["enabled"] else "DISABLED"
            print(f"  {name}: {status} | {case['workflow']} | "
                  f"pool={case['pool']} | devices={chips} | "
                  f"runner={case['runner']['kind']}")
        return

    with single_agent():
        LOG.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[RotatingFileHandler(LOG, maxBytes=5*1024*1024, backupCount=3),
                                      logging.StreamHandler()])
        client = GitHubClient(authorization())
        state = JobState(ROOT)
        workflows = {case["workflow"]: case for case in cases.values() if case["enabled"]}
        inflight = {}
        with (ThreadPoolExecutor(max_workers=TRAINING_WORKERS) as training_executor,
              ThreadPoolExecutor(max_workers=LIGHTWEIGHT_WORKERS) as lightweight_executor):
            while True:
                for key, (job, cancel_event, run_id) in list(inflight.items()):
                    if job.done():
                        try:
                            job.result()
                        except Exception:
                            logging.exception("dispatcher crashed for run %s", key)
                        del inflight[key]
                    else:
                        # Confirm with the exact run ID (do not infer cancellation
                        # from a paginated/stale in-progress list).
                        try:
                            current = client.workflow_status(run_id)
                            if current.get("status") == "completed":
                                cancel_event.set()
                        except Exception:
                            logging.warning("GitHub Run status unavailable for %s", run_id)
                try:
                    # Prioritize eligible large topologies: 64P > 16P > 8P.
                    # Within one size preserve Run-ID FIFO. Already running
                    # cases are never preempted; A5 disabled is not admitted.
                    active_runs = client.running_workflows()
                    prune_completed_unadmitted(state, client, inflight, active_runs)
                    for run in prioritize_runs(active_runs, workflows):
                        filename = (run.get("path") or "").rsplit("/", 1)[-1]
                        case = workflows.get(filename)
                        if not case or not eligible(run, case):
                            continue
                        if case["runner"]["kind"] == "ssh":
                            # Expand each verified GitHub Matrix Case into its
                            # own independently scheduled execution task.
                            request = client.ci_request(run)
                            if request is None:
                                continue
                            run["_ci_request"] = request
                            items = parse_plan(request, run, case["pipeline_id"])
                        else:
                            items = [None]
                        run_key = job_key(run)
                        for item in items:
                            test_id = item["test_id"] if item is not None else None
                            key = task_key(run, test_id) if test_id else run_key
                            if key in inflight:
                                continue
                            past = state.read().get(key, {})
                            if past.get("phase") == "reported":
                                continue
                            if (past.get("phase") == "running"
                                    and run.get("_ci_request", {}).get("schema") != 2):
                                continue
                            if past.get("phase") == "blocked_recovery":
                                continue
                            if past.get("phase") != "pending_report":
                                state.update(key, phase="queued", case=case["name"],
                                             queued_at=past.get("queued_at", time.time()),
                                             requested_devices=requested_devices(case),
                                             sha=run["head_sha"])
                            if test_id and state.read().get(run_key, {}).get("phase") != "running":
                                state.update(run_key, phase="queued", case=case["name"],
                                             sha=run["head_sha"])
                            cancel_event = Event()
                            subrun = {**run, "_ci_cancel_event": cancel_event}
                            if test_id:
                                subrun["_ci_case_id"] = test_id
                            # Never queue blocked NPU work into the executor:
                            # keeping waiting 8P Futures ahead of new 16P
                            # starves large cases even after devices release.
                            if test_id:
                                train_inflight = sum(
                                    1 for task_key_value in inflight
                                    if task_key_value.count(":") >= 2)
                                if train_inflight >= TRAINING_WORKERS:
                                    continue
                            executor = (training_executor if test_id
                                        else lightweight_executor)
                            inflight[key] = (executor.submit(run_and_report, case, subrun,
                                                             root=ROOT),
                                             cancel_event, run["id"])
                except Exception:
                    logging.exception("poll failed")
                if args.once:
                    break
                time.sleep(INTERVAL)
            for job, _, _ in inflight.values():
                job.result()


if __name__ == "__main__":
    main()
