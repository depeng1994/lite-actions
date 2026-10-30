"""Resolve GitHub test selectors against trusted OverrideDefinitions at a fixed SHA.

Inputs arrive in a run/attempt/SHA-bound Artifact. Model code defines the
cases; deployment/runtime resource configuration stays with Lite Actions.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tarfile

SELECTOR = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


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
    """Parse historical title selectors for log-inspection compatibility."""
    workflow_label(run)
    title = run["display_title"]
    if " | test=" not in title:
        raise ValueError("selected test is available only in request artifact")
    selected = title.split(" | test=", 1)[1]
    if not SELECTOR.fullmatch(selected):
        raise ValueError("invalid historical test ID")
    return selected


ASSET_ENV_KEYS = ("ASCEND_SET_ENV_PATH", "HF_ASSETS_PATH", "CKPT_INIT_LOAD_PATH")
RESERVED_RUNTIME_ENV = frozenset((
    "NODE_IPS", "NGPU", "NNODES", "ASCEND_RT_VISIBLE_DEVICES", "LITE_CI_JOB_ID",
    "CKPT_SAVE_LOAD_PATH", "PYTHONPATH", "PYTHONUNBUFFERED", "Network_Interface",
))
CASE_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def validate_case_environment(env: object, *, kind: str = "train") -> dict[str, str]:
    """Validate trusted model-derived environment before SSH and preflight.

    An A5 case without configured assets remains discoverable with `inspect`,
    but cannot be executed until its real paths are defined in model source.
    """
    if not isinstance(env, dict):
        raise ValueError("model test env_vars must be a dictionary")
    for key, value in env.items():
        if (not isinstance(key, str) or not CASE_ENV_KEY.fullmatch(key)
                or key in RESERVED_RUNTIME_ENV or not isinstance(value, str)
                or "\x00" in value or "\n" in value or "\r" in value):
            raise ValueError("invalid/forbidden test-owned environment variable")
    if kind not in {"train", "npu_smoke"}:
        raise ValueError("unsupported NPU execution kind")
    required = ASSET_ENV_KEYS if kind == "train" else ("ASCEND_SET_ENV_PATH",)
    for key in required:
        path = env.get(key)
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"model test must define an absolute {key} in env_vars")
    return env.copy()


def parse_request(data: dict, run: dict) -> list[dict]:
    """Identity-bound, safe suite/ID selectors, never modules or CLI."""
    if not isinstance(data,dict) or set(data)!={"schema","run_id","attempt","sha","cases"}:
        raise ValueError("invalid CI request schema")
    if data["schema"]!=1 or type(data["run_id"]) is not int or type(data["attempt"]) is not int or data["run_id"]!=run["id"] or data["attempt"]!=run.get("run_attempt",1) or data["sha"]!=run["head_sha"]:
        raise ValueError("CI request/run identity mismatch")
    cases=data["cases"]
    if not isinstance(cases,list) or not cases:raise ValueError("invalid CI request case list")
    for item in cases:
        if (not isinstance(item, dict) or len(item) != 1
                or (('test_id' in item) == ('suite' in item))):
            raise ValueError("invalid CI case selector format")
        name = item.get('test_id', item.get('suite'))
        if not isinstance(name, str) or not SELECTOR.fullmatch(name):
            raise ValueError("invalid CI test ID/suite")
    return cases


def _discovery_tar_filter(member: tarfile.TarInfo, path: str):
    """Strict extraction filter for metadata discovery.

    Model metadata inspection does not need repository symlinks/hardlinks.
    Drop all links and retain Python data_filter protections for ordinary
    files/directories, including absolute paths and path traversal.
    """
    if member.issym() or member.islnk():
        return None
    return tarfile.data_filter(member, path)


def inspect_archive(archive:Path, selection:dict)->list[dict]:
    """Import only trusted pinned-SHA model definitions in an isolated tmpdir.

    This is not a security sandbox: actor/branch/SHA authorization remains the
    prerequisite; a Python build_test_list runs trusted repository code.
    """
    import os
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory(prefix='lite-ci-discovery-') as temp:
        folder=Path(temp)
        with tarfile.open(archive,'r:gz') as tar:
            tar.extractall(folder,filter=_discovery_tar_filter)
        argv=[sys.executable,'-m','tests.integration_tests.tools.lite_actions.entrypoint','inspect']
        argv+=['--suite',selection['suite']] if 'suite' in selection else ['--test-id',selection['test_id']]
        env={**os.environ,'PYTHONPATH':str(folder)}
        proc=subprocess.run(argv,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
        if proc.returncode or not proc.stdout.strip():
            raise ValueError('untrusted/missing CI definition: '+proc.stderr[-220:])
        rows=json.loads(proc.stdout.strip().splitlines()[-1])
        if not isinstance(rows,list) or not rows:raise ValueError('empty test selection')
        return rows


def resolve_request(archive:Path, selections:list[dict],*,nodes:int,ngpu:int)->list[dict]:
    expanded=[]
    seen=set()
    for item in selections:
        for found in inspect_archive(archive,item):
            name=found.get('test_id')
            if not isinstance(name,str) or not SELECTOR.fullmatch(name) or name in seen:
                raise ValueError('duplicate/invalid/disabled selected test')
            if (found.get('nnodes'),found.get('ngpu'))!=(nodes,ngpu):
                raise ValueError('requested test topology does not match NPU allocation')
            env_vars = validate_case_environment(found.get('env_vars'), kind=found.get('kind', 'train'))
            seen.add(name)
            expanded.append({'test_id': name, 'env_vars': env_vars})
    return expanded
