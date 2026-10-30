#!/usr/bin/env python3
"""Release a quarantined case only after read-only process and NPU verification."""

import argparse
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_core.config import execution_node, load
from ci_core.executor import remote_preflight, task_dir
from ci_core.job_state import JobState
from ci_core.transport import run


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True)
    p.add_argument("--task-key", required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    _, pools = load(root, a.worker_id)
    state = JobState(root)
    record = state.read().get(a.task_key)
    if not record or not record.get("quarantined"):
        raise ValueError("task is not quarantined")
    if record["item"]["worker"] != a.worker_id:
        raise ValueError("wrong worker")
    runtime = record.get("runtime")
    if not runtime or not record.get("allocation"):
        raise ValueError("allocation missing; manual reconciliation required")
    subrun = {**record["run"], "_ci_test_id": record["item"]["test_id"]}
    stamp = task_dir(subrun, root).name
    script = """import os,pathlib,sys
marker=('LITE_CI_JOB_ID='+sys.argv[1]).encode()
for p in pathlib.Path('/proc').glob('[0-9]*/environ'):
 try:
  if marker in p.read_bytes().split(b'\\x00'):raise SystemExit('owned process still active')
 except (FileNotFoundError,ProcessLookupError):pass
"""
    lookup = {n["host_id"]: n for n in pools[runtime["pool"]]["nodes"]}
    for allocation in record["allocation"]:
        node = execution_node(lookup[allocation["host_id"]], runtime["docker"])
        run(
            node["ssh"],
            "python3 -c " + shlex.quote(script) + " " + shlex.quote(stamp),
            timeout=30,
        )
    from ci_core.executor import prepare_profile

    profile = prepare_profile(runtime, subrun)
    remote_preflight(runtime, pools, ascend_env=profile["ASCEND_SET_ENV_PATH"])
    state.update(
        a.task_key,
        quarantined=False,
        reconciliation="verified no owned process and no NPU occupancy",
    )
    print(json.dumps(dict(task_key=a.task_key, quarantined=False)))


if __name__ == "__main__":
    main()
