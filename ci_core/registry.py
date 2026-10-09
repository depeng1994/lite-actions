"""Resolve a GitHub-selected test only against a pinned model-repo registry.

GitHub Actions inputs are transferred via a bounded run/attempt/SHA-bound
Artifact, and the model's pinned commit registry authorizes selected modules.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tarfile

SELECTOR = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
MODULE = re.compile(r"tests\.integration_tests(?:\.[a-z][a-z0-9_]*){1,6}\Z")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
RESERVED = {"NODE_IPS", "NGPU", "NNODES", "ASCEND_RT_VISIBLE_DEVICES",
            "PYTHONUNBUFFERED", "LITE_CI_JOB_ID", "HF_ASSETS_PATH",
            "CKPT_INIT_LOAD_PATH", "CKPT_SAVE_LOAD_PATH", "Network_Interface",
            "PYTHONPATH", "ASCEND_SET_ENV_PATH"}
REGISTRY = "tests/integration_tests/ci_registry.json"


def workflow_label(run: dict) -> str:
    """Static GitHub Workflow name, independently from job inputs.

    Older runs embedded test selection in the title; accept those for audit.
    New runs leave the title exactly equal to the Workflow name.
    """
    name, title = run.get("name"), run.get("display_title")
    if not isinstance(name, str) or not isinstance(title, str):
        raise ValueError("missing workflow title")
    if name != title:
        raise ValueError("inconsistent GitHub run name / display_title")
    static = name.split(" | test=", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", static):
        raise ValueError("invalid workflow name")
    return static


def selected_test(run: dict) -> str:
    """Legacy run-name format compatibility; not used by new task dispatch."""
    workflow_label(run)
    title = run["display_title"]
    if " | test=" not in title:
        raise ValueError("selected test is available only in request artifact")
    selected = title.split(" | test=", 1)[1]
    if not SELECTOR.fullmatch(selected):
        raise ValueError("invalid legacy test ID")
    return selected


def parse_request(data: dict, run: dict) -> list[dict]:
    """Validate an Actions artifact bound to one exact run, attempt and SHA."""
    if not isinstance(data, dict) or set(data) != {"schema", "run_id", "attempt", "sha", "cases"}:
        raise ValueError("invalid CI request schema")
    if (data["schema"] != 1 or type(data["run_id"]) is not int
            or type(data["attempt"]) is not int
            or data["run_id"] != run["id"]
            or data["attempt"] != run.get("run_attempt", 1)
            or data["sha"] != run["head_sha"]):
        raise ValueError("CI request/run identity mismatch")
    cases = data["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ValueError("CI request must contain 1..12 cases")
    for item in cases:
        if not isinstance(item, dict) or set(item) != {"test_id", "params"}:
            raise ValueError("invalid CI case format")
        if not isinstance(item["test_id"], str) or not SELECTOR.fullmatch(item["test_id"]):
            raise ValueError("invalid CI test ID")
        params = item["params"]
        if not isinstance(params, dict) or len(params) > 12 or any(
            not isinstance(k, str) or not ENV_KEY.fullmatch(k)
            or not isinstance(v, str) or len(v) > 120 or any(c in v for c in "\r\n\x00")
            for k,v in params.items()
        ):
            raise ValueError("invalid CI case params")
    return cases


def load_profile(archive: Path, test_case: str, *, nodes: int, ngpu: int,
                 params: dict | None = None) -> dict:
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
    params = params if params is not None else {}
    if not isinstance(params, dict) or any(k not in env or k in RESERVED for k in params):
        raise ValueError("parameters not allowed by registered model test")
    if params:
        for key, value in params.items():
            if not isinstance(value, str) or len(value) > 120 or any(c in value for c in "\r\n\x00"):
                raise ValueError("invalid registered test parameter")
            if key == "STEPS" and (not value.isdecimal() or not 1 <= int(value) <= 1000):
                raise ValueError("STEPS must be between 1 and 1000")
            if key == "COMPILE_ENABLE" and value not in ("0", "1"):
                raise ValueError("COMPILE_ENABLE must be 0 or 1")
        profile = dict(profile)
        profile["env_vars"] = {**env, **params}
    return profile
