"""Optional, identity-bound Worker readiness marker for code upgrades."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}\Z")


def process_identity(pid: int) -> tuple[str, int]:
    """Linux boot ID + /proc start ticks prevents stale-PID reuse."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    stat = Path(f"/proc/{pid}/stat").read_text()
    suffix = stat.rsplit(")", 1)[1].split()
    return boot, int(suffix[19])


def actual_sha(root: Path) -> str:
    value = subprocess.check_output(
        ["git", "-C", str(root.resolve()), "rev-parse", "HEAD"],
        timeout=15,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    if not _SHA.fullmatch(value):
        raise ValueError("Worker checkout SHA invalid")
    return value


def publish_ready(root: Path, destination: Path) -> dict:
    """Write after first successful router.tick(); never before it."""
    pid = os.getpid()
    boot, start_ticks = process_identity(pid)
    value = {
        "release_sha": actual_sha(root),
        "pid": pid,
        "boot_id": boot,
        "process_start_ticks": start_ticks,
        "ready_at": time.time(),
    }
    destination = Path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("Ready path must not be a symlink")
    fd, name = tempfile.mkstemp(prefix=".ready-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, destination)
        parent = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return value


def verify_ready(
    path: Path, *, expected_sha: str, expected_pid: int, not_before: float
) -> bool:
    path = Path(path)
    if path.is_symlink():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        boot, ticks = process_identity(expected_pid)
        return (
            value.get("release_sha") == expected_sha
            and value.get("pid") == expected_pid
            and value.get("boot_id") == boot
            and value.get("process_start_ticks") == ticks
            and isinstance(value.get("ready_at"), (int, float))
            and value["ready_at"] >= not_before
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
