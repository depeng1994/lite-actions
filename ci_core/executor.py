"""Execute one pinned model case while Router holds its complete resource lease."""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ci_core.broadcast import broadcast_job
from ci_core.logs import metadata, select_remote
from ci_core.registry import resolve_test

BASE = Path(__file__).resolve().parents[1]
CHIP_PROCESS = re.compile(r"^\|\s*(\d+)\s*([01])\s*\|\s*(\d+)\s*\|")
MARKER_START = "RESULT_LAST_20_LINES_BEGIN"
MARKER_END = "RESULT_LAST_20_LINES_END"


class ExternalNpuOccupancy(RuntimeError):
    """External activity detected before any CI process has started."""


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
    return (
        root
        / "logs/runs"
        / (
            f"{local:%Y-%m-%d_%H-%M-%S}_run-{int(run['id'])}"
            f"_attempt-{int(run.get('run_attempt', 1))}{suffix}"
        )
    )


def remote_task_name(run: dict) -> str:
    """Human-readable execution-node folder: time + workflow + run_number.

    GitHub's workflow run_number is the #6 shown in Actions, not the global
    run ID and not run_attempt. Append attempt only for reruns to avoid a
    collision with the original execution's immutable training logs.
    """
    name = run.get("name")
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", name
    ):
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


def process_evidence(output: str, devices: list[int], *, indexing: str) -> list[tuple[int, int]]:
    """Return (allocated chip, container PID); reject ambiguous process rows."""
    if "Process id in container" not in output:
        raise ValueError("npu-smi process table absent")
    if indexing not in ("card", "card-die"):
        raise ValueError("unknown NPU process index mapping")
    selected = set(devices)
    rows = []
    for line in output.split("Process id in container", 1)[1].splitlines():
        if "No running processes found in NPU" in line:
            continue
        if not line.lstrip().startswith("|"):
            continue
        cells = [v.strip() for v in line.strip().strip("|").split("|")]
        if indexing == "card-die":
            match = CHIP_PROCESS.match(line)
            if match:
                chip = int(match[1]) * 2 + int(match[2])
                pid = int(match[3])
                rows.append((chip, pid))
            elif re.match(r"^\\|\\s*\\d+", line):
                raise ValueError("unrecognized NPU process row")
        elif len(cells) == 5 and cells[0].isdigit() and cells[1].isdigit() and cells[-1].isdigit():
            # Actual A5 row: NPU ID | host PID | name | memory | container PID.
            rows.append((int(cells[0]), int(cells[-1])))
        elif re.match(r"^\\|\\s*\\d+", line):
            raise ValueError("unrecognized NPU process row")
    # Never kill a PID which also appears on an unallocated chip.
    targets = {pid for chip, pid in rows if chip in selected}
    if any(chip not in selected and pid in targets for chip, pid in rows):
        raise ValueError("PID spans allocated and unallocated NPU devices")
    if any(pid <= 1 for chip, pid in rows if chip in selected):
        raise ValueError("unsafe NPU process PID")
    return [(chip, pid) for chip, pid in rows if chip in selected]


def selected_processes(
    output: str, devices: list[int], *, indexing: str = "card-die"
) -> list[str]:
    return [f"chip {chip}: pid {pid}" for chip, pid in process_evidence(output, devices, indexing=indexing)]


def remote_preflight(
    case: dict, pools: dict, *, ascend_env: str | None = None,
    evidence=None,
) -> None:
    """Inspect every allocated device; reclaim only for trusted yellow/A5 pools."""
    if not case.get("allocation"):
        return
    pool = pools[case["pool"]]
    reclaim = pool.get("force_reclaim", False)
    if type(reclaim) is not bool or (reclaim and (pool.get("owner") != "yellow" or pool.get("type") != "ascend-a5")):
        raise ValueError("invalid force_reclaim pool")
    by_host = {x["host_id"]: x for x in pool["nodes"]}
    indexing = pool.get("process_indexing")
    if indexing not in ("card", "card-die"):
        raise ValueError("NPU process index mode must be configured")
    from ci_core.transport import run as transport_run
    for request in case["allocation"]:
        node = by_host[request["host_id"]]
        host = node.get("ssh")
        if not isinstance(host, str) or not host:
            raise ValueError("NPU preflight host SSH is not configured")
        command = "npu-smi info"
        if ascend_env:
            command = "source " + shlex.quote(ascend_env) + " >/dev/null 2>&1 && npu-smi info"
        if case.get("docker"):
            from ci_core.config import execution_node
            node = execution_node(node, case["docker"])
            host = node["ssh"]
        rounds = 4 if reclaim else 1
        for attempt in range(rounds):
            proc = transport_run(host, command, timeout=40, check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"NPU occupancy query failed on {request['host_id']}")
            found = process_evidence(proc.stdout, request["devices"], indexing=indexing)
            if evidence is not None:
                evidence.write(f"RECLAIM_EVIDENCE host={request['host_id']} round={attempt} allocated={request['devices']} chips_pids={found}\\n")
                evidence.flush()
            if not found:
                break
            if not reclaim:
                raise ExternalNpuOccupancy(f"External NPU activity on {request['host_id']}: {found[:3]}")
            # Recheck immediately before signalling; refuse PIDs on other chips.
            verify = transport_run(host, command, timeout=40, check=False)
            if verify.returncode != 0:
                raise RuntimeError("NPU reclaim confirmation failed")
            confirmed = process_evidence(verify.stdout, request["devices"], indexing=indexing)
            for pid in sorted(set(pid for _, pid in found) & set(pid for _, pid in confirmed)):
                result = transport_run(host, "kill -9 -- " + str(pid), timeout=20, check=False)
                if evidence is not None:
                    evidence.write(f"RECLAIM_KILL host={request['host_id']} pid={pid} rc={result.returncode}\\n")
                    evidence.flush()
                if result.returncode != 0:
                    raise RuntimeError(f"NPU reclaim kill failed on {request['host_id']} pid={pid}")
            if attempt == rounds - 1:
                # The final state must be verified, not assumed from kill status.
                final = transport_run(host, command, timeout=40, check=False)
                if final.returncode != 0 or process_evidence(final.stdout, request["devices"], indexing=indexing):
                    raise RuntimeError(f"NPU reclaim not idle on {request['host_id']}")
        else:
            raise RuntimeError(f"NPU reclaim attempts exhausted on {request['host_id']}")


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


