"""Read-only NPU occupancy census, independent of the scheduler."""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from ci_core.executor import _process_rows
from ci_core.resources import resource_keys
from ci_core.transport import run


def group_id(pool, host, devices):
    return pool, host, tuple(sorted(devices))


def observe(worker, pools, records, *, execute=run):
    leased = set()
    for record in records.values():
        if record.get("phase") in {"RESERVED", "STAGING", "RUNNING", "CLEANING"}:
            leased.update(resource_keys(record.get("allocation") or []))
    results = {}
    skipped = False
    for pool_name, pool in pools.items():
        if pool.get("owner") != worker or not pool.get("enabled"):
            continue
        for node in pool["nodes"]:
            host = node["host_id"]
            available = []
            for group in node.get("allocation_groups", []):
                if leased.intersection(
                    resource_keys([{"host_id": host, "devices": group["devices"]}])
                ):
                    skipped = True
                else:
                    available.append(group["devices"])
            if not available:
                continue
            all_devices = sorted({device for group in available for device in group})
            try:
                output = execute(
                    node.get("host_ssh") or node["ssh"], "npu-smi info", timeout=20
                ).stdout
                processes = _process_rows(
                    output,
                    all_devices,
                    indexing=pool["process_indexing"],
                    require_exclusive=False,
                    require_end=True,
                    require_coverage=True,
                )
                occupied = {chip for chip, _, _ in processes}
            except Exception:
                occupied = None
            now = int(time.time())
            for devices in available:
                state = (
                    "probe_error"
                    if occupied is None
                    else "busy"
                    if occupied.intersection(devices)
                    else "idle"
                )
                results[group_id(pool_name, host, devices)] = (state, now)
    return results, None if skipped else int(time.time())


def _health_key(pool, host, devices):
    return json.dumps([pool, host, sorted(devices)], separators=(",", ":"))


def observe_health(
    worker, pools, records, *, execute=run, previous=None, probe_round=1
):
    """Probe every configured node and return occupancy plus visibility state.

    Visibility is checked for leased and paused groups as well. Occupancy is
    still reported only for groups that are not held by a Lite Actions lease.
    The caller supplies the previous health map so a failed round can retain
    ``last_online_at`` and advance the five-round failure counter exactly once.
    """
    previous = previous or {}
    leased = set()
    for record in records.values():
        if record.get("phase") in {"RESERVED", "STAGING", "RUNNING", "CLEANING"}:
            leased.update(resource_keys(record.get("allocation") or []))
    results = {}
    health = {}
    complete = True
    now = int(time.time())
    for pool_name, pool in pools.items():
        if pool.get("owner") != worker:
            continue
        for node in pool["nodes"]:
            host = node["host_id"]
            groups = [group["devices"] for group in node.get("allocation_groups", [])]
            try:
                output = execute(
                    node.get("host_ssh") or node["ssh"], "npu-smi info", timeout=20
                ).stdout
            except Exception:
                output = None
                complete = False
            for devices in groups:
                key = _health_key(pool_name, host, devices)
                old = previous.get(key, {})
                if output is None:
                    occupied = None
                    visible = False
                else:
                    try:
                        processes = _process_rows(
                            output,
                            devices,
                            indexing=pool["process_indexing"],
                            require_exclusive=False,
                            require_end=True,
                            require_coverage=True,
                        )
                        occupied = {chip for chip, _, _ in processes}
                        visible = True
                    except Exception:
                        occupied = None
                        visible = False
                        complete = False
                failures = 0 if visible else min(int(old.get("mc", 0)) + 1, 5)
                visibility = 0 if visible else (2 if failures >= 5 else 1)
                last_online = now if visible else old.get("lo")
                health[key] = {
                    "v": visibility,
                    "mc": failures,
                    "lo": last_online,
                    "pr": probe_round,
                }
                resources = resource_keys([{"host_id": host, "devices": devices}])
                if set(resources).intersection(leased):
                    continue
                state = (
                    "probe_error"
                    if occupied is None
                    else "busy"
                    if occupied.intersection(devices)
                    else "idle"
                )
                results[group_id(pool_name, host, devices)] = (state, now)
    return results, (now if complete else None), health


class IdleProbe:
    def __init__(self, worker, view, *, interval=120, state_dir=None):
        self.worker = worker
        self.view = view
        self.interval = interval
        self.state_path = (
            Path(state_dir) / "visibility-probe.json" if state_dir else None
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="npu-idle-probe", daemon=True
        )
        self.sample = ({}, None, None)
        self.health = {}
        self.probe_round = 0
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.probe_round = int(saved.get("round", 0))
                self.health = saved.get("groups", {})
            except (OSError, ValueError, TypeError):
                self.probe_round, self.health = 0, {}

    def cycle(self):
        pools, records = self.view()
        self.probe_round += 1
        states, complete_time, health = observe_health(
            self.worker,
            pools,
            records,
            previous=self.health,
            probe_round=self.probe_round,
        )
        self.sample = (states, complete_time, pools)
        self.health = health
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.state_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(
                        {"round": self.probe_round, "groups": self.health},
                        file,
                        separators=(",", ":"),
                    )
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(name, self.state_path)
            finally:
                Path(name).unlink(missing_ok=True)

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                logging.warning("idle probe deferred (%s)", type(exc).__name__)
            self.stop.wait(self.interval)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
