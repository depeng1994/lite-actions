#!/usr/bin/env python3
"""Verify trusted deployment bindings; optionally persist an explicit selection."""

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_core.config import execution_node, format_pools, load
from ci_core.transport import run

ENVIRONMENT = "cann_master_torchtitan_master_xyj"
INSPECT = """docker inspect --format '{"Id":{{json .Id}},"Image":{{json .Image}},"ImageRef":{{json .Config.Image}},"State":{"Running":{{json .State.Running}}}}' """


def probe_machine(node):
    identity = run(
        node["ssh"],
        "python3 -c 'import pathlib,hashlib; print(hashlib.sha256(pathlib.Path(\"/etc/machine-id\").read_bytes()).hexdigest())'",
        timeout=30,
    ).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{64}", identity) or (
        node.get("machine_fingerprint") and node["machine_fingerprint"] != identity
    ):
        raise ValueError("PHYSICAL_NODE_IDENTITY_CHANGED")
    return identity


def probe_node(node, environment, *, container_ref=None):
    host = node["ssh"]
    identity = probe_machine(node)
    configured = node.get("environments", {}).get(environment)
    selected = container_ref or (configured or {}).get("container_id") or environment
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", selected):
        raise ValueError("select an existing container name or ID")
    data = json.loads(run(host, INSPECT + shlex.quote(selected), timeout=30).stdout)
    if not data.get("State", {}).get("Running"):
        raise ValueError("selected container is not running")
    binding = dict(
        container_id=data["Id"],
        image_id=data["Image"],
        image_ref=data["ImageRef"],
    )
    if (
        configured
        and not container_ref
        and any(binding.get(key) != value for key, value in configured.items())
    ):
        raise ValueError("configured image/container identity changed")
    checked = {
        **node,
        "environments": {**node.get("environments", {}), environment: binding},
    }
    execution_node(checked, environment)
    return dict(machine_fingerprint=identity, binding=binding)


def device_list(value):
    result = []
    for group in value.split(","):
        if not re.fullmatch(r"[0-9]{1,3}(?:-[0-9]{1,3})?", group):
            raise ValueError("invalid device list or range")
        first, _, last = group.partition("-")
        lo, hi = int(first), int(last or first)
        if not 0 <= lo <= hi < 256:
            raise ValueError("invalid device index")
        result.extend(range(lo, hi + 1))
    if len(result) != len(set(result)):
        raise ValueError("duplicate physical device")
    return sorted(result)


def node_snippet(args):
    import ipaddress

    host, _, port = (args.host_id or "").partition(":")
    if (
        str(ipaddress.IPv4Address(host)) != host
        or not re.fullmatch(r"[1-9][0-9]{0,4}", port)
        or int(port) > 65535
    ):
        raise ValueError("invalid canonical host ID")
    ipaddress.IPv4Address(args.node_ip)
    if not args.nic or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", args.nic):
        raise ValueError("invalid network interface")
    if not args.devices or not args.groups:
        raise ValueError("--devices and --groups are required")
    devices = device_list(args.devices)
    groups = [device_list(group) for group in args.groups.split(";")]
    if len(set(map(tuple, groups))) != len(groups) or any(
        not set(group).issubset(devices) for group in groups
    ):
        raise ValueError("invalid device groups")
    node = {
        "host_id": args.host_id,
        "ssh": args.ssh or args.host_id,
        "host_ssh": args.host_ssh or args.ssh or args.host_id,
        "broadcast_ssh": [args.host_ssh or args.ssh or args.host_id],
        "node_ip": args.node_ip,
        "network_interface": args.nic,
        "device_ids": devices,
        "allocation_groups": [{"devices": group, "verified": True} for group in groups],
        "capabilities": ["train", "npu_smoke"],
        "occupancy_policy": "wait",
    }
    result = probe_node(node, args.environment, container_ref=args.container)
    if not re.fullmatch(r"[a-f0-9]{64}", result["machine_fingerprint"]):
        raise ValueError("invalid machine fingerprint")
    node.update(
        machine_fingerprint=result["machine_fingerprint"],
        environments={args.environment: result["binding"]},
    )
    return node


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True)
    p.add_argument(
        "--environment",
        default=ENVIRONMENT,
        help="Workflow docker environment key; defaults to the same existing container name",
    )
    p.add_argument(
        "--node", help="Canonical node ID; omit to verify all registered owned nodes"
    )
    p.add_argument(
        "--bindings-file",
        type=Path,
        help="Trusted JSON mapping canonical node IDs to existing container names/IDs",
    )
    p.add_argument("--write-bindings", action="store_true")
    p.add_argument("--export-node-snippet", action="store_true")
    p.add_argument("--host-id")
    p.add_argument("--ssh")
    p.add_argument("--host-ssh")
    p.add_argument("--node-ip")
    p.add_argument("--nic")
    p.add_argument("--devices", help="Comma-separated device IDs or ranges")
    p.add_argument("--groups", help="Semicolon-separated device groups")
    p.add_argument("--container")
    a = p.parse_args()
    if a.export_node_snippet:
        if a.write_bindings or a.node or a.bindings_file:
            p.error(
                "--export-node-snippet cannot be combined with registered node operations"
            )
        print(json.dumps(node_snippet(a), ensure_ascii=False))
        return 0
    root = Path(__file__).resolve().parents[1]
    _, pools = load(root, a.worker_id)
    selections = json.loads(a.bindings_file.read_text()) if a.bindings_file else {}
    if not isinstance(selections, dict):
        raise ValueError("bindings must be a node-to-container object")
    owned = {
        n["host_id"]
        for pool in pools.values()
        if pool["owner"] == a.worker_id
        for n in pool["nodes"]
    }
    if set(selections) - owned or any(
        not isinstance(v, str) or not v for v in selections.values()
    ):
        raise ValueError(
            "bindings must select registered owned nodes and nonempty container names/IDs"
        )
    failed = False
    changed = False
    found = False
    for name, pool in pools.items():
        if pool["owner"] != a.worker_id:
            continue
        for node in pool["nodes"]:
            if a.node and a.node != node["host_id"]:
                continue
            found = True
            try:
                result = probe_node(
                    node, a.environment, container_ref=selections.get(node["host_id"])
                )
                print(
                    json.dumps(
                        dict(pool=name, node=node["host_id"], status="PASS", **result)
                    )
                )
                if a.write_bindings:
                    node["machine_fingerprint"] = result["machine_fingerprint"]
                    node.setdefault("environments", {})[a.environment] = result[
                        "binding"
                    ]
                    changed = True
            except Exception as exc:
                failed = True
                print(
                    json.dumps(
                        dict(
                            pool=name,
                            node=node["host_id"],
                            status="INFRA_ERROR",
                            reason=str(exc),
                        )
                    )
                )
    if a.node and not found:
        raise ValueError("node is not registered and owned by this worker")
    if a.write_bindings and changed and not failed:
        path = root / "config/pools.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(format_pools(pools))
        temp.replace(path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
