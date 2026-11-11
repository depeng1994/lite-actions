"""Atomic code version switch and systemd Worker lifecycle.

No worker ID/zone logic belongs in this module.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from auto_update.check_source import checkout_sha
from ci_core.worker_ready import verify_ready

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_UNIT = re.compile(r"[A-Za-z0-9_.@:-]+\.service\Z")
_BINDINGS = ("config", "state", "logs", "cache")
_EXTERNAL = (
    "/etc/lite-actions/",
    "/var/lib/lite-actions/",
    "/var/log/lite-actions/",
    "/var/cache/lite-actions/",
)


class SwitchError(RuntimeError):
    """Code switch failed; no shell output included."""


def validate_unit(name: str) -> None:
    if not _UNIT.fullmatch(name) or name.startswith("."):
        raise SwitchError("invalid_worker_unit")
    if name == "lite-actions-auto-update.service":
        raise SwitchError("updater_cannot_manage_itself")


def release_at(root: Path, sha: str) -> Path:
    if not _SHA.fullmatch(sha):
        raise SwitchError("invalid_release_sha")
    return root / "releases" / sha


def current_release(root: Path) -> tuple[Path, str]:
    current = root / "current"
    if not current.is_symlink():
        raise SwitchError("current_not_symlink")
    try:
        target = current.resolve(strict=True)
    except OSError as exc:
        raise SwitchError("current_broken") from exc
    sha = target.name
    release_dir = root / "releases"
    if (
        target.parent != release_dir.resolve(strict=True)
        or release_at(root, sha).is_symlink()
    ):
        raise SwitchError("current_outside_releases")
    if checkout_sha(target) != sha:
        raise SwitchError("current_sha_mismatch")
    return target, sha


def prepare_release(root: Path, stage: Path, sha: str, current: Path) -> Path:
    """Do not move task state or secrets into a Git commit."""
    target = release_at(root, sha)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        if checkout_sha(target) != sha:
            raise SwitchError("existing_release_corrupted")
        shutil.rmtree(stage, ignore_errors=True)
        return target
    if checkout_sha(stage) != sha:
        raise SwitchError("candidate_sha_mismatch")
    for name in _BINDINGS:
        link = current / name
        if not link.is_symlink():
            continue
        raw = os.readlink(link)
        if not os.path.isabs(raw):
            raise SwitchError("nonabsolute_external_binding")
        resolved = str(link.resolve(strict=True))
        if not any(resolved.startswith(prefix) for prefix in _EXTERNAL):
            raise SwitchError("unexpected_external_binding")
        dest = stage / name
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        os.symlink(resolved, dest)
    # A same-filesystem rename of an immutable, complete checkout.
    os.replace(stage, target)
    return target


def switch_current(root: Path, target: Path) -> None:
    target = release_at(root, target.name)
    if not target.is_dir() or checkout_sha(target) != target.name:
        raise SwitchError("invalid_switch_target")
    if not (root / "current").is_symlink():
        raise SwitchError("current_not_symlink")
    fd, name = tempfile.mkstemp(prefix=".current-", dir=root)
    os.close(fd)
    os.unlink(name)
    try:
        os.symlink(str(target), name)
        os.replace(name, root / "current")
        dfd = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def systemctl(action: str, unit: str, *, timeout: int = 60) -> str:
    validate_unit(unit)
    if action not in ("stop", "start", "is-active", "show"):
        raise SwitchError("invalid_systemctl_action")
    args = ["systemctl", action, unit]
    if action == "show":
        args = ["systemctl", "show", "-p", "MainPID", "--value", unit]
    try:
        result = subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwitchError("systemctl_" + action.replace("-", "_") + "_failed") from exc
    if result.returncode:
        raise SwitchError("systemctl_" + action.replace("-", "_") + "_failed")
    return result.stdout.strip()


def is_active(unit: str) -> bool:
    try:
        return systemctl("is-active", unit, timeout=10) == "active"
    except SwitchError:
        return False


def main_pid(unit: str) -> int:
    try:
        pid = int(systemctl("show", unit, timeout=10))
    except ValueError as exc:
        raise SwitchError("invalid_systemd_pid") from exc
    return pid


def stop_worker(unit: str, *, timeout: int = 920) -> None:
    systemctl("stop", unit, timeout=timeout)
    if is_active(unit):
        raise SwitchError("worker_still_active")


def start_worker(unit: str) -> None:
    systemctl("start", unit, timeout=45)


def wait_ready(
    unit: str,
    ready_path: Path,
    sha: str,
    *,
    started_at: float,
    switch_time: float,
    ready_timeout: int = 120,
    pid_timeout: int = 30,
) -> tuple[int, float, float]:
    """Return PID, switch-to-PID seconds, start-to-Ready seconds."""
    limit = time.monotonic() + ready_timeout
    pid = 0
    observed = 0.0
    while time.monotonic() < limit:
        if not is_active(unit):
            raise SwitchError("worker_not_active")
        current_pid = main_pid(unit)
        if current_pid > 0 and not pid:
            pid = current_pid
            observed = time.monotonic() - switch_time
            if observed > pid_timeout:
                raise SwitchError("worker_pid_slo_exceeded")
        if pid and verify_ready(
            ready_path, expected_sha=sha, expected_pid=pid, not_before=started_at
        ):
            return pid, observed, time.monotonic() - (switch_time + 0.0)
        if not pid and time.monotonic() - switch_time > pid_timeout:
            raise SwitchError("worker_pid_timeout")
        time.sleep(0.25)
    raise SwitchError("worker_ready_timeout")


def restore_old(
    root: Path, old: Path, unit: str, ready_path: Path, *, timeout: int = 120
) -> str:
    """Never attest recovery from ActiveState alone on a Ready-capable release."""
    if ready_path.is_symlink():
        raise SwitchError("unsafe_ready_symlink")
    active = is_active(unit)
    current = root / "current"
    already_old = active and current.is_symlink() and current.resolve() == old
    not_before = 0.0
    if not already_old:
        if active:
            stop_worker(unit)
        switch_current(root, old)
        ready_path.unlink(missing_ok=True)
        not_before = time.time()
        start_worker(unit)

    capable = (old / "ci_core" / "worker_ready.py").is_file()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = main_pid(unit) if is_active(unit) else 0
        if pid > 0:
            if capable and verify_ready(
                ready_path, expected_sha=old.name, expected_pid=pid, not_before=not_before
            ):
                return "ready_verified"
            if not capable and Path(f"/proc/{pid}/cwd").resolve() == old:
                return "legacy_process_active"
        time.sleep(0.25)
    raise SwitchError("rollback_worker_unavailable")