def prepare_profile(case: dict, run: dict) -> dict:
    from ci_core.ssh_runner import model_archive

    with tempfile.TemporaryDirectory(prefix="lite-model-") as tmp:
        archive = model_archive(run["head_sha"], Path(tmp))
        definition = resolve_test(
            archive,
            run["_ci_test_id"],
            nodes=case["resources"]["nodes"],
            ngpu=case["resources"]["devices_per_node"],
        )
    return definition["env_vars"]


def execute(case: dict, run: dict, *, root: Path = BASE) -> Execution:
    if not case.get("enabled"):
        raise ValueError("disabled case")
    cancel = run.get("_ci_cancel_event")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("cancelled before launch")
    environment = prepare_profile(case, run)
    setup = environment["ASCEND_SET_ENV_PATH"]
    pools = json.loads((root / "config/pools.json").read_text())
    from ci_core.scheduler import validate_allocation

    validate_allocation(case, pools, case["allocation"])
    log_dir = task_dir(run, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    runtime_file = log_dir / "runtime-case.json"
    runtime_file.write_text(json.dumps(case) + "\n")
    command = [
        sys.executable,
        "-u",
        "-m",
        "ci_core.ssh_runner",
        "--case-file",
        str(runtime_file),
        "--test-id",
        run["_ci_test_id"],
        "--sha",
        run["head_sha"],
        "--stamp",
        log_dir.name,
        "--remote-dir",
        remote_task_name(run),
    ]
    callback = run.get("_ci_phase_callback", lambda phase: None)
    release_verified = False
    announced = False
    reclaim_started = False
    with logfile.open("w", encoding="utf-8") as stream:
        rc = 125
        try:
            reclaim_started = True
            remote_preflight(case, pools, ascend_env=setup, evidence=stream)
            failures = broadcast_job(case, pools, run, "begin")
            if failures:
                raise RuntimeError("CI begin wall failed on " + ",".join(failures))
            announced = True
            callback("LAUNCHING")
            proc = subprocess.Popen(
                command,
                cwd=root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 600
                staged = False
                while True:
                    if not staged:
                        stream.flush()
                        staged = logfile.read_text(errors="replace").count(
                            "STAGED: "
                        ) >= len(case["allocation"])
                        if staged:
                            callback("RUNNING")
                            deadline = (
                                time.monotonic() + case["execution_timeout_seconds"]
                            )
                    if proc.poll() is not None:
                        rc = proc.returncode
                        break
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("GitHub Run cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            command,
                            case["execution_timeout_seconds"] if staged else 600,
                        )
                    try:
                        rc = proc.wait(timeout=min(5.0, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except (subprocess.TimeoutExpired, InterruptedError) as exc:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                rc = 130 if isinstance(exc, InterruptedError) else 124
                stream.write(
                    f"ERROR: case interrupted rc={rc}; verifying remote cleanup\n"
                )
        except Exception as exc:
            stream.write(f"ERROR: {type(exc).__name__}: {str(exc)[:240]}\n")
            logging.exception("case execution failed run=%s", run["id"])
            rc = 125
        finally:
            callback("CLEANING")
            if announced or reclaim_started:
                from ci_core.ssh_runner import (
                    DEST_ROOT,
                    allocations,
                    owned_processes_gone,
                )

                for attempt in range(3):
                    try:
                        remote_preflight(case, pools, ascend_env=setup, evidence=stream)
                        if announced:
                            for node in allocations(case, pools):
                                destination = f"{DEST_ROOT}/{remote_task_name(run)}/node{node['position']}"
                                owned_processes_gone(node, destination, log_dir.name)
                        release_verified = True
                        break
                    except (RuntimeError, subprocess.SubprocessError, ValueError):
                        if attempt < 2:
                            time.sleep(2)
                try:
                    if announced:
                        broadcast_job(case, pools, run, "end", released=release_verified)
                except Exception:
                    logging.exception("CI exit notification failed")
                if not release_verified:
                    stream.write("ERROR: remote resource release unverified\n")
                    rc = rc or 125
            else:
                release_verified = True
            stream.flush()
    try:
        from ci_core.config import execution_node

        location = metadata(logfile)
        selected_hosts = {n["host_id"] for n in case["allocation"]}
        approved = {
            execution_node(n, case["docker"])["ssh"]
            for n in pools[case["pool"]]["nodes"]
            if n["host_id"] in selected_hosts
        }
        excerpt = select_remote(
            location["host"],
            location["path"],
            rc,
            approved_hosts=approved,
            require_loss=case["kind"] == "train",
        )
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        excerpt = summarize(logfile, rc)[-18:]
        excerpt.append(f"Log collection failed: {type(exc).__name__}: {str(exc)[:120]}")
        if rc == 0:
            rc = 125
    return Execution(rc, excerpt[:20], logfile, release_verified)
