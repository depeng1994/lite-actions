"""Bounded remote log transfer; all content selection runs on dispatcher Python."""
from __future__ import annotations

from collections import deque
import re
import shlex
import subprocess
from pathlib import Path

ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SAFE_PATH = re.compile(r"^/mnt/share/ci_tests/[A-Za-z0-9_-]+/(?:node[0-9]+/)?run\.log$")
METRIC = re.compile(r"tps\b|elapsed_time_per_step|\bstep:\s*[1-9]|\bloss:", re.I)
ERROR = re.compile(r"Traceback|Error:|Exception:|RuntimeError|AssertionError|FAILED|fatal", re.I)
RANK0 = re.compile(r"rank0\b|rank[_= ]0\b|global_rank_0", re.I)
MAX_REMOTE_BYTES = 131072


def remote_excerpt(host: str, path: str, *, approved_hosts: set[str], runner=subprocess.run) -> str:
    """Retrieve only the last 128 KiB from approved execution hosts."""
    if host not in approved_hosts or not SAFE_PATH.fullmatch(path):
        raise ValueError("untrusted execution log locator")
    command = "tail -c 131072 -- " + shlex.quote(path)
    result = runner(
        __import__("ci_core.transport",fromlist=["command"]).command(host,command),
        capture_output=True, timeout=45, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"remote log unavailable ({host}, rc={result.returncode})")
    if len(result.stdout) > MAX_REMOTE_BYTES:
        raise ValueError("remote log exceeds bounded transfer")
    return result.stdout.decode("utf-8", errors="replace")


def clean(line: str) -> str:
    return "".join(ch if (ord(ch) >= 32 and ord(ch) != 127) else " "
                   for ch in ESCAPE.sub("", line))[:180]


def select_lines(data: str, exit_code: int) -> list[str]:
    """Uniform dispatcher-side selection, never executed on training machines."""
    lines = [clean(line) for line in data.splitlines() if line.strip()]
    if exit_code == 0:
        metric = [line for line in lines if METRIC.search(line) and RANK0.search(line)]
        if not metric:
            metric = [line for line in lines if METRIC.search(line)]
        return metric[-20:] or lines[-20:] or ["Training finished without log content"]
    matches = [i for i, line in enumerate(lines) if ERROR.search(line)]
    if matches:
        selected = []
        for i in matches[-3:]:
            selected.extend(lines[max(0, i - 1):min(len(lines), i + 4)])
        selected += lines[-8:]
        selected = list(dict.fromkeys(selected))
        return selected[-20:]
    return lines[-20:] or [f"Process exited with {exit_code}, no diagnostics"]


def select_remote(host: str, path: str, exit_code: int, *,
                  approved_hosts: set[str], runner=subprocess.run) -> list[str]:
    return select_lines(remote_excerpt(host, path, approved_hosts=approved_hosts,
                                       runner=runner), exit_code)


def metadata(log: Path) -> dict[str, str]:
    """Read trusted RESULT_HOST/RESULT_LOG emitted by pinned pipeline snapshot."""
    found = {}
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("RESULT_HOST: "):
                found["host"] = line.partition(": ")[2].strip()
            elif line.startswith("RESULT_LOG: "):
                found["path"] = line.partition(": ")[2].strip()
    if len(found) != 2:
        raise ValueError("missing remote log metadata")
    return found
