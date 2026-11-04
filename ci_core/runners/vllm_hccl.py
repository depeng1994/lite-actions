"""Trusted Blue HCCL supervisor, copied separately from the tested model source.

Only the approved source payload runs in the NPU container. Platform tokens
remain on the controller. This is not a sandbox for arbitrary fork code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import runpy
from pathlib import Path

TOPOLOGIES = {"dispatch_smoke_a3_8p": 8, "dispatch_smoke_a3_16p": 16}
PAYLOAD = ".github/scripts/lite_actions/blue_hccl_payload.py"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-id", choices=tuple(TOPOLOGIES), required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise ValueError("invalid immutable source SHA")
    root = args.source_root.resolve(strict=True)
    payload = (root / PAYLOAD).resolve(strict=True)
    if not payload.is_relative_to(root) or not payload.is_file():
        raise ValueError("source payload escapes tested source")
    world = int(os.environ["WORLD_SIZE"])
    if world != TOPOLOGIES[args.test_id]:
        raise ValueError("runtime world size differs from trusted case")
    local_rank, rank = int(os.environ["LOCAL_RANK"]), int(os.environ["RANK"])

    import torch
    import torch.distributed as dist
    import torch_npu

    torch.npu.set_device(local_rank)
    dist.init_process_group("hccl", timeout=dt.timedelta(seconds=150))
    tensor = torch.tensor([float(rank)], device=f"npu:{local_rank}")
    namespace = runpy.run_path(str(payload))
    if namespace.get("PAYLOAD_API_VERSION") != 1:
        raise ValueError("unsupported HCCL source payload interface")
    collective = namespace.get("all_reduce")
    if not callable(collective):
        raise ValueError("missing source all_reduce payload")
    collective(tensor)
    torch.npu.synchronize()
    expected = world * (world - 1) // 2
    actual = tensor.item()
    if actual != expected:
        raise AssertionError(f"all_reduce result {actual} != {expected}")
    dist.barrier()
    if rank == 0:
        evidence = {
            "schema": 1,
            "test_id": args.test_id,
            "status": "PASS",
            "source_sha": args.source_sha,
            "world_size": world,
            "all_reduce_sum": actual,
            "payload_path": str(payload),
            "torch_version": str(torch.__version__),
            "torch_npu_version": str(torch_npu.__version__),
        }
        args.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = args.output_dir / "blue-smoke-evidence.json.tmp"
        temporary.write_text(json.dumps(evidence, sort_keys=True) + "\n")
        temporary.replace(args.output_dir / "blue-smoke-evidence.json")
        print("DISPATCH_SMOKE_PASS " + json.dumps(evidence, sort_keys=True), flush=True)
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
