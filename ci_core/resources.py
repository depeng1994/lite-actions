"""Cross-process physical Ascend device leases on a shared dispatcher host.

Advisory locks coordinate *our* CI agents. They cannot block unrelated users.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
import time
from typing import Callable, Iterable, Iterator, Mapping


class ResourceBusy(RuntimeError):
    """One of the requested physical chips is already leased."""


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
