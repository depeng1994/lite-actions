"""Dispatcher boundary: trusted local/SSH pipelines under physical NPU leases.

This module does not speak GitHub and never kills unknown processes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import collections
import datetime as dt
import re
import subprocess
import shutil
import shlex
import os
import signal

from ci_core.resources import wait_reserve


BASE = Path(__file__).resolve().parents[1]
CHIP_PROCESS = re.compile(r"^\|\s*(\d+)\s+([01])\s*\|\s*(\d+)\s*\|")
MARKER_START = "RESULT_LAST_20_LINES_BEGIN"
MARKER_END = "RESULT_LAST_20_LINES_END"
SCRIPTS = {
    "local": BASE / "pipeline.sh",
    "legacy_8p": BASE / "legacy/github/scripts/a3-8p-remote-pipeline.sh",
    "legacy_16p": BASE / "legacy/github/scripts/a3-16p-remote-pipeline.sh",
}


@dataclass
class Execution:
    exit_code: int
    tail: list[str]
    log: Path


def task_dir(run: dict, root: Path) -> Path:
    timestamp = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    local = timestamp.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return root / "logs/runs" / (
        f"{local:%Y-%m-%d_%H-%M-%S}_run-{int(run['id'])}"
        f"_attempt-{int(run.get('run_attempt', 1))}"
    )


def selected_processes(output: str, devices: list[int]) -> list[str]:
    """Fail closed if NPU process table is unrecognizable.

    This particular A3 environment exposes two physical chip IDs per card;
    npu-smi reports (card, die, PID) in its process section.
    """
    if "Process id in container" not in output:
        raise ValueError("npu-smi process table absent")
    selected = set(devices)
    processes = []
    for line in output.split("Process id in container")[-1].splitlines():
        match = CHIP_PROCESS.match(line)
        if match:
            chip_id = int(match.group(1)) * 2 + int(match.group(2))
            if chip_id in selected:
                processes.append(f"chip {chip_id}: pid {match.group(3)}")
    return processes


def remote_preflight(case: dict, pools: dict) -> None:
    """Read-only external occupancy precheck; no preemption."""
    if not case.get("allocation"):
        return
    pool = pools[case["pool"]]
    by_host = {x["host_id"]: x for x in pool["nodes"]}
    if pool["type"] != "ascend-a3":
        raise ValueError("Only A3 real hardware has a verified process-table parser")
    for request in case["allocation"]:
        host = by_host[request["host_id"]]["ssh"]
        setup = "/mnt/share/Ascend/20260805101249091/ascend-toolkit/latest/set_env.sh"
        command = f"source {setup} >/dev/null 2>&1 && npu-smi info"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             host, "bash -lc " + shlex.quote(command)],
            capture_output=True, text=True, timeout=40,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"NPU preflight failed on {request['host_id']}")
        found = selected_processes(proc.stdout, request["devices"])
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
            resource_wait_seconds: int = 600) -> Execution:
    if not case.get("enabled"):
        raise ValueError("Disabled pipeline cannot run on real hardware")
    kind = case["runner"]["kind"]
    if kind == "mock":
        raise ValueError("A5 mock must never launch hardware")
    log_dir = task_dir(run, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    script = SCRIPTS[kind] if root == BASE else (
        root / ("pipeline.sh" if kind == "local" else
                "legacy/github/scripts/a3-8p-remote-pipeline.sh" if kind == "legacy_8p" else
                "legacy/github/scripts/a3-16p-remote-pipeline.sh")
    )
    if not script.exists():
        raise FileNotFoundError(script)
    stable = log_dir / "pipeline-snapshot.sh"
    shutil.copyfile(script, stable)
    cmd = ["bash", str(stable)]
    if kind != "local":
        cmd += [str(run["id"]), str(run.get("run_attempt", 1)),
                run["head_sha"], log_dir.name]
    import json
    pools = json.loads((root / "config/pools.json").read_text())
    lock_root = lock_root or root / "state/device-locks"
    with wait_reserve(lock_root, case.get("allocation", []),
                      timeout=resource_wait_seconds):
        remote_preflight(case, pools)
        with logfile.open("w", encoding="utf-8") as stream:
            try:
                proc = subprocess.Popen(
                    cmd, cwd=root, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    rc = proc.wait(timeout=case["timeout_seconds"])
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    stream.write("\nERROR: dispatcher timeout; check remote child cleanup\n")
                    rc = 124
            except Exception as exc:
                stream.write(f"\nERROR: {type(exc).__name__}\n")
                rc = 125
    return Execution(rc, summarize(logfile, rc), logfile)
