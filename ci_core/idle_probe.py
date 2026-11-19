"""Read-only NPU occupancy census, independent of the scheduler."""

import logging
import threading
import time

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


class IdleProbe:
    def __init__(self, worker, view, *, interval=120):
        self.worker = worker
        self.view = view
        self.interval = interval
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="npu-idle-probe", daemon=True
        )
        self.sample = ({}, None, None)

    def cycle(self):
        pools, records = self.view()
        states, complete_time = observe(self.worker, pools, records)
        self.sample = (states, complete_time, pools)

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
