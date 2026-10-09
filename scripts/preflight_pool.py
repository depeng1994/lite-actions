#!/usr/bin/env python3
"""Read-only SSH access check for one Lite Actions resource pool.

No training, resource leases, process termination or auto-accepting host keys.
Hardware occupancy checks are separate and hardware-specific.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check_node(node: dict, *, npu: bool = False) -> tuple[str, bool, str]:
    address = node.get("ssh")
    if not address or not isinstance(address, str):
        return (node["host_id"], False, "no SSH address configured")
    # Deliberately avoid user-controlled shell commands, password prompts and
    # accepting unknown SSH server keys automatically.
    command = "npu-smi info" if npu else "hostname"
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", "ConnectTimeout=5", address, command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (node["host_id"], False, type(exc).__name__)
    if result.returncode:
        return (node["host_id"], False,
                (result.stderr.strip().splitlines() or [f"exit={result.returncode}"])[-1][:140])
    return (node["host_id"], True, (result.stdout.strip().splitlines() or ["OK"])[0][:140])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, help="Trusted name in config/pools.json")
    parser.add_argument("--npu", action="store_true", help="Also run read-only npu-smi info (not an occupancy certification)")
    args = parser.parse_args(argv)
    pools = json.loads((ROOT / "config/pools.json").read_text())
    if args.pool not in pools:
        parser.error(f"unknown resource pool {args.pool!r}")
    nodes = pools[args.pool]["nodes"]
    with ThreadPoolExecutor(max_workers=min(len(nodes), 8) or 1) as executor:
        results = list(executor.map(lambda n: check_node(n, npu=args.npu), nodes))
    for host, ok, message in results:
        print(f"{'PASS' if ok else 'FAIL'} {host}: {message}")
    return 0 if results and all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
