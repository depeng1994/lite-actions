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
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
RESERVED = {"NODE_IPS", "NGPU", "NNODES", "ASCEND_RT_VISIBLE_DEVICES",
            "PYTHONUNBUFFERED", "LITE_CI_JOB_ID", "HF_ASSETS_PATH",
            "CKPT_INIT_LOAD_PATH", "CKPT_SAVE_LOAD_PATH", "Network_Interface",
            "PYTHONPATH", "ASCEND_SET_ENV_PATH"}


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
    """Identity-bound, safe suite/ID selectors, never modules or CLI."""
    if not isinstance(data,dict) or set(data)!={"schema","run_id","attempt","sha","cases"}:
        raise ValueError("invalid CI request schema")
    if data["schema"]!=1 or type(data["run_id"]) is not int or type(data["attempt"]) is not int or data["run_id"]!=run["id"] or data["attempt"]!=run.get("run_attempt",1) or data["sha"]!=run["head_sha"]:
        raise ValueError("CI request/run identity mismatch")
    cases=data["cases"]
    if not isinstance(cases,list) or not 1<=len(cases)<=12:raise ValueError("invalid CI request case count")
    for item in cases:
        if not isinstance(item,dict) or len(item)!=2 or "params" not in item or (('test_id' in item)==('suite' in item)):
            raise ValueError("invalid CI case format")
        name=item.get('test_id',item.get('suite'))
        if not isinstance(name,str) or not SELECTOR.fullmatch(name):raise ValueError("invalid CI test ID/suite")
        params=item['params']
        if not isinstance(params,dict) or set(params)-{'STEPS'}:raise ValueError("unapproved CI parameter")
        if 'STEPS' in params:
            v=params['STEPS']
            if not isinstance(v,str) or not v.isdecimal() or not 1<=int(v)<=1000:
                raise ValueError("STEPS must be 1..1000")
    return cases


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
            tar.extractall(folder,filter='data')
        argv=[sys.executable,'-m','tests.integration_tests.lite_actions.tools.entrypoint','inspect']
        argv+=['--suite',selection['suite']] if 'suite' in selection else ['--test-id',selection['test_id']]
        env={**os.environ,'PYTHONPATH':str(folder)}
        if 'STEPS' in selection['params']:env['LITE_TEST_STEPS']=selection['params']['STEPS']
        else:env.pop('LITE_TEST_STEPS',None)
        proc=subprocess.run(argv,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
        if proc.returncode or not proc.stdout.strip():
            raise ValueError('untrusted/missing CI definition: '+proc.stderr[-220:])
        rows=json.loads(proc.stdout.strip().splitlines()[-1])
        if not isinstance(rows,list) or not rows:raise ValueError('empty test selection')
        return rows


def resolve_request(archive:Path, selections:list[dict],*,nodes:int,ngpu:int,max_cases:int)->list[dict]:
    expanded=[]
    seen=set()
    for item in selections:
        for found in inspect_archive(archive,item):
            name=found.get('test_id')
            if not isinstance(name,str) or not SELECTOR.fullmatch(name) or name in seen:
                raise ValueError('duplicate/invalid/disabled selected test')
            if (found.get('nnodes'),found.get('ngpu'))!=(nodes,ngpu):
                raise ValueError('requested test topology does not match NPU allocation')
            if type(found.get('timeout')) is not int or found['timeout']>14400:
                raise ValueError('unsafe model test duration')
            seen.add(name)
            expanded.append({'test_id':name,'params':item['params']})
    if len(expanded)>max_cases:raise ValueError('expanded suite exceeds channel/Waiter budget')
    return expanded
