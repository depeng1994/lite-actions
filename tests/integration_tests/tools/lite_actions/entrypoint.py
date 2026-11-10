"""Small self-smoke adapter for the pinned repository's declared cases."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def definition(test_id):
    path = Path(__file__).resolve().parents[4] / ".ci/lite_actions/smoke_cases.json"
    data = json.loads(path.read_text())
    rows = [row for row in data["cases"] if row["test_id"] == test_id]
    if len(rows) != 1:
        raise ValueError("unknown or ambiguous self-smoke case")
    row = rows[0]
    return {
        "test_id": row["test_id"],
        "label": row["label"],
        "nnodes": row["nodes"],
        "ngpu": row["devices_per_node"],
        "timeout": row["timeout"],
        "kind": row["kind"],
        "ci_overrides": {"worker": row["worker"]},
        "env_vars": row["env_vars"],
    }


def result_for(case):
    return {
        "status": "PASS",
        "test_id": case["test_id"],
        "world_size": case["nnodes"] * case["ngpu"],
        "nodes": case["nnodes"],
        "devices_per_node": case["ngpu"],
    }


def launch(case, output):
    if case["kind"] != "npu_smoke":
        raise ValueError("only NPU smoke can execute")
    nodes, ngpu = case["nnodes"], case["ngpu"]
    ips = os.environ["NODE_IPS"].split(",")
    rank = int(os.environ["NODE_RANK"])
    if (
        int(os.environ["NNODES"]) != nodes
        or int(os.environ["NGPU"]) != ngpu
        or len(ips) != nodes
        or not 0 <= rank < nodes
    ):
        raise ValueError("allocated smoke topology mismatch")
    for name in ("LITE_CI_MASTER_PORT", "LITE_CI_HCCL_IF_BASE_PORT"):
        port = os.environ[name]
        if not port.isascii() or not port.isdecimal() or not 1024 <= int(port) < 65536:
            raise ValueError("invalid leased port")
    for name in ("LITE_CI_HCCL_HOST_PORT_RANGE", "LITE_CI_HCCL_NPU_PORT_RANGE"):
        if not re.fullmatch(r"[0-9]{4,5}-[0-9]{4,5}", os.environ[name]):
            raise ValueError("invalid leased port range")
    env = {
        **os.environ,
        "LITE_SMOKE_EXPECTED_WORLD": str(nodes * ngpu),
        "OMP_NUM_THREADS": "1",
    }
    command = [
        sys.executable,
        "-m",
        "torch.distributed.run",
        "--nnodes",
        str(nodes),
        "--nproc-per-node",
        str(ngpu),
        "--node-rank",
        str(rank),
        "--master-addr",
        ips[0],
        "--master-port",
        os.environ["LITE_CI_MASTER_PORT"],
        "-m",
        "tests.integration_tests.tools.lite_actions.dispatch_smoke_worker",
    ]
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("smoke output is not empty")
    subprocess.run(command, env=env, check=True, timeout=case["timeout"])
    (output / "dispatch_smoke_result.json").write_text(
        json.dumps(result_for(case)) + "\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("inspect", "launch", "verify"))
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    case = definition(args.test_id)
    if args.phase == "inspect":
        print(json.dumps([case]))
    elif args.output_dir is None:
        parser.error("output directory is required")
    elif args.phase == "launch":
        launch(case, args.output_dir)
    elif json.loads(
        (args.output_dir / "dispatch_smoke_result.json").read_text()
    ) != result_for(case):
        raise ValueError("invalid smoke result")


if __name__ == "__main__":
    main()
