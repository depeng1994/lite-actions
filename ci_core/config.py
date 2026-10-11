"""Validate trusted current pipeline and physical resource pool boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import re


def load_config(root: Path) -> dict:
    pools = json.loads((root / "config/pools.json").read_text())
    pipelines = json.loads((root / "config/pipelines.json").read_text())
    if not isinstance(pools, dict) or not isinstance(pipelines, dict):
        raise ValueError("CI config must be JSON objects")
    workflows, result = set(), {}
    for name, spec in pipelines.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise ValueError(f"invalid pipeline ID: {name!r}")
        workflow = spec["workflow"]
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_]+\.yml", workflow)
                or workflow in workflows):
            raise ValueError("invalid or duplicate workflow")
        workflows.add(workflow)
        pool_id = spec["pool"]
        if pool_id not in pools:
            raise ValueError("unknown pool")
        pool = pools[pool_id]
        if type(pool.get("reclaim_external_processes", False)) is not bool:
            raise ValueError("pool reclaim_external_processes must be boolean")
        hosts = {node["host_id"]: node for node in pool["nodes"]}
        if len(hosts) != len(pool["nodes"]):
            raise ValueError("duplicate physical host")
        for host in hosts.values():
            ids = host["device_ids"]
            if (not isinstance(ids, list) or len(set(ids)) != len(ids)
                    or any(type(x) is not int or x < 0 or x >= 256 for x in ids)):
                raise ValueError("invalid physical Device IDs")
            for group in host.get("allocation_groups", []):
                group_ids = group["devices"]
                if (not isinstance(group.get("verified"), bool)
                        or not isinstance(group_ids, list) or not group_ids
                        or len(group_ids) != len(set(group_ids))
                        or not set(group_ids) <= set(ids)):
                    raise ValueError("invalid verified allocation group")
        runner = spec["runner"]
        if runner["kind"] not in {"ssh", "local"}:
            raise ValueError("unknown trusted runner")
        has_static = "allocation" in spec
        has_dynamic = "resources" in spec
        if has_static == has_dynamic:
            raise ValueError("exactly one of allocation/resources required")
        if has_static:
            allocation = spec["allocation"]
            for item in allocation:
                if (item["host_id"] not in hosts or not item["devices"]
                        or len(set(item["devices"])) != len(item["devices"])
                        or not set(item["devices"]) <= set(hosts[item["host_id"]]["device_ids"])):
                    raise ValueError("invalid static allocation")
        else:
            resources = spec["resources"]
            if set(resources) != {"nodes", "devices_per_node"}:
                raise ValueError("unexpected resource specification")
            nodes, per_node = resources["nodes"], resources["devices_per_node"]
            if (type(nodes) is not int or type(per_node) is not int
                    or not 1 <= nodes <= 16 or not 1 <= per_node <= 256):
                raise ValueError("invalid resource request")
            if spec["enabled"] and runner["kind"] == "ssh":
                from ci_core.scheduler import candidates
                if not candidates(spec, pools):
                    raise ValueError("enabled pool has no verified topology")
        if runner["kind"] == "ssh":
            if set(runner) != {"kind"}:
                raise ValueError("SSH pipeline must be generic")
            if has_static and not spec["allocation"]:
                raise ValueError("SSH must allocate devices")
            for host in hosts.values():
                if not host.get("ssh"):
                    raise ValueError("missing SSH address")
            if spec["enabled"]:
                active_hosts = (hosts.values() if has_dynamic else
                                (hosts[x["host_id"]] for x in spec["allocation"]))
                if any(not host.get("node_ip") for host in active_hosts):
                    raise ValueError("enabled pool requires HCCL node_ip")
        if type(spec["timeout_seconds"]) is not int or spec["timeout_seconds"] < 1:
            raise ValueError("invalid timeout")
        result[name] = {**spec, "name": name}
    return result
