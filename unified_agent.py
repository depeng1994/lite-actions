#!/usr/bin/env python3
"""Single internal Pull Agent for trusted, allowlisted Actions.

This agent is NOT activated until legacy A3-2p/8p/16p polling agents stop.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from ci_core.config import load_config
from ci_core.dispatch import execute, task_dir
from ci_core.github import GitHubClient, authorization

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "unified-jobs.json"
LOG = ROOT / "logs" / "system" / "unified-agent.log"
OWNER = "depeng1994"
BRANCHES = set(filter(None, os.environ.get("A3_CI_ALLOWED_BRANCHES", "master").split(",")))
MAX_WORKERS = 2
INTERVAL = int(os.environ.get("A3_CI_POLL_SECONDS", "60"))
STATUSES = {"a3-smoke": "A3-2p-CI", "a3-8p": "A3-8p-CI",
            "a3-16p": "A3-16p-CI"}
STATE_MUTEX = threading.RLock()


def task_identity(run: dict) -> str:
    return f"{run['id']}:{run.get('run_attempt', 1)}"


def read_state() -> dict:
    with STATE_MUTEX:
        if not STATE.exists():
            return {}
        return json.loads(STATE.read_text())


def update_state(key: str, **fields):
    with STATE_MUTEX:
        state = read_state()
        current = state.get(key, {})
        current.update(fields)
        state[key] = current
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(STATE)


def eligible(run: dict, case: dict) -> bool:
    return (case["enabled"]
            and run.get("event") == "workflow_dispatch"
            and run.get("status") == "in_progress"
            and run.get("head_branch") in BRANCHES
            and run.get("actor", {}).get("login") == OWNER
            and run.get("path") == ".github/workflows/" + case["workflow"]
            and run.get("name") == STATUSES.get(case["name"])
            and isinstance(run.get("id"), int)
            and isinstance(run.get("run_attempt", 1), int)
            and bool(re.fullmatch("[0-9a-f]{40}", run.get("head_sha", ""))))


def marker(case: dict, run: dict) -> str:
    return f"{STATUSES[case['name']]}-RESULT {task_identity(run)}"


def make_report(case: dict, run: dict, exit_code: int, lines: list[str]) -> dict:
    return {
        "run_id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "sha": run["head_sha"],
        "pipeline": case["pipeline_id"],
        "status": "PASS" if exit_code == 0 else "FAIL",
        "exit_code": exit_code,
        "last_20_lines": [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)[:180]
                          for s in lines[-20:]],
    }


def report_payload(case: dict, run: dict, report: dict) -> str:
    return marker(case, run) + "\n" + json.dumps(
        report, ensure_ascii=False, separators=(",", ":"))


def report_to_github(case: dict, run: dict, report: dict, log_path: Path):
    key = task_identity(run)
    client = GitHubClient(authorization())
    body = report_payload(case, run, report)
    if len(body.encode("utf-8")) > 6100:
        # Final POST serializes the body again; keep room under 7KB.
        raise ValueError("result exceeds conservative 6100 byte limit")
    # Avoid double posting when POST succeeded but the network acknowledgement failed.
    already = any((item.get("body") or "").startswith(marker(case, run) + "\n")
                  for item in client.commit_comments(run["head_sha"]))
    if not already:
        client.send_result(run["head_sha"], body)
    metrics = {
        "time": datetime.now(timezone.utc).isoformat(),
        "run_id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "status": report["status"],
        "final_post_body_bytes": len(json.dumps({"body":body},
                                                  separators=(",", ":"),
                                                  ensure_ascii=False).encode()),
        "api_request_upload_bytes_estimate": client.sent_bytes,
        "api_request_count": client.requests,
        "full_log": str(log_path),
        "note": "excludes background polls, proxy, TLS, TCP and retries from other runs",
    }
    stats = log_path.parent / "upload-metrics.json"
    stats.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    update_state(key, phase="reported", result=report["status"])
    logging.info("reported run=%s pipeline=%s %s",run["id"],case["name"],report["status"])


def handle_run(case: dict, run: dict):
    key = task_identity(run)
    folder = task_dir(run, ROOT)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "pipeline.log"
    saved = read_state().get(key)
    if saved and saved.get("phase") == "pending_report":
        report = saved["report"]
    else:
        try:
            execution = execute(case, run, root=ROOT, lock_root=ROOT / "state/device-locks",
                                resource_wait_seconds=900)
            output = execution.log
            report = make_report(case, run, execution.exit_code, execution.tail)
        except Exception as exc:
            # Fail closed: exceptions must not turn into PASS.
            if not output.exists():
                output.write_text(f"CI infra error: {type(exc).__name__}: {str(exc)[:160]}\n")
            report = make_report(case, run, 125,
                                 [f"CI infrastructure failure: {type(exc).__name__}: {str(exc)[:160]}"])
        update_state(key, phase="pending_report", report=report)
    try:
        report_to_github(case, run, report, output)
    except Exception as exc:
        logging.error("POST pending run=%s: %s",run["id"],type(exc).__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="One poll only")
    parser.add_argument("--validate", action="store_true", help="Validate without GitHub network")
    args = parser.parse_args()
    cases = load_config(ROOT)
    if args.validate:
        print("Valid pipelines:", ",".join(sorted(cases)))
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(LOG,maxBytes=5*1024*1024,backupCount=3),
                                  logging.StreamHandler()])
    client = GitHubClient(authorization())
    workflow_map = {case["workflow"]:case for case in cases.values()
                    if case.get("enabled")}
    inflight = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as workers:
        while True:
            for key, job in list(inflight.items()):
                if job.done():
                    try: job.result()
                    except Exception: logging.exception("worker failed for %s",key)
                    del inflight[key]
            try:
                runs = client.running_workflows()
                for run in runs:
                    filename = (run.get("path") or "").rsplit("/",1)[-1]
                    case = workflow_map.get(filename)
                    if not case or not eligible(run,case):
                        continue
                    key = task_identity(run)
                    if key in inflight:
                        continue
                    previous = read_state().get(key,{})
                    if previous.get("phase") in ("reported","running"):
                        continue
                    if previous.get("phase") != "pending_report":
                        update_state(key,phase="running",case=case["name"],sha=run["head_sha"])
                    inflight[key] = workers.submit(handle_run,case,run)
            except Exception:
                logging.exception("poll failed")
            if args.once:
                break
            time.sleep(INTERVAL)
        for job in inflight.values():
            job.result()


if __name__=="__main__":
    main()
