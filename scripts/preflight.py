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

IMAGE = "cann_master_torchtitan_master_xyj"
INSPECT = """docker inspect --format '{"Id":{{json .Id}},"Image":{{json .Image}},"ImageRef":{{json .Config.Image}},"State":{"Running":{{json .State.Running}}}}' """


def probe_node(node, image, *, container_ref=None):
    host = node["ssh"]
    identity = run(
        host,
        "python3 -c 'import pathlib,hashlib; print(hashlib.sha256(pathlib.Path(\"/etc/machine-id\").read_bytes()).hexdigest())'",
        timeout=30,
    ).stdout.strip()
    if node.get("machine_fingerprint") and node["machine_fingerprint"] != identity:
        raise ValueError("PHYSICAL_NODE_IDENTITY_CHANGED")
    configured = node.get("environments", {}).get(image)
    selected = container_ref or (configured or {}).get("container_id")
    image_id = None
    if selected:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", selected):
            raise ValueError("invalid configured container")
        ids = [selected]
    else:
        image_id = run(
            host,
            "docker image inspect --format '{{.Id}}' " + shlex.quote(image),
            timeout=45,
        ).stdout.strip()
        ids = run(host, "docker ps -q --no-trunc", timeout=45).stdout.split()
    matches = []
    for ident in ids:
        data = json.loads(run(host, INSPECT + shlex.quote(ident), timeout=30).stdout)
        if (image_id is None or data.get("Image") == image_id) and data.get(
            "State", {}
        ).get("Running"):
            matches.append(data)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one explicitly selected running container for {image}; found {len(matches)}"
        )
    binding = dict(
        container_id=matches[0]["Id"],
        image_id=matches[0]["Image"],
        image_ref=(
            matches[0]["ImageRef"]
            if container_ref
            else (configured or {}).get("image_ref", image)
        ),
    )
    if (
        configured
        and not container_ref
        and any(binding.get(key) != value for key, value in configured.items())
    ):
        raise ValueError("configured image/container identity changed")
    checked = {**node, "environments": {**node.get("environments", {}), image: binding}}
    execution_node(checked, image)
    return dict(machine_fingerprint=identity, binding=binding)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True)
    p.add_argument(
        "--image",
        default=IMAGE,
        help="Workflow docker environment key; explicit bindings may use different per-node image references",
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
    a = p.parse_args()
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
                    node, a.image, container_ref=selections.get(node["host_id"])
                )
                print(
                    json.dumps(
                        dict(pool=name, node=node["host_id"], status="PASS", **result)
                    )
                )
                if a.write_bindings:
                    node["machine_fingerprint"] = result["machine_fingerprint"]
                    node.setdefault("environments", {})[a.image] = result["binding"]
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
