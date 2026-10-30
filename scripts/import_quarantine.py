#!/usr/bin/env python3
"""Preserve legacy quarantine under canonical IDs; never clear live hardware leases."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_core.config import load
from ci_core.job_state import JobState


def guards(legacy_root, target_root, worker):
    _, pools = load(target_root, worker)
    nodes = [n for p in pools.values() if p["owner"] == worker for n in p["nodes"]]
    by_ip = {n["node_ip"]: n["host_id"] for n in nodes if n.get("node_ip")}
    old_pools = json.loads((legacy_root / "config/pools.json").read_text())
    mapping = {}
    for pool in old_pools.values():
        for node in pool["nodes"]:
            if node.get("node_ip") in by_ip:
                mapping[node["host_id"]] = by_ip[node["node_ip"]]
    path = legacy_root / "state/unified-jobs.json"
    records = json.loads(path.read_text()) if path.exists() else {}
    if any(r.get("phase") not in {"reported"} for r in records.values()):
        raise ValueError("drain/cancel and report every legacy task before migration")
    result = {}
    for key, r in records.items():
        if not r.get("quarantined"):
            continue
        if r.get("allocation"):
            allocation = []
            for item in r["allocation"]:
                if item["host_id"] not in mapping:
                    raise ValueError("cannot prove canonical quarantine mapping")
                allocation.append(
                    dict(host_id=mapping[item["host_id"]], devices=item["devices"])
                )
        else:
            allocation = [
                dict(host_id=n["host_id"], devices=n["device_ids"]) for n in nodes
            ]
        if not allocation:
            raise ValueError(
                "unscoped quarantine without a configured resource inventory"
            )
        ident = "migration_guard_" + hashlib.sha256(key.encode()).hexdigest()
        result[ident] = dict(
            phase="REPORTED",
            quarantined=True,
            allocation=allocation,
            legacy_job_key=key,
            reason="Legacy quarantine preserved; requires remote evidence before reconciliation",
        )
    return result


def legacy_running(root):
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if proc.stat().st_uid != os.geteuid():
                continue
            if (proc / "cwd").resolve() != root.resolve():
                continue
            if any(
                arg.endswith(b"unified_agent.py")
                for arg in (proc / "cmdline").read_bytes().split(b"\x00")
            ):
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--legacy-root", required=True, type=Path)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    target = Path(__file__).resolve().parents[1]
    items = guards(a.legacy_root, target, a.worker_id)
    if a.apply:
        if legacy_running(a.legacy_root):
            raise ValueError("stop legacy dispatcher before applying state migration")
        state = JobState(target)
        for key, value in items.items():
            existing = state.read().get(key)
            if existing and existing != value:
                raise ValueError("conflicting existing migration guard")
            state.update(key, **value)
        source = a.legacy_root / "state/unified-jobs.json"
        if source.exists():
            backup = state.path.parent / "migration-backups"
            backup.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            (backup / (hashlib.sha256(content).hexdigest() + ".json")).write_bytes(
                content
            )
    print(
        json.dumps(
            dict(
                mode="apply" if a.apply else "dry-run",
                guards=len(items),
                allocations=[r["allocation"] for r in items.values()],
            )
        )
    )


if __name__ == "__main__":
    main()
