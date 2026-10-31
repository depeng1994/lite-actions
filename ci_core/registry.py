"""Resolve GitHub test selectors against trusted OverrideDefinitions at a fixed SHA.

Inputs arrive in a run/attempt/SHA-bound Artifact. Model code defines the
cases; deployment/runtime resource configuration stays with Lite Actions.
"""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

SELECTOR = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


ASSET_ENV_KEYS = ("ASCEND_SET_ENV_PATH", "HF_ASSETS_PATH", "CKPT_INIT_LOAD_PATH")
RESERVED_RUNTIME_ENV = frozenset(
    (
        "NODE_IPS",
        "NGPU",
        "NNODES",
        "ASCEND_RT_VISIBLE_DEVICES",
        "LITE_CI_JOB_ID",
        "CKPT_SAVE_LOAD_PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "Network_Interface",
    )
)
CASE_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def validate_case_environment(env: object, *, kind: str = "train") -> dict[str, str]:
    """Validate trusted model-derived environment before SSH and preflight.

    An A5 case without configured assets remains discoverable with `inspect`,
    but cannot be executed until its real paths are defined in model source.
    """
    if not isinstance(env, dict):
        raise ValueError("model test env_vars must be a dictionary")
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not CASE_ENV_KEY.fullmatch(key)
            or key in RESERVED_RUNTIME_ENV
            or not isinstance(value, str)
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("invalid/forbidden test-owned environment variable")
    if kind not in {"train", "npu_smoke"}:
        raise ValueError("unsupported NPU execution kind")
    required = ASSET_ENV_KEYS if kind == "train" else ("ASCEND_SET_ENV_PATH",)
    for key in required:
        path = env.get(key)
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"model test must define an absolute {key} in env_vars")
    return env.copy()


def _discovery_tar_filter(member: tarfile.TarInfo, path: str):
    """Strict extraction filter for metadata discovery.

    Model metadata inspection does not need repository symlinks/hardlinks.
    Drop all links and retain Python data_filter protections for ordinary
    files/directories, including absolute paths and path traversal.
    """
    if member.issym() or member.islnk():
        return None
    return tarfile.data_filter(member, path)


def inspect_archive(archive: Path, selection: dict) -> list[dict]:
    """Import only trusted pinned-SHA model definitions in an isolated tmpdir.

    This is not a security sandbox: actor/branch/SHA authorization remains the
    prerequisite; a Python build_test_list runs trusted repository code.
    """
    from ci_core.contract import REPO

    if REPO == "torchtitan-npu/vllm-ascend":
        from ci_core.vllm_ascend import definition

        if "test_id" in selection:
            return [definition(selection["test_id"])]
        raise ValueError("vLLM only accepts fixed trusted smoke test IDs")

    import os
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lite-ci-discovery-") as temp:
        folder = Path(temp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(folder, filter=_discovery_tar_filter)
        argv = [
            sys.executable,
            "-m",
            "tests.integration_tests.tools.lite_actions.entrypoint",
            "inspect",
        ]
        argv += (
            ["--suite", selection["suite"]]
            if "suite" in selection
            else ["--test-id", selection["test_id"]]
        )
        env = {**os.environ, "PYTHONPATH": str(folder)}
        proc = subprocess.run(
            argv, cwd=folder, env=env, capture_output=True, text=True, timeout=45
        )
        if proc.returncode or not proc.stdout.strip():
            raise ValueError("untrusted/missing CI definition: " + proc.stderr[-220:])
        rows = json.loads(proc.stdout.strip().splitlines()[-1])
        if not isinstance(rows, list) or not rows:
            raise ValueError("empty test selection")
        return rows


def resolve_test(archive: Path, test_id: str, *, nodes: int, ngpu: int) -> dict:
    rows = inspect_archive(archive, {"test_id": test_id})
    if len(rows) != 1 or rows[0].get("test_id") != test_id:
        raise ValueError("missing or ambiguous pinned test")
    found = rows[0]
    if (found.get("nnodes"), found.get("ngpu")) != (nodes, ngpu):
        raise ValueError("test topology does not match leased resources")
    return {
        **found,
        "env_vars": validate_case_environment(
            found.get("env_vars"), kind=found["kind"]
        ),
    }
