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
from ci_core.reporting import NAMES, job_key, run_and_report as run_v1_and_report
from ci_core.resources import single_agent
from ci_core.dispatcher import run_and_report as run_v2_and_report
from ci_core.registry import workflow_label

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "system" / "unified-agent.log"
OWNER = "depeng1994"
BRANCHES = set(filter(None, os.environ.get("A3_CI_ALLOWED_BRANCHES", "master").split(",")))
MAX_WORKERS = 1  # Phase 1: one training Case across ALL workflows
INTERVAL = int(os.environ.get("A3_CI_POLL_SECONDS", "60"))
STATUSES = {case: labels[0] for case, labels in NAMES.items()}
LEGACY_STATUSES = {case: labels[1] for case, labels in NAMES.items() if len(labels) > 1}


def eligible(run: dict, case: dict) -> bool:
    name = run.get("name")
    known_names = NAMES.get(case["name"], ())
    authorized_name = name in known_names
    if not authorized_name and case["runner"]["kind"] == "ssh":
        try:
            authorized_name = workflow_label(run) in known_names
        except ValueError:
            authorized_name = False
    return (case["enabled"]
            and run.get("event") == "workflow_dispatch"
            and run.get("status") == "in_progress"
            and run.get("head_branch") in BRANCHES
            and run.get("actor", {}).get("login") == OWNER
            and run.get("path") == ".github/workflows/" + case["workflow"]
            and authorized_name
            and isinstance(run.get("id"), int)
            and type(run.get("run_number")) is int and run["run_number"] > 0
            and type(run.get("run_attempt", 1)) is int and run.get("run_attempt", 1) > 0
            and bool(re.fullmatch("[0-9a-f]{40}", run.get("head_sha", ""))))


def select_run_handler(run: dict):
    """The local smoke uses legacy reporting; SHA-bound Matrix cases use V2."""
    if run.get("_ci_request", {}).get("schema") == 2:
        return run_v2_and_report
    return run_v1_and_report


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
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
                    # GitHub returns newest-first; preserve submission order for
                    # finite-timeout smoke jobs and globally serial NPU cases.
                    for run in sorted(client.running_workflows(), key=lambda item: item["id"]):
                        filename = (run.get("path") or "").rsplit("/", 1)[-1]
                        case = workflows.get(filename)
                        if not case or not eligible(run, case):
                            continue
                        if case["runner"]["kind"] == "ssh":
                            # The preparation Job uploads the structured manifest.
                            # Poll again if it is not uploaded yet; never guess
                            # test IDs from the display title.
                            request = client.ci_request(run)
                            if request is None:
                                continue
                            run["_ci_request"] = request
                        key = job_key(run)
                        if key in inflight:
                            continue
                        past = state.read().get(key, {})
                        # V2 task state prevents duplicate execution; allow
                        # safe pending-report retry after a network interruption.
                        if past.get("phase") == "reported":
                            continue
                        if (past.get("phase") == "running"
                                and run.get("_ci_request", {}).get("schema") != 2):
                            continue
                        if past.get("phase") == "blocked_recovery":
                            continue
                        if past.get("phase") != "pending_report":
                            state.update(key, phase="queued", case=case["name"],
                                         sha=run["head_sha"])
                        handler = select_run_handler(run)
                        cancel_event = Event()
                        run["_ci_cancel_event"] = cancel_event
                        inflight[key] = (executor.submit(handler, case, run, root=ROOT),
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
