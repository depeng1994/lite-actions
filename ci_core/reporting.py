"""Pure dispatcher-side Python result packaging and GitHub REST reporting.

The Pull Agent only schedules this function; no AI agent API is involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from ci_core.dispatch import execute, task_dir
from ci_core.github import GitHubClient, authorization
from ci_core.job_state import JobState
from ci_core.logs import clean
from ci_core.registry import workflow_label

NAMES = {
    "a3-smoke": ("Dispatcher-Smoke", "A3-2p-CI"),
    "a3-8p": ("A3-8p-CI-Example", "A3-8p-CI"),
    "a3-16p": ("A3-16p-CI-Example", "A3-16p-CI"),
    "a5-64p": ("A5-64p-CI",),
}


def job_key(run: dict) -> str:
    return f"{run['id']}:{run.get('run_attempt', 1)}"


def marker(case: dict, run: dict) -> str:
    name = run.get("name", NAMES[case["name"]][0])
    if name not in NAMES[case["name"]] and case["runner"]["kind"] == "ssh":
        name = workflow_label(run)
    if name not in NAMES[case["name"]]:
        raise ValueError("unexpected workflow name")
    return f"{name}-RESULT {job_key(run)}"


def case_summary(log:Path|None,run:dict)->list[dict]:
    """Extract bounded per-test results; missing events never imply PASS."""
    import re
    plan=[x['test_id'] for x in run.get('_ci_expanded_cases',[])]
    results={}
    if log is not None and log.is_file():
        for line in log.read_text(encoding='utf-8',errors='replace').splitlines():
            if line.startswith('CASE_PLAN '):
                names=line[len('CASE_PLAN '):].split(',')
                if not plan:plan=names[:12]
            match=re.fullmatch(r'CASE_RESULT ([a-z][a-z0-9_]{0,79}) (PASS|FAIL) (\d+)',line)
            if match:
                name,status,rc=match.groups()
                if name in plan and name not in results:
                    results[name]={'test_name':name,'status':status,'exit_code':int(rc)}
    if not plan and '_ci_request' in run:
        plan=[row.get('test_id',row.get('suite','unknown')) for row in run['_ci_request'].get('cases',[])][:12]
    return [results.get(name,{'test_name':name,'status':'NOT_RUN','exit_code':None}) for name in plan]


def make_report(case: dict, run: dict, rc: int, excerpt: list[str], log:Path|None=None) -> dict:
    if len(excerpt)>20 or not all(isinstance(x,str) for x in excerpt):
        raise ValueError('invalid dispatcher excerpt')
    cases=case_summary(log,run) if case.get('runner',{}).get('kind')=='ssh' else []
    ok=rc==0 and (not cases or all(row['status']=='PASS' and row['exit_code']==0 for row in cases))
    return {
        'run_id':run['id'],'attempt':run.get('run_attempt',1),'sha':run['head_sha'],
        'pipeline':case['pipeline_id'],'status':'PASS' if ok else 'FAIL',
        'exit_code':rc if rc!=0 else (0 if ok else 125),
        'cases':cases,
        'last_20_lines':[clean(x) for x in excerpt],
    }


def report_payload(case: dict, run: dict, report: dict) -> str:
    return marker(case, run) + "\n" + json.dumps(
        report, ensure_ascii=False, separators=(",", ":"))


def post_result(case: dict, run: dict, report: dict, log_path: Path, state: JobState) -> None:
    client = GitHubClient(authorization())
    body = report_payload(case, run, report)
    if len(body.encode("utf-8")) > 6100:
        raise ValueError("GitHub result exceeds 6100-byte transport budget")
    if not any((item.get("body") or "").startswith(marker(case, run) + "\n")
               for item in client.commit_comments(run["head_sha"])):
        client.send_result(run["head_sha"], body)
    metrics = {
        "time": datetime.now(timezone.utc).isoformat(),
        "run_id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "status": report["status"],
        "final_post_body_bytes": len(json.dumps({"body": body},
                             ensure_ascii=False, separators=(",", ":")).encode()),
        "api_request_upload_bytes_estimate": client.sent_bytes,
        "api_request_count": client.requests,
        "full_log": str(log_path),
        "note": "Excludes idle polls, proxy, TLS and TCP overhead",
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    (log_path.parent / "upload-metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    state.update(job_key(run), phase="reported", result=report["status"])
    logging.info("reported run=%s pipeline=%s %s",
                 run["id"], case["name"], report["status"])


def run_and_report(case: dict, run: dict, *, root: Path) -> None:
    """Run fixed pipeline, collect diagnostics, and POST: all on dispatcher."""
    state = JobState(root)
    key = job_key(run)
    output = task_dir(run, root) / "pipeline.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = state.read().get(key, {})
    if pending.get("phase") == "pending_report":
        report = pending["report"]
    else:
        try:
            if "resources" in case:
                from ci_core.dispatch import prepare_profile
                from ci_core.scheduler import claim_dynamic
                _, profile = prepare_profile(case, run)
                with claim_dynamic(root, case, ascend_env=profile["ascend_env"],
                                   timeout=900) as runtime_case:
                    execution = execute(runtime_case, run, root=root,
                                        lock_root=root / "state/device-locks",
                                        pre_reserved=True)
            else:
                execution = execute(case, run, root=root,
                                    lock_root=root / "state/device-locks",
                                    resource_wait_seconds=900)
            output = execution.log
            report = make_report(case, run, execution.exit_code, execution.tail, execution.log)
        except Exception as exc:
            if not output.exists():
                output.write_text(f"CI infra error: {type(exc).__name__}\n")
            report = make_report(case, run, 125,
                                 [f"Dispatcher error: {type(exc).__name__}"],output)
        state.update(key, phase="pending_report", report=report)
    try:
        post_result(case, run, report, output, state)
    except Exception as exc:
        logging.error("GitHub POST pending for run=%s: %s",
                      run["id"], type(exc).__name__)
