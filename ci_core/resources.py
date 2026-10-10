"""Cross-process physical Ascend device leases on a shared dispatcher host.

Advisory locks coordinate *our* CI agents. They cannot block unrelated users.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterable, Iterator, Mapping


class ResourceBusy(RuntimeError):
    """One of the requested physical chips is already leased."""


class GlobalSlotBusy(ResourceBusy):
    """Another dispatcher process owns the Phase 1 global training slot."""


class AgentAlreadyRunning(RuntimeError):
    """A second unified Dispatcher process attempted to start."""


def agent_lock_path() -> Path:
    configured = os.environ.get("LITE_ACTIONS_AGENT_LOCK")
    path = (Path(configured).expanduser() if configured else
            Path("/tmp") / f"lite-actions-{os.getuid()}" /
            "phase1-agent.lock")
    if not path.is_absolute():
        raise ValueError("LITE_ACTIONS_AGENT_LOCK must be absolute")
    return path


def global_slot_path() -> Path:
    """One lock path shared by all Lite Actions worktrees for this OS user."""
    configured = os.environ.get("LITE_ACTIONS_GLOBAL_SLOT_LOCK")
    path = (Path(configured).expanduser() if configured else
            Path("/tmp") / f"lite-actions-{os.getuid()}" /
            "phase1-global-training.lock")
    if not path.is_absolute():
        raise ValueError("LITE_ACTIONS_GLOBAL_SLOT_LOCK must be absolute")
    return path


def resource_keys(resources: Iterable[Mapping]) -> tuple[tuple[str, int], ...]:
    keys = set()
    for node in resources:
        host = node["host_id"]
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", host):
            raise ValueError("invalid canonical host identifier")
        for device in node["devices"]:
            if isinstance(device, bool) or not isinstance(device, int) or not 0 <= device < 256:
                raise ValueError("invalid physical NPU chip ID")
            keys.add((host, device))
    return tuple(sorted(keys))


@contextmanager
def reserve(lock_root: Path, resources: Iterable[Mapping]) -> Iterator[tuple[tuple[str, int], ...]]:
    """All-or-nothing nonblocking lock, held until the context exits."""
    devices = resource_keys(resources)
    lock_root.mkdir(parents=True, exist_ok=True)
    held = []
    try:
        for host, device in devices:
            fd = (lock_root / f"{host}_chip-{device:03d}.lock").open("a+b")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                fd.close()
                raise ResourceBusy(f"{host}:chip-{device}") from exc
            held.append(fd)
        yield devices
    finally:
        for fd in reversed(held):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()


@contextmanager
def wait_reserve(lock_root: Path, resources: Iterable[Mapping], *,
                 timeout: float, interval: float = 2.0,
                 canceled: Callable[[], bool] | None = None
                 ) -> Iterator[tuple[tuple[str, int], ...]]:
    """Wait without holding partial resources; never preempt another process."""
    deadline = time.monotonic() + timeout
    while True:
        if canceled is not None and canceled():
            raise RuntimeError("job canceled while awaiting resources")
        try:
            with reserve(lock_root, resources) as keys:
                yield keys
                return
        except ResourceBusy:
            if time.monotonic() >= deadline:
                raise ResourceBusy("device lease wait timed out")
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


@contextmanager
def single_agent(lock_path: Path | None = None) -> Iterator[None]:
    """Fail fast if another unified Dispatcher process is already active."""
    path = lock_path or agent_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = path.open("a+b")
    acquired = False
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise AgentAlreadyRunning(
                f"another Lite Actions Dispatcher owns {path}") from exc
        yield
    finally:
        if acquired:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


@contextmanager
def wait_global_slot(lock_path: Path, *, timeout: float,
                     interval: float = 2.0,
                     canceled: Callable[[], bool] | None = None
                     ) -> Iterator[None]:
    """Cross-process Phase 1 serialization lock.

    Device flocks protect physical cards. This additional dispatcher-wide
    flock enforces the stronger Phase 1 invariant: at most one training Case
    may be inside allocation/execution, even if a duplicate agent process is
    started accidentally.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_path.open("a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            if canceled is not None and canceled():
                raise RuntimeError("job canceled while awaiting global training slot")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise GlobalSlotBusy("global training slot wait timed out") from exc
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
