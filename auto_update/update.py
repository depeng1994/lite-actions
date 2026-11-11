"""Trusted, pull-only, transactional update runner for one configured Worker unit."""

from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from auto_update.check_source import SourceError, check_source, stage_source, validate_source
from auto_update.update_log import record
from auto_update.version_switch import (
    SwitchError,
    current_release,
    is_active,
    main_pid,
    prepare_release,
    release_at,
    restore_old,
    start_worker,
    stop_worker,
    switch_current,
    validate_unit,
    wait_ready,
)

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_TERMINAL = {
    "COMMITTED", "ROLLED_BACK", "RECOVERY_REQUIRED", "FAILED_PREPARE"
}


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    root: Path
    source: str
    ref: str
    unit: str
    state_dir: Path
    ready_file: Path
    log_file: Path
    lock_file: Path
    ready_timeout: int

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        root = Path(e.get("LITE_ACTIONS_UPDATE_ROOT", "/opt/lite-actions"))
        data = cls(
            root=root,
            source=e.get(
                "LITE_ACTIONS_UPDATE_GIT_URL",
                "https://github.com/depeng1994/lite-actions.git",
            ),
            ref=e.get("LITE_ACTIONS_UPDATE_GIT_REF", "refs/heads/main"),
            unit=e.get("LITE_ACTIONS_UPDATE_WORKER_UNIT", ""),
            state_dir=Path(e.get(
                "LITE_ACTIONS_UPDATE_STATE_DIR", "/var/lib/lite-actions/auto-update"
            )),
            ready_file=Path(e.get(
                "LITE_ACTIONS_UPDATE_READY_FILE",
                "/var/lib/lite-actions/worker-ready.json",
            )),
            log_file=Path(e.get(
                "LITE_ACTIONS_UPDATE_LOG_FILE",
                "/var/log/lite-actions/management/updates.jsonl",
            )),
            lock_file=Path(e.get(
                "LITE_ACTIONS_UPDATE_LOCK_FILE",
                "/run/lock/lite-actions-auto-update.lock",
            )),
            ready_timeout=int(e.get("LITE_ACTIONS_UPDATE_READY_TIMEOUT", "120")),
        )
        for p in (data.root, data.state_dir, data.ready_file, data.log_file, data.lock_file):
            if not p.is_absolute() or ".." in p.parts:
                raise UpdateError("invalid_absolute_path")
        if not data.root.is_dir() or data.root.is_symlink():
            raise UpdateError("invalid_install_root")
        if data.ready_timeout < 1 or data.ready_timeout > 600:
            raise UpdateError("invalid_ready_timeout")
        validate_unit(data.unit)
        validate_source(data.source, data.ref)
        return data


