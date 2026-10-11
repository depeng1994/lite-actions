"""Run admission -> sequential Case tasks -> independent GitHub results.

Phase 1 safety invariant: one global dispatcher execution worker.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path

from ci_core.dispatch import execute, task_dir, prepare_profile
from ci_core.github import GitHubClient, authorization
from ci_core.job_state import JobState
from ci_core.logs import clean
from ci_core.protocol import parse_plan
from ci_core.source_repo import REPO
from ci_core.reporting import make_report, job_key, post_result
from ci_core.scheduler import claim_dynamic, ResourcesUnavailable, UnsafeResourceState


def task_key(run: dict, test_id: str) -> str:
    return f"{run['id']}:{run.get('run_attempt', 1)}:{test_id}"


def case_marker(run: dict, test_id: str) -> str:
    return f"Lite-CI-RESULT {run['id']}:{run.get('run_attempt', 1)}:{test_id}"


def result_for_case(case: dict, run: dict, test_id: str, rc: int,
                    excerpt: list[str], log: Path | None,
                    *, infra: bool = False) -> dict:
    report = make_report(case, run, rc, excerpt, log)
    checks = report.get("cases", [])
    valid = len(checks) == 1 and checks[0]["test_name"] == test_id
    if rc == 130:
        status = "CANCELLED"
    elif infra or not valid:
        status = "INFRA_ERROR"
    elif checks[0]["status"] == "PASS" and rc == 0 and report["status"] == "PASS":
        status = "PASS"
    elif checks[0]["status"] == "NOT_RUN":
        status = "NOT_RUN"
    else:
        status = "FAIL"
    return {"schema": 2, "repo": REPO,
            "run_id": run["id"], "attempt": run.get("run_attempt", 1),
            "sha": run["head_sha"], "pipeline": case["pipeline_id"],
            "test_id": test_id, "plan_digest": run["_ci_request"]["plan_digest"],
            "status": status, "exit_code": rc,
            "last_20_lines": [clean(s) for s in excerpt[:20]]}


def post_case(client: GitHubClient, run: dict, test_id: str, report: dict) -> None:
    marker = case_marker(run, test_id)
    body = marker + "\n" + json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    if len(body.encode("utf-8")) > 6100:
        raise ValueError("case result over GitHub upload budget")
    author = os.environ.get("A3_CI_RESULT_AUTHOR")
    prior = [item.get("body", "") for item in client.commit_comments(run["head_sha"])
             if (not author or item.get("user", {}).get("login") == author)
             and (item.get("body") or "").startswith(marker + "\n")]
    if prior:
        if body not in prior:
            raise ValueError("conflicting CI result already published")
        return
    client.send_result(run["head_sha"], body)


def post_progress_running(client: GitHubClient, run: dict, *, pipeline: str,
                          test_id: str, plan_digest: str = "") -> None:
    """One tiny, identity-scoped transition per Case; no periodic uploads.

    The GitHub commit-comments endpoint itself scopes this marker to the SHA.
    This is display-only telemetry, not a PASS/FAIL authorization signal.
    """
    marker = f"LCI-R {run['id']}:{run.get('run_attempt', 1)}:{test_id}"
    author = os.environ.get("A3_CI_RESULT_AUTHOR")
    prior = [item for item in client.commit_comments(run["head_sha"])
             if (not author or item.get("user", {}).get("login") == author)
             and (item.get("body") or "") == marker]
    if not prior:
        client.send_result(run["head_sha"], marker)


def announce_running(client: GitHubClient, run: dict, *, pipeline: str,
                     test_id: str, plan_digest: str = "") -> None:
    try:
        post_progress_running(client, run, pipeline=pipeline, test_id=test_id,
                              plan_digest=plan_digest)
    except Exception:
        # Progress telemetry must not turn a real training PASS into FAIL.
        logging.warning("CI progress upload unavailable: %s/%s", run["id"], test_id,
                        exc_info=True)


def _run_matrix_and_report(case: dict, run: dict, *, root: Path) -> None:
    items = parse_plan(run["_ci_request"], run, case["pipeline_id"])
    limit = {"a3-8p": 4, "a3-16p": 2, "a5-64p": 1}[case["name"]]
    if len(items) > limit:
        raise ValueError("CI plan exceeds channel case limit")
    state = JobState(root)
    client = GitHubClient(authorization())
    run_key = f"{run['id']}:{run.get('run_attempt', 1)}"
    if state.read().get(run_key, {}).get("phase") == "queued":
        state.update(run_key, phase="running")
    for item in items:
        test_id = item["test_id"]
        key = task_key(run, test_id)
        current = state.read().get(key, {})
        if current.get("phase") == "reported":
            continue
        if current.get("phase") == "pending_report":
            report = current["report"]
        elif current.get("phase") in ("running", "blocked_recovery"):
            # Never restart unknown remote children after a dispatcher crash.
            state.update(key, phase="blocked_recovery", quarantined=True)
            state.update(f"{run['id']}:{run.get('run_attempt', 1)}",
                         phase="blocked_recovery", reason="unverified remote Case")
            logging.error("unfinished CI case needs remote recovery: %s", key)
            return
        elif (run.get("_ci_cancel_event") is not None
              and run["_ci_cancel_event"].is_set()):
            report = {"schema": 2, "repo": REPO,
                      "run_id": run["id"], "attempt": run.get("run_attempt", 1),
                      "sha": run["head_sha"], "pipeline": case["pipeline_id"],
                      "test_id": test_id, "plan_digest": run["_ci_request"]["plan_digest"],
                      "status": "CANCELLED", "exit_code": 130,
                      "last_20_lines": ["GitHub Run cancelled before dispatch"]}
            state.update(key, phase="pending_report", report=report)
        else:
            subrun = {**run, "_ci_test_id": test_id,
                      "_ci_request": {"schema": 1, "run_id": run["id"],
                                      "attempt": run.get("run_attempt", 1),
                                      "sha": run["head_sha"],
                                      "cases": [{"test_id": test_id}]}}
            expected = (item["nnodes"], item["ngpu"])
            if "resources" in case:
                assigned = (case["resources"]["nodes"], case["resources"]["devices_per_node"])
            else:
                allocation = case["allocation"]
                assigned = (len(allocation), len(allocation[0]["devices"])) if allocation else (0, 0)
            if expected != assigned:
                raise ValueError(f"Case topology unauthorized {test_id}: {expected}!={assigned}")
            state.update(key, phase="running", pipeline=case["name"],
                         sha=run["head_sha"], test_id=test_id)
            log = task_dir(subrun, root) / "pipeline.log"
            try:
                if "resources" in case:
                    actual_test, profile = prepare_profile(case, subrun)
                    if actual_test != test_id:
                        raise ValueError("pinned model Test ID mismatch")
                    with claim_dynamic(root, case, ascend_env=profile["ascend_env"],
                                       timeout=900, owner_key=key,
                                       canceled=(run.get("_ci_cancel_event").is_set
                                                 if run.get("_ci_cancel_event")
                                                 is not None else None)) as selected:
                        # Persist the exact assigned devices before remote SSH.
                        state.update(key, allocation=selected["allocation"])
                        announce_running(client, run, pipeline=case["pipeline_id"],
                                         test_id=test_id,
                                         plan_digest=run["_ci_request"]["plan_digest"])
                        execution = execute(selected, subrun, root=root,
                                            lock_root=root / "state/device-locks",
                                            pre_reserved=True)
                else:
                    announce_running(client, run, pipeline=case["pipeline_id"],
                                     test_id=test_id,
                                     plan_digest=run["_ci_request"]["plan_digest"])
                    execution = execute(case, subrun, root=root,
                                        lock_root=root / "state/device-locks",
                                        resource_wait_seconds=900)
                if not execution.release_verified:
                    state.update(key, quarantined=True)
                report = result_for_case(case, {**subrun, "_ci_request": run["_ci_request"]},
                                         test_id, execution.exit_code,
                                         execution.tail, execution.log,
                                         infra=not execution.release_verified)
            except Exception as exc:
                logging.exception("CI case execution error: %s", key)
                # If a remote assignment existed, an exceptional path
                # cannot safely prove the remote ranks have stopped.
                if state.read().get(key, {}).get("allocation"):
                    state.update(key, quarantined=True)
                log.parent.mkdir(parents=True, exist_ok=True)
                if not log.exists():
                    log.write_text(f"Dispatcher error: {type(exc).__name__}\n")
                report = result_for_case(
                    case, {**subrun, "_ci_request": run["_ci_request"],
                           "_ci_expanded_cases": [{"test_id": test_id}]},
                    test_id, 125, [f"Dispatcher error: {type(exc).__name__}"],
                    log, infra=True)
                if (run.get("_ci_cancel_event") is not None
                        and run["_ci_cancel_event"].is_set()):
                    report["status"] = "CANCELLED"
                    report["exit_code"] = 130
                elif isinstance(exc, ResourcesUnavailable) and not isinstance(exc, UnsafeResourceState):
                    report["status"] = "RESOURCE_TIMEOUT"
            state.update(key, phase="pending_report", report=report)
        try:
            post_case(client, run, test_id, report)
        except Exception:
            logging.exception("CI result upload pending: %s", key)
            return
        state.update(key, phase="reported", result=report["status"])
        logging.info("CI Case reported %s %s", key, report["status"])
    state.update(f"{run['id']}:{run.get('run_attempt', 1)}",
                 phase="reported", result="COMPLETE",
                 time=datetime.now(timezone.utc).isoformat())


def _run_local_and_report(case: dict, run: dict, *, root: Path) -> None:
    """Run an authorized local probe and post the existing smoke result."""
    state = JobState(root)
    key = job_key(run)
    output = task_dir(run, root) / "pipeline.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = state.read().get(key, {})
    if pending.get("phase") == "queued":
        state.update(key, phase="running")
    if pending.get("phase") == "pending_report":
        report = pending["report"]
    else:
        announce_running(GitHubClient(authorization()), run,
                         pipeline=case["pipeline_id"], test_id="npu-smi")
        try:
            if "resources" in case:
                from ci_core.dispatch import prepare_profile
                from ci_core.scheduler import claim_dynamic
                _, profile = prepare_profile(case, run)
                with claim_dynamic(root, case, ascend_env=profile["ascend_env"],
                                   timeout=900, owner_key=key) as runtime_case:
                    execution = execute(runtime_case, run, root=root,
                                        lock_root=root / "state/device-locks",
                                        pre_reserved=True)
            else:
                execution = execute(case, run, root=root,
                                    lock_root=root / "state/device-locks",
                                    resource_wait_seconds=900)
            output = execution.log
            report = make_report(case, run, execution.exit_code, execution.tail, execution.log)
        except Exception as exc:
            if not output.exists():
                output.write_text(f"CI infra error: {type(exc).__name__}\n")
            report = make_report(case, run, 125,
                                 [f"Dispatcher error: {type(exc).__name__}"],output)
        state.update(key, phase="pending_report", report=report)
    try:
        post_result(case, run, report, output, state)
    except Exception as exc:
        logging.error("GitHub POST pending for run=%s: %s",
                      run["id"], type(exc).__name__)


def run_and_report(case: dict, run: dict, *, root: Path) -> None:
    """Only dispatcher entrypoint, routed by the authorized runner kind.

    A local smoke has no Matrix plan. All SSH training requires the current
    schema-2 immutable plan; old SSH request formats are not accepted.
    """
    kind = case.get("runner", {}).get("kind")
    if kind == "local":
        if "_ci_request" in run:
            raise ValueError("local smoke must not accept a CI plan")
        return _run_local_and_report(case, run, root=root)
    if kind != "ssh":
        raise ValueError("unsupported CI runner kind")
    if run.get("_ci_request", {}).get("schema") != 2:
        raise ValueError("SSH Case requires a schema-2 CI plan")
    return _run_matrix_and_report(case, run, root=root)
