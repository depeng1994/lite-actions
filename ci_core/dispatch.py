"""Dispatcher boundary: trusted local/SSH pipelines under physical NPU leases.

This module does not speak GitHub and never kills unknown processes.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
import collections
import datetime as dt
import re
import subprocess
import shlex
import sys
import os
import signal
import tempfile
import json

from ci_core.resources import wait_reserve
from ci_core.broadcast import broadcast_job
from ci_core.logs import metadata, select_remote
from ci_core.registry import workflow_label, parse_request, resolve_request
import logging
import time


BASE = Path(__file__).resolve().parents[1]
CHIP_PROCESS = re.compile(r"^\|\s*(\d+)\s+([01])\s*\|\s*(\d+)\s*\|")
MARKER_START = "RESULT_LAST_20_LINES_BEGIN"
MARKER_END = "RESULT_LAST_20_LINES_END"
SCRIPTS = {"local": BASE / "pipeline.sh"}


@dataclass
class Execution:
    exit_code: int
    tail: list[str]
    log: Path
    release_verified: bool = True


def _local_run_time(run: dict) -> dt.datetime:
    """GitHub timestamps are UTC; paths use Beijing time (UTC+08:00)."""
    timestamp = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    return timestamp.astimezone(dt.timezone(dt.timedelta(hours=8)))


def task_dir(run: dict, root: Path) -> Path:
    """Keep globally unique run ID in dispatcher logs/state for correlation."""
    local = _local_run_time(run)
    suffix = ""
    if "_ci_test_id" in run:
        from ci_core.registry import SELECTOR
        test_id = run["_ci_test_id"]
        if not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id):
            raise ValueError("invalid CI task Test ID")
        suffix = f"_test-{test_id}"
    return root / "logs/runs" / (
        f"{local:%Y-%m-%d_%H-%M-%S}_run-{int(run['id'])}"
        f"_attempt-{int(run.get('run_attempt', 1))}{suffix}"
    )


def remote_task_name(run: dict) -> str:
    """Human-readable execution-node folder: time + workflow + run_number.

    GitHub's workflow run_number is the #6 shown in Actions, not the global
    run ID and not run_attempt. Append attempt only for reruns to avoid a
    collision with the original execution's immutable training logs.
    """
    name = run.get("name")
    if isinstance(name, str) and " | test=" in name:
        name = workflow_label(run)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", name):
        raise ValueError("invalid GitHub workflow name for execution directory")
    number = run.get("run_number")
    attempt = run.get("run_attempt", 1)
    if type(number) is not int or number < 1:
        raise ValueError("GitHub run_number must be a positive integer")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("GitHub run_attempt must be a positive integer")
    local = _local_run_time(run)
    suffix = f"_attempt-{attempt}" if attempt > 1 else ""
    if "_ci_test_id" in run:
        from ci_core.registry import SELECTOR
        test_id = run["_ci_test_id"]
        if not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id):
            raise ValueError("invalid CI test ID")
        suffix += f"_test-{test_id}"
    return f"{local:%Y-%m-%d_%H-%M-%S}_run_{name}_{number}{suffix}"


def selected_processes(output: str, devices: list[int], *, indexing: str = "card-die") -> list[str]:
    """Fail closed if NPU process table is unrecognizable.

    This particular A3 environment exposes two physical chip IDs per card;
    npu-smi reports (card, die, PID) in its process section.
    """
    if "Process id in container" not in output:
        raise ValueError("npu-smi process table absent")
    if indexing not in ("card", "card-die"):
        raise ValueError("unknown NPU process index mapping")
    selected = set(devices)
    processes = []
    for line in output.split("Process id in container")[-1].splitlines():
        match = CHIP_PROCESS.match(line)
        if match:
            chip_id = int(match.group(1)) * 2 + int(match.group(2)) if indexing == "card-die" else int(match.group(1))
            if chip_id in selected:
                processes.append(f"chip {chip_id}: pid {match.group(3)}")
        elif re.match(r"^\|\s*\d+\s+\d+\s*\|", line):
            # An unknown process-row format cannot safely imply that chips are idle.
            raise ValueError("unrecognized NPU process row")
    return processes


def prepare_profile(case:dict,run:dict)->tuple[str,dict]:
    """Resolve suite before locking/staging and freeze the expanded plan."""
    allocation = case.get("allocation", [])
    if "_ci_request" not in run: raise ValueError("CI request artifact required")
    selections=parse_request(run['_ci_request'],run)
    with tempfile.TemporaryDirectory(prefix='lite-ci-validated-model-') as temp:
        from ci_core.ssh_runner import model_archive
        archive=model_archive(run['head_sha'],Path(temp))
        if case.get("resources"):
            nodes = case["resources"]["nodes"]
            ngpu = case["resources"]["devices_per_node"]
        else:
            nodes, ngpu = len(allocation), len(allocation[0]["devices"])
        expanded = resolve_request(archive, selections, nodes=nodes,
            ngpu=ngpu)
    run['_ci_expanded_cases']=expanded
    # Preflight sources the toolkit selected by the model test, never a
    # hardcoded deployment value from Lite Actions.
    return expanded[0]['test_id'], {'ascend_env': expanded[0]['env_vars']['ASCEND_SET_ENV_PATH']}


def remote_preflight(case: dict, pools: dict, *, ascend_env: str | None = None) -> None:
    """Read-only external occupancy precheck; no preemption."""
    if not case.get("allocation"):
        return
    pool = pools[case["pool"]]
    by_host = {x["host_id"]: x for x in pool["nodes"]}
    indexing = pool.get("process_indexing")
    if indexing not in ("card", "card-die"):
        raise ValueError("NPU process index mode must be configured")
    for request in case["allocation"]:
        node = by_host[request["host_id"]]
        # Occupancy is a physical-host property.  Use the same host namespace
        # as PID classification/reclamation instead of the execution container,
        # whose npu-smi visibility depends on its CANN/runtime environment.
        targets = node.get("broadcast_ssh", [])
        if isinstance(targets, list) and targets and isinstance(targets[0], str) and targets[0]:
            host = targets[0]
        else:
            host = node.get("ssh")
        if not isinstance(host, str) or not host:
            raise ValueError("NPU preflight host SSH is not configured")
        command = "npu-smi info"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             host, "bash -lc " + shlex.quote(command)],
            capture_output=True, text=True, timeout=40,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"NPU preflight failed on {request['host_id']}")
        found = selected_processes(proc.stdout, request["devices"], indexing=indexing)
        if found:
            raise RuntimeError(
                f"External NPU activity on {request['host_id']}: {found[:3]}"
            )


def summarize(log: Path, rc: int) -> list[str]:
    """Use selected remote diagnostics; fallback to local dispatcher tail."""
    inside = False
    chosen = collections.deque(maxlen=20)
    tail = collections.deque(maxlen=20)
    with log.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.rstrip("\r\n"))[:180]
            tail.append(line)
            if line == MARKER_START:
                inside = True
                chosen.clear()
            elif line == MARKER_END and inside:
                return list(chosen) or ["No remote log lines returned"]
            elif inside:
                chosen.append(line)
    return list(tail) or [f"Execution returned {rc} without output"]


def execute(case: dict, run: dict, *, root: Path = BASE, lock_root: Path | None = None,
            resource_wait_seconds: int = 600, pre_reserved: bool = False) -> Execution:
    if not case.get("enabled"):
        raise ValueError("Disabled pipeline cannot run on real hardware")
    if (run.get("_ci_cancel_event") is not None
            and run["_ci_cancel_event"].is_set()):
        raise InterruptedError("CI Run already cancelled; refusing to launch")
    kind = case["runner"]["kind"]
    log_dir = task_dir(run, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    setup = None
    if kind == "local":
        script = root / "pipeline.sh"
        if not script.exists():
            raise FileNotFoundError(script)
        cmd = ["bash", str(script)]
    elif kind == "ssh":
        test_id, profile = prepare_profile(case, run)
        setup = profile["ascend_env"]
        cmd = [sys.executable, "-u", "-m", "ci_core.ssh_runner",
               "--case", case["name"], "--sha", run["head_sha"],
               "--stamp", log_dir.name, "--remote-dir", remote_task_name(run)]
        if "_ci_request" in run:
            # The manifest below is resolved/validated by the dispatcher.
            manifest = log_dir / "ci-request.json"
            # The GitHub Artifact contains selectors only; model env_vars are
            # always re-resolved from trusted pinned source, never from input.
            request={**run["_ci_request"],"cases":[
                {'test_id': item['test_id']} for item in run['_ci_expanded_cases']]}
            manifest.write_text(json.dumps(request, separators=(",", ":")) + "\n")
            cmd += ["--request-file", str(manifest), "--run-id", str(run["id"]),
                    "--run-attempt", str(run.get("run_attempt", 1))]
        else:
            cmd += ["--test-case", test_id]
        if case.get("resources"):
            from ci_core.scheduler import validate_allocation
            pools_for_assignment = json.loads((root / "config/pools.json").read_text())
            validate_allocation(case, pools_for_assignment, case.get("allocation", []))
            alloc_path = log_dir / "allocation.json"
            alloc_path.write_text(json.dumps({"allocation": case["allocation"]}) + "\n")
            cmd += ["--allocation-file", str(alloc_path)]
    else:
        raise ValueError(f"unknown trusted runner: {kind}")
    pools = json.loads((root / "config/pools.json").read_text())
    lock_root = lock_root or root / "state/device-locks"
    lease = (nullcontext() if pre_reserved else
             wait_reserve(lock_root, case.get("allocation", []),
                          timeout=resource_wait_seconds))
    release_verified = True
    with lease:
        remote_preflight(case, pools, ascend_env=setup)
        announced = bool(case.get("allocation"))
        with logfile.open("w", encoding="utf-8") as stream:
            rc = 125
            try:
                # Mandatory host-user notification before taking over NPUs.
                if announced:
                    failures = broadcast_job(case, pools, run, "begin")
                    if failures:
                        raise RuntimeError("CI begin wall failed on " + ",".join(failures))
                proc = subprocess.Popen(
                    cmd, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    case_count = len(run.get("_ci_expanded_cases", [])) or 1
                    budget = case["timeout_seconds"] * case_count
                    deadline = time.monotonic() + budget
                    cancel = run.get("_ci_cancel_event")
                    while True:
                        if proc.poll() is not None:
                            rc = proc.returncode
                            break
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError("GitHub Run cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(cmd, budget)
                        try:
                            rc = proc.wait(timeout=min(5.0, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                except (subprocess.TimeoutExpired, InterruptedError) as exc:
                    # Only the locally spawned SSH runner's own session.
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    cancelled = isinstance(exc, InterruptedError)
                    stream.write("\nERROR: dispatcher " +
                                 ("cancelled" if cancelled else "timeout") +
                                 "; checking remote child cleanup\n")
                    rc = 130 if cancelled else 124
            except Exception as exc:
                stream.write(f"\nERROR: {type(exc).__name__}: {str(exc)[:240]}\n")
                logging.exception("CI dispatcher failure run=%s", run["id"])
                rc = 125
            finally:
                if announced:
                    # Exit broadcast must not falsely claim that remote ranks
                    # have released devices merely because SSH exited.
                    idle = False
                    for retry in range(3):
                        try:
                            remote_preflight(case, pools, ascend_env=setup)
                            idle = True
                            break
                        except (RuntimeError, subprocess.SubprocessError, ValueError):
                            if retry < 2:
                                time.sleep(2)
                    phase = "end" if idle else "uncertain"
                    try:
                        errors = broadcast_job(case, pools, run, phase)
                        if errors:
                            # End/uncertain broadcast is best-effort: notification
                            # transport failures do not invalidate confirmed
                            # training exit, TensorBoard, or NPU release.
                            # BEGIN remains mandatory and blocks launch.
                            logging.warning("CI %s wall delivery incomplete: %s",
                                            phase, errors)
                    except Exception:
                        logging.exception("CI exit wall failed (non-fatal alert)")
                    if not idle:
                        release_verified = False
                        stream.write("\nERROR: NPU resource release unverified; "
                                     "remote processes may remain; operator action needed\n")
                        rc = rc or 125
                    stream.flush()
    if kind == "local":
        excerpt = summarize(logfile, rc)
    else:
        try:
            location = metadata(logfile)
            approved = {node["ssh"] for node in pools[case["pool"]]["nodes"] if node.get("ssh")}
            excerpt = select_remote(location["host"], location["path"], rc,
                                    approved_hosts=approved)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            excerpt = summarize(logfile, rc)[-18:]
            excerpt.append(f"Dispatcher log collection failed: {type(exc).__name__}")
            if rc == 0:
                rc = 125
    return Execution(rc, excerpt[:20], logfile, release_verified)