@contextmanager
def exclusive(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UpdateError("invalid_update_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("update_already_running") from exc
        yield
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise UpdateError("state_path_is_symlink")
    fd, temp = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        parent_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise UpdateError("state_path_is_symlink")
    try:
        value = json.loads(path.read_text())
        if type(value) is not dict:
            raise ValueError()
        return value
    except (ValueError, OSError) as exc:
        raise UpdateError("transaction_corrupt") from exc


def _transition(settings: Settings, info: dict, phase: str) -> None:
    info["phase"] = phase
    info["last_transition"] = time.time()
    _atomic_json(settings.state_dir / "transaction.json", info)


def _audit(settings: Settings, event: str, **fields: object) -> None:
    record(settings.log_file, event, **fields)


def _validate_candidate(path: Path) -> None:
    """Pre-stop syntax and file checks; do not execute arbitrary candidate scripts."""
    required = (
        "unified_router.py",
        "ci_core/config.py",
        "ci_core/router.py",
        "ci_core/worker_ready.py",
        "auto_update/update.py",
        "auto_update/version_switch.py",
    )
    for rel in required:
        file = path / rel
        if file.is_symlink() or not file.is_file():
            raise UpdateError("candidate_missing_required_file")
        try:
            ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        except (UnicodeError, SyntaxError, OSError) as exc:
            raise UpdateError("candidate_syntax_error") from exc
    if (path / "ci_core").is_symlink():
        raise UpdateError("candidate_symlinked_code")


def _blocked(settings: Settings) -> str | None:
    entry = _read_json(settings.state_dir / "blocked.json")
    sha = entry.get("sha") if entry else None
    if sha is not None and not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        raise UpdateError("blocked_state_corrupt")
    return sha


def _block(settings: Settings, sha: str, reason: str) -> None:
    _atomic_json(settings.state_dir / "blocked.json", {
        "sha": sha,
        "error_code": reason,
        "timestamp": time.time(),
    })


def _recover(settings: Settings) -> bool:
    """Recover exactly one interrupted nonterminal transaction, never retry it now."""
    state = _read_json(settings.state_dir / "transaction.json")
    if not state or state.get("phase") in _TERMINAL:
        return False
    old_sha = state.get("old_sha")
    target_sha = state.get("target_sha")
    if not all(isinstance(x, str) and _SHA.fullmatch(x) for x in (old_sha, target_sha)):
        raise UpdateError("transaction_sha_invalid")
    old = release_at(settings.root, old_sha)
    if not old.is_dir() or old.is_symlink():
        raise UpdateError("recovery_old_release_missing")
    phase = state.get("phase")
    try:
        if phase in {"FETCHING", "PREPARING", "VALIDATING", "PREPARED"}:
            # No stop has been attempted in these phases.
            current, sha = current_release(settings.root)
            if sha != old_sha or not is_active(settings.unit):
                raise UpdateError("recovery_pre_stop_state_unexpected")
            _transition(settings, state, "FAILED_PREPARE")
            _audit(settings, "RECOVERED_PREPARE", old_sha=sha, target_sha=target_sha)
            return True
        # Stop was begun or the link may already have changed.
        restore_old(settings.root, old, settings.unit, settings.ready_file)
        _block(settings, target_sha, "interrupted_transaction")
        _transition(settings, state, "ROLLED_BACK")
        _audit(settings, "RECOVERED_ROLLBACK", old_sha=old_sha, target_sha=target_sha)
        return True
    except (UpdateError, SourceError, SwitchError, OSError) as exc:
        _transition(settings, state, "RECOVERY_REQUIRED")
        _audit(settings, "RECOVERY_REQUIRED", error_code=type(exc).__name__)
        raise UpdateError("manual_recovery_required") from exc


def _check(settings: Settings) -> int:
    old_path, old_sha = current_release(settings.root)
    target = check_source(settings.source, settings.ref)
    event = "NO_CHANGE" if target == old_sha else "UPDATE_AVAILABLE"
    print(json.dumps({
        "status": event,
        "old_sha": old_sha,
        "target_sha": target,
        "current": str(old_path),
    }, sort_keys=True))
    return 0


def _run(settings: Settings) -> int:
    if _recover(settings):
        return 0
    old_path, old_sha = current_release(settings.root)
    time_start = time.monotonic()
    target_sha = check_source(settings.source, settings.ref)
    check_duration = int((time.monotonic() - time_start) * 1000)
    if target_sha == old_sha:
        _audit(settings, "NO_CHANGE", old_sha=old_sha,
               target_sha=target_sha, check_duration_ms=check_duration)
        return 0
    if _blocked(settings) == target_sha:
        _audit(settings, "SKIPPED_FAILED_SHA", old_sha=old_sha, target_sha=target_sha)
        return 0

    txn = {
        "old_sha": old_sha,
        "target_sha": target_sha,
        "unit": settings.unit,
        "created_at": time.time(),
    }
    stop_started = False
    stage = None
    begin_download = time.monotonic()
    times = {"check_duration_ms": check_duration}
    try:
        _transition(settings, txn, "FETCHING")
        stage = stage_source(settings.root, settings.source, settings.ref, target_sha, old_sha)
        times["download_duration_ms"] = int((time.monotonic() - begin_download) * 1000)
        _transition(settings, txn, "PREPARING")
        _validate_candidate(stage)
        target_path = prepare_release(settings.root, stage, target_sha, old_path)
        stage = None
        _transition(settings, txn, "PREPARED")
        # TODO(auto-update): add scheduler quiesce/drain/resume protocol.
        # V1 deliberately does not skip an update because jobs are running.
        # Existing Worker SIGTERM/Router.close() cleanup and quarantine apply.
        stop_started = True
        _transition(settings, txn, "STOPPING")
        start_stop = time.monotonic()
        stop_worker(settings.unit)
        times["stop_duration_ms"] = int((time.monotonic() - start_stop) * 1000)
        _transition(settings, txn, "SWITCHING")
        switch_current(settings.root, target_path)
        switch_time = time.monotonic()
        _transition(settings, txn, "STARTING")
        if settings.ready_file.is_symlink():
            raise UpdateError("unsafe_ready_link")
        settings.ready_file.unlink(missing_ok=True)
        start_mono = time.monotonic()
        times["switch_to_start_ms"] = int((start_mono - switch_time) * 1000)
        start_wall = time.time()
        if times["switch_to_start_ms"] > 5000:
            raise UpdateError("worker_start_request_slo_exceeded")
        start_worker(settings.unit)
        _transition(settings, txn, "VERIFYING")
        pid, switch_to_pid, switch_to_ready = wait_ready(
            settings.unit,
            settings.ready_file,
            target_sha,
            started_at=start_wall,
            switch_time=switch_time,
            ready_timeout=settings.ready_timeout,
        )
        times["switch_to_pid_ms"] = int(switch_to_pid * 1000)
        times["start_to_ready_ms"] = int((switch_to_ready - (start_mono - switch_time)) * 1000)
        if times["start_to_ready_ms"] > settings.ready_timeout * 1000:
            raise UpdateError("worker_ready_slo_exceeded")
        _transition(settings, txn, "COMMITTED")
        _audit(
            settings, "UPDATED", old_sha=old_sha, target_sha=target_sha,
            worker_pid=pid, **times
        )
        return 0
    except (UpdateError, SourceError, SwitchError, OSError, ValueError) as exc:
        reason = type(exc).__name__
        if stage is not None and stage.is_dir():
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
        if not stop_started:
            _transition(settings, txn, "FAILED_PREPARE")
            _audit(settings, "FAILED_PREPARE", old_sha=old_sha,
                   target_sha=target_sha, error_code=reason)
            return 2
        try:
            _transition(settings, txn, "ROLLING_BACK")
            status = restore_old(settings.root, old_path, settings.unit, settings.ready_file)
            _block(settings, target_sha, reason)
            _transition(settings, txn, "ROLLED_BACK")
            _audit(settings, "ROLLED_BACK", old_sha=old_sha,
                   target_sha=target_sha, recovery=status, error_code=reason)
            return 3
        except (UpdateError, SourceError, SwitchError, OSError, ValueError) as rollback_exc:
            _transition(settings, txn, "RECOVERY_REQUIRED")
            _audit(settings, "RECOVERY_REQUIRED", old_sha=old_sha,
                   target_sha=target_sha, error_code=type(rollback_exc).__name__)
            return 4


def main() -> int:
    parser = argparse.ArgumentParser(description="Update one configured Worker unit")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--recover", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        with exclusive(settings.lock_file):
            if args.check:
                return _check(settings)
            if args.recover:
                return 0 if _recover(settings) else 0
            return _run(settings)
    except (UpdateError, SourceError, SwitchError, OSError, ValueError) as exc:
        # Only safe error categories, not stderr/credentials.
        print("auto-update: " + type(exc).__name__, file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
