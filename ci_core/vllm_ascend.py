"""Trusted vLLM Ascend A5 smoke definitions (never import PR Python in router)."""

import os

CASES = {
    "dispatch_smoke_a5_8p": (1, 8, 120),
    "dispatch_smoke_a5_32p": (4, 8, 240),
}


def definition(test_id):
    if test_id not in CASES:
        raise ValueError("unknown vLLM A5 smoke case")
    nodes, ngpu, timeout = CASES[test_id]
    toolkit = os.environ.get(
        "LITE_ACTIONS_VLLM_ASCEND_SET_ENV_PATH",
        "/usr/local/Ascend/ascend-toolkit/set_env.sh",
    )
    if not toolkit.startswith("/") or "\n" in toolkit or "\x00" in toolkit:
        raise ValueError("invalid trusted Ascend toolkit path")
    return {
        "test_id": test_id,
        "label": "A5 HCCL distributed smoke " + str(nodes * ngpu) + "P",
        "nnodes": nodes,
        "ngpu": ngpu,
        "kind": "npu_smoke",
        "ci_overrides": {},
        "timeout": timeout,
        "env_vars": {"ASCEND_SET_ENV_PATH": toolkit},
    }
