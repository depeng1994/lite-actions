"""Validate trusted CI resource and pipeline definitions before dispatch."""
from __future__ import annotations
import json
from pathlib import Path
import re

def load_config(root: Path) -> dict:
    pools = json.loads((root / "config" / "pools.json").read_text())
    pipelines = json.loads((root / "config" / "pipelines.json").read_text())
    if not isinstance(pools, dict) or not isinstance(pipelines, dict):
        raise ValueError("CI config must be JSON objects")
    used_workflows = set()
    resolved = {}
    for name, spec in pipelines.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise ValueError(f"invalid pipeline ID: {name!r}")
        workflow = spec["workflow"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]+\.yml", workflow) or workflow in used_workflows:
            raise ValueError(f"invalid/duplicate workflow: {workflow!r}")
        used_workflows.add(workflow)
        pool = spec.get("pool")
        allocation = spec.get("allocation", [])
        if pool not in pools:
            raise ValueError(f"unknown resource pool: {pool}")
        hosts = {node["host_id"]: node for node in pools[pool]["nodes"]}
        for node in allocation:
            host_id = node["host_id"]
            if host_id not in hosts:
                raise ValueError(f"allocation for unknown host: {host_id}")
            permitted = set(hosts[host_id]["device_ids"])
            devices = node["devices"]
            if not devices or len(set(devices)) != len(devices) or not set(devices) <= permitted:
                raise ValueError(f"invalid device allocation for host: {host_id}")
        kind = spec["runner"]["kind"]
        if kind not in ("local", "ssh"):
            raise ValueError(f"unknown trusted runner: {kind}")
        if kind == "ssh":
            runner = spec["runner"]
            if not allocation:
                raise ValueError("SSH execution must allocate devices")
            # Stable dispatcher: no model/toolkit/asset paths or case-specific
            # environment overrides may be configured in this repository.
            if set(runner) != {"kind"}:
                raise ValueError("SSH pipeline runner must be generic (kind only)")
            for node in allocation:
                host = hosts[node["host_id"]]
                if not isinstance(host.get("ssh"), str) or not host["ssh"]:
                    raise ValueError("SSH host address required")
                addr = host.get("node_ip")
                if spec.get("enabled") and (not isinstance(addr, str) or not addr):
                    raise ValueError("enabled SSH pool requires verified HCCL node_ip")
        if not isinstance(spec["timeout_seconds"], int) or spec["timeout_seconds"] < 1:
            raise ValueError("invalid timeout")
        resolved[name] = {**spec, "name": name}
    return resolved
