"""Resolve a GitHub-selected test only against a pinned model-repo registry.

workflow_dispatch inputs are not reliably returned as fields by GitHub's
list-runs REST endpoint; the model Workflow explicitly projects the chosen
`test_case` into its `run-name`, exposed as `display_title`.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tarfile

SELECTOR = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
MODULE = re.compile(r"tests\.integration_tests\.nightly_all_models_test\.[a-z][a-z0-9_]*_tests\Z")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
RESERVED = {"NODE_IPS", "NGPU", "NNODES", "ASCEND_RT_VISIBLE_DEVICES",
            "PYTHONUNBUFFERED", "LITE_CI_JOB_ID", "HF_ASSETS_PATH",
            "CKPT_INIT_LOAD_PATH", "CKPT_SAVE_LOAD_PATH", "Network_Interface",
            "PYTHONPATH", "ASCEND_SET_ENV_PATH"}
REGISTRY = "tests/integration_tests/ci_registry.json"


def workflow_label(run: dict) -> str:
    """Extract the static Workflow name from a GitHub custom run-name.

    GitHub's runs API sets BOTH `name` and `display_title` to the run-name,
    so unlike the Workflow YAML `name`, the fields can include test selection.
    """
    name = run.get("name")
    title = run.get("display_title")
    if not isinstance(name, str) or not isinstance(title, str) or name != title:
        raise ValueError("inconsistent GitHub run name/display_title")
    prefix, separator, selector = title.partition(" | test=")
    if not separator or not SELECTOR.fullmatch(selector):
        raise ValueError("missing or invalid GitHub CI test selector")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", prefix):
        raise ValueError("unsafe Workflow name")
    return prefix


def selected_test(run: dict) -> str:
    workflow_label(run)
    return run["display_title"].split(" | test=", 1)[1]


def load_profile(archive: Path, test_case: str, *, nodes: int, ngpu: int) -> dict:
    """Read data, not executable code, from precisely the dispatched Git SHA."""
    if not SELECTOR.fullmatch(test_case):
        raise ValueError("unsafe CI test selector")
    with tarfile.open(archive, "r:gz") as tar:
        item = tar.extractfile(REGISTRY)
        if item is None:
            raise ValueError("model repository does not expose CI registry")
        payload = item.read(65537)
    if len(payload) > 65536:
        raise ValueError("CI registry is too large")
    items = json.loads(payload)
    if not isinstance(items, dict) or test_case not in items:
        raise ValueError("test_case not registered in model SHA")
    profile = items[test_case]
    if not isinstance(profile, dict) or not MODULE.fullmatch(profile.get("module", "")):
        raise ValueError("invalid model-registered entrypoint")
    if profile.get("mode") not in ("single", "distributed"):
        raise ValueError("invalid model test mode")
    if type(profile.get("nnodes")) is not int or type(profile.get("ngpu")) is not int:
        raise ValueError("invalid registered test topology")
    if (profile["nnodes"], profile["ngpu"]) != (nodes, ngpu):
        raise ValueError("test_case resource requirements do not match this CI channel")
    if profile["mode"] == "single" and nodes != 1:
        raise ValueError("single-node test cannot be dispatched across nodes")
    for key in ("ascend_env", "hf_assets_path", "ckpt_init_path"):
        value = profile.get(key)
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or "\n" in value:
            raise ValueError(f"model test {test_case} requires configured {key}")
    env = profile.get("env_vars")
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not ENV_KEY.fullmatch(key)
        or key in RESERVED or not isinstance(value, str)
        or len(value) > 160 or any(c in value for c in "\r\n\x00")
        for key, value in env.items()
    ):
        raise ValueError("invalid model-owned test environment")
    return profile
