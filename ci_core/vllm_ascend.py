"""Trusted vLLM smoke catalog; never import model Python on the controller."""

import os

VLLM_BLUE_SMOKE_DOCKER = "vllm_hccl_smoke"
BLUE_CASES = {
    "vllm_worker_os_blue": (0, 0, 90),
    "dispatch_smoke_a3_8p": (1, 8, 180),
    "dispatch_smoke_a3_16p": (2, 8, 240),
}
A5_CASES = {
    "dispatch_smoke_a5_8p": (1, 8, 120),
    "dispatch_smoke_a5_32p": (4, 8, 240),
}
CASES = {**A5_CASES, **BLUE_CASES}
BLUE_PAYLOAD = ".github/scripts/lite_actions/blue_hccl_payload.py"


def definition(test_id):
    if test_id not in CASES:
        raise ValueError("unknown vLLM smoke case")
    nodes, ngpu, timeout = CASES[test_id]
    probe = test_id == "vllm_worker_os_blue"
    blue = test_id in BLUE_CASES
    toolkit = os.environ.get(
        "LITE_ACTIONS_VLLM_ASCEND_SET_ENV_PATH",
        "/usr/local/Ascend/ascend-toolkit/set_env.sh",
    )
    if not toolkit.startswith("/") or any(c in toolkit for c in "\n\r\x00"):
        raise ValueError("invalid trusted Ascend toolkit path")
    overrides = (
        {"worker": "blue"}
        if probe
        else {
            "worker": "blue",
            "target_pool": "a3_shared_blue",
            "docker": VLLM_BLUE_SMOKE_DOCKER,
        }
        if blue
        else {}
    )
    return {
        "test_id": test_id,
        "label": (
            "vLLM Blue worker OS probe"
            if probe
            else f"{'A3' if blue else 'A5'} HCCL distributed smoke {nodes * ngpu}P"
        ),
        "nnodes": nodes,
        "ngpu": ngpu,
        "kind": "worker_probe" if probe else "npu_smoke",
        "ci_overrides": overrides,
        "timeout": timeout,
        "env_vars": {"ASCEND_SET_ENV_PATH": toolkit},
    }


def validate_blue_case(item):
    """Enforce routing before any lease, independently of producer defaults."""
    if item["test_id"] not in BLUE_CASES:
        if item["worker"] == "blue":
            raise ValueError("vLLM Blue only accepts its fixed OS/A3 smoke catalog")
        return
    row = definition(item["test_id"])
    probe = row["kind"] == "worker_probe"
    expected = {
        "worker": "blue",
        "target_pool": None if probe else "a3_shared_blue",
        "docker": None if probe else VLLM_BLUE_SMOKE_DOCKER,
        "kind": row["kind"],
        "resources": {"nodes": row["nnodes"], "devices_per_node": row["ngpu"]},
        "execution_timeout_seconds": row["timeout"],
        "label": row["label"],
    }
    if any(item.get(key) != value for key, value in expected.items()):
        raise ValueError("vLLM Blue case/container policy mismatch")
