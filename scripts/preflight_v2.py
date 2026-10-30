#!/usr/bin/env python3
"""Read-only deployment verification; optionally persist verified candidate bindings."""

import argparse
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_core.v2_config import load
from ci_core.transport import run

IMAGE = "dsv4.1_train_torchtitan_a3:cann_master_newest_0916"


def probe_node(node, image):
    host = node["ssh"]
    identity = run(
        host,
        "python3 -c 'import pathlib,hashlib; print(hashlib.sha256(pathlib.Path(\"/etc/machine-id\").read_bytes()).hexdigest())'",
        timeout=30,
    ).stdout.strip()
    image_id = run(
        host,
        "docker image inspect --format '{{.Id}}' " + shlex.quote(image),
        timeout=45,
    ).stdout.strip()
    ids = run(host, "docker ps -q --no-trunc", timeout=45).stdout.split()
    matches = []
    for ident in ids:
        data = json.loads(
            run(
                host,
                "docker inspect --format '{{json .}}' " + shlex.quote(ident),
                timeout=30,
            ).stdout
        )
        if data.get("Image") == image_id and data.get("State", {}).get("Running"):
            matches.append(data)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one running container for {image}; found {len(matches)}"
        )
    data = matches[0]
    return dict(
        machine_fingerprint=identity,
        binding=dict(container_id=data["Id"], image_id=image_id),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True)
    p.add_argument("--image", default=IMAGE)
    p.add_argument("--write-bindings", action="store_true")
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    _, pools = load(root, a.worker_id)
    failed = False
    changed = False
    for name, pool in pools.items():
        if pool["owner"] != a.worker_id or not pool["enabled"]:
            continue
        for node in pool["nodes"]:
            try:
                result = probe_node(node, a.image)
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
    if a.write_bindings and changed and not failed:
        path = root / "config/pools.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(pools, indent=2) + "\n")
        temp.replace(path)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
