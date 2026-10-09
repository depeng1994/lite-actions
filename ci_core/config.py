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
            if runner.get("mode") not in ("single", "distributed"):
                raise ValueError("invalid SSH test mode")
            module = runner.get("module", "")
            if not re.fullmatch(r"tests\.integration_tests\.[a-z][a-z0-9_]*", module):
                raise ValueError("untrusted model test module")
            env = runner.get("env", {})
            reserved = {"NODE_IPS", "NGPU", "NNODES", "ASCEND_RT_VISIBLE_DEVICES",
                        "PYTHONUNBUFFERED", "LITE_CI_JOB_ID", "HF_ASSETS_PATH",
                        "CKPT_INIT_LOAD_PATH", "CKPT_SAVE_LOAD_PATH", "Network_Interface"}
            if not isinstance(env, dict) or any(
                key in reserved or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or not isinstance(value, str) or len(value) > 160
                or any(c in value for c in "\r\n\x00")
                for key, value in env.items()
            ):
                raise ValueError("invalid fixed runner environment")
            # Every SSH address, node IP and process mapper is explicitly trusted locally.
            if pools[pool].get("process_indexing") not in ("card", "card-die"):
                raise ValueError("unknown physical NPU process mapping")
            for host in hosts.values():
                if not host.get("ssh") or not host.get("node_ip"):
                    raise ValueError("missing SSH / HCCL host address")
            if spec.get("enabled"):
                for allocation_node in allocation:
                    host = hosts[allocation_node["host_id"]]
                    if runner.get("requires_assets") and not host.get("hf_assets_path"):
                        raise ValueError("missing HF assets path for enabled pipeline")
                    if runner.get("requires_checkpoint") and not host.get("ckpt_init_path"):
                        raise ValueError("missing initial checkpoint path for enabled pipeline")
        if not isinstance(spec["timeout_seconds"], int) or spec["timeout_seconds"] < 1:
            raise ValueError("invalid timeout")
        resolved[name] = {**spec, "name": name}
    return resolved
