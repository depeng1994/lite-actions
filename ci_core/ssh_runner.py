"""One trusted SSH execution engine for A3/A5, 1..N nodes.

Only trusted local JSON selects the entrypoint. Training and validation live in
model repository at the exact GitHub SHA. No GitHub credential reaches nodes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import shutil
import json
from pathlib import Path
import re
import signal
import shlex
import subprocess
import sys
import tempfile

from ci_core.config import load_config
from ci_core.registry import inspect_archive, SELECTOR, parse_request, resolve_request

ROOT = Path(__file__).resolve().parents[1]
from ci_core.source_repo import REPO as MODEL_REPO
REPO = f"git@github.com:{MODEL_REPO}.git"
DEST_ROOT = "/mnt/share/ci_tests"
SHA = re.compile(r"[0-9a-f]{40}\Z")
STAMP = re.compile(r"[0-9A-Za-z_-]+\Z")


def q(value: object) -> str:
    return shlex.quote(str(value))


def ssh(host: str, command: str, *, timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=25",
         host, "bash -lc " + q(command)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"SSH {host} failed ({result.returncode}): {result.stderr[-250:]}")
    return result


def model_archive(sha: str, work: Path) -> Path:
    """Package exactly the vetted Commit SHA on dispatcher, not on shared NPUs."""
    if not SHA.fullmatch(sha):
        raise ValueError("invalid commit SHA")
    repo = work / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=25)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", REPO], check=True)
    subprocess.run(["git", "-C", str(repo), "fetch", "--depth=1", "-q", "origin", sha],
                   check=True, timeout=180)
    actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "FETCH_HEAD"], text=True).strip()
    if actual != sha:
        raise RuntimeError("GitHub SHA mismatch")
    archive = work / "source.tar.gz"
    with archive.open("wb") as output:
        pack = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", sha],
                                stdout=subprocess.PIPE)
        assert pack.stdout is not None
        with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1) as zipped:
            shutil.copyfileobj(pack.stdout, zipped)
        pack.stdout.close()
        if pack.wait(timeout=90) != 0:
            raise RuntimeError("git archive failed")
    if archive.stat().st_size == 0:
        raise RuntimeError("empty source archive")
    return archive


def allocations(case: dict, pools: dict) -> list[dict]:
    pool = pools[case["pool"]]
    lookup = {node["host_id"]: node for node in pool["nodes"]}
    nodes = []
    for position, allocated in enumerate(case["allocation"]):
        node = lookup[allocated["host_id"]]
        if not node.get("ssh") or not node.get("node_ip"):
            raise ValueError(f"missing SSH/HCCL address for {allocated['host_id']}")
        nodes.append({**node, "devices": list(allocated["devices"]), "position": position})
    if not nodes or len({n["host_id"] for n in nodes}) != len(nodes):
        raise ValueError("empty or duplicate multi-node allocation")
    if len({len(n["devices"]) for n in nodes}) != 1:
        raise ValueError("mixed devices-per-node not supported")
    return nodes


def stage(node: dict, base: str, archive: Path, sha: str) -> str:
    host = node["ssh"]
    dest = f"{base}/node{node['position']}"
    ssh(host, f"mkdir -p {q(base)} && mkdir {q(dest)} && mkdir {q(dest+'/repo')}")
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", str(archive),
                    f"{host}:{dest}/source.tar.gz"], check=True, timeout=180)
    ssh(host, f"cd {q(dest)} && tar -xzf source.tar.gz -C repo && "
              f"printf '%s\\n' {q(sha)} > source_sha.txt && "
              f"test -f repo/tests/integration_tests/__init__.py")
    print(f"STAGED: {node['host_id']} SHA={sha[:12]} dir={dest}", flush=True)
    return dest


def remote_command(node: dict, nodes: list[dict], runner: dict, dest: str,
                   stamp: str, timeout: int, test_case: str) -> str:
    """Remote job owns its own process group; its complete log stays remote."""
    # Generic, fixed model-owned entrypoint; never interpolate GitHub modules.
    entry = ("python3 -u -m tests.integration_tests.tools.lite_actions.entrypoint launch --test-id "
             + q(test_case) + " --output-dir " + q(dest + "/output"))
    env = {
        "NODE_IPS": ",".join(x["node_ip"] for x in nodes),
        "NGPU": str(len(node["devices"])),
        "NNODES": str(len(nodes)),
        "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, node["devices"])),
        "PYTHONUNBUFFERED": "1",
        "LITE_CI_JOB_ID": stamp,
        **runner["env_vars"],
    }
    if node.get("network_interface"):
        env["Network_Interface"] = node["network_interface"]
    env["CKPT_SAVE_LOAD_PATH"] = dest + "/checkpoints"
    setup = f"source {q(runner['env_vars']['ASCEND_SET_ENV_PATH'])} >/dev/null 2>&1 && "
    # PID file enables bounded cancellation. Python on the dispatcher always
    # validates job identity before killing its *own* remote process group.
    exports = " ".join(f"{key}={q(value)}" for key, value in env.items())
    # Preserve legacy A3 ordering: source CANN first, then apply trusted
    # NODE_IPS / HCCL / device variables so the toolkit cannot override them.
    inner = (f"cd {q(dest+'/repo')} && {setup}"
             f"export {exports} && "
             f"export PYTHONPATH=\"$PWD:${{PYTHONPATH:-}}\" && "
             f"exec timeout --signal=TERM --kill-after=30s {int(timeout)}s {entry}")
    # The previous `setsid ... & pid=$!` occasionally recorded a wrapper PID
    # instead of the process-group leader. Python's start_new_session creates
    # the exact PGID/PID that we persist and may safely cancel by job marker.
    supervisor = (
        "import os,pathlib,subprocess,sys; "
        "dest=pathlib.Path(sys.argv[1]); marker=sys.argv[2]; inner=sys.argv[3]; "
        "log=(dest/'run.log').open('wb'); "
        "env=dict(os.environ,LITE_CI_JOB_ID=marker); "
        "proc=subprocess.Popen(['bash','-lc',inner],stdout=log,"
        "stderr=subprocess.STDOUT,env=env,start_new_session=True); "
        "(dest/'job.pgid').write_text(str(proc.pid)+'\\n'); "
        "rc=proc.wait(); log.close(); "
        "(dest/'exit_code.txt').write_text(str(rc)+'\\n'); "
        "print('NODE_EXIT:',rc,flush=True); sys.exit(rc)"
    )
    return (f"cd {q(dest)} || exit 125; "
            f"python3 -c {q(supervisor)} {q(dest)} {q(stamp)} {q(inner)}")


def cancel(node: dict, dest: str, stamp: str) -> None:
    """Stop all verified processes owned by this exact CI job.

    Torchrun's ranks can create independent process groups; killing only the
    saved launcher PGID leaves active NPU ranks.  Match the unique inherited
    LITE_CI_JOB_ID in /proc, record PID start times, and signal each PID only
    after an identity recheck. Never match an unrelated command line.
    """
    code = """\
import os
from pathlib import Path
import signal
import sys
import time

folder = Path(sys.argv[1])
marker = sys.argv[2]
leader = int((folder / 'job.pgid').read_text().strip())
if leader <= 1:
    raise SystemExit('invalid recorded job pid')
tag = b'LITE_CI_JOB_ID=' + marker.encode()

def identity(pid):
    try:
        env = Path(f'/proc/{pid}/environ').read_bytes()
        if tag not in env.split(b'\\x00'):
            return None
        # Field 22 is process starttime; remove the '(comm)' part first.
        data = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return data[19]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

# Never act on a reused launcher PID or any unverified foreign process.
if Path(f'/proc/{leader}').exists() and identity(leader) is None:
    raise SystemExit('refuse reused/foreign launcher PID')
owned = {}
for entry in os.scandir('/proc'):
    if entry.name.isdecimal():
        pid = int(entry.name)
        start = identity(pid)
        if start is not None:
            owned[pid] = start

for pid, start in owned.items():
    if identity(pid) == start:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

for _ in range(12):
    if not any(identity(pid) == start for pid, start in owned.items()):
        break
    time.sleep(1)

for pid, start in owned.items():
    if identity(pid) == start:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

time.sleep(1)
left = [pid for pid, start in owned.items() if identity(pid) == start]
if left:
    raise SystemExit('owned process cleanup unverified: ' + str(left))
"""
    try:
        ssh(node["ssh"], f"python3 -c {q(code)} {q(dest)} {q(stamp)}", timeout=35)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"CLEANUP_UNVERIFIED: {node['host_id']} {type(exc).__name__}", flush=True)


def verify(nodes: list[dict], runner: dict, base: str, timeout: int, test_case: str) -> int:
    if runner["mode"] != "distributed":
        return 0
    node = nodes[0]
    dest = base + "/node0"
    exports = runner["env_vars"]
    exported = " ".join(f"{key}={q(value)}" for key, value in exports.items())
    command = (f"cd {q(dest+'/repo')} && "
               + f"source {q(runner['env_vars']['ASCEND_SET_ENV_PATH'])} >/dev/null 2>&1 && "
               + f"export {exported} && "
               + f"export PYTHONPATH=\"$PWD:${{PYTHONPATH:-}}\" && "
               + f"python3 -u -m tests.integration_tests.tools.lite_actions.entrypoint verify --test-id {q(test_case)} --output-dir {q(dest+'/output')} > {q(dest+'/verify.log')} 2>&1; "
               + f"rc=$?; if test $rc -ne 0; then cat {q(dest+'/verify.log')} >> {q(dest+'/run.log')}; fi; exit $rc")
    try:
        return ssh(node["ssh"], command, timeout=timeout, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def run(case: dict, sha: str, stamp: str, *, test_case: str,
        remote_dir: str | None = None, root: Path = ROOT) -> int:
    # stamp is the internal immutable GitHub run identity used for safe process
    # termination; remote_dir is only the operator-facing folder name.
    remote_dir = remote_dir if remote_dir is not None else stamp
    if (not STAMP.fullmatch(stamp) or not SHA.fullmatch(sha)
            or not STAMP.fullmatch(remote_dir) or not SELECTOR.fullmatch(test_case)):
        raise ValueError("unsafe run identifier / commit / execution directory")
    pools = json.loads((root / "config/pools.json").read_text())
    nodes = allocations(case, pools)
    base = f"{DEST_ROOT}/{remote_dir}"
    with tempfile.TemporaryDirectory(prefix="lite-ci-stage-") as tmp:
        archive = model_archive(sha, Path(tmp))
        matches=resolve_request(archive,[{'test_id':test_case}],
                               nodes=len(nodes),ngpu=len(nodes[0]['devices']),max_cases=1)
        if len(matches)!=1:raise ValueError('case selection changed')
        # These values are resolved from the pinned SHA's OverrideDefinitions.
        # The Lite Actions JSON has no testcase-owned environment fields.
        runner={**case['runner'],
                'mode':'single' if len(nodes)==1 else 'distributed',
                'env_vars':matches[0]['env_vars']}
        dirs = {}
        with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = {executor.submit(stage, node, base, archive, sha): node for node in nodes}
            errors = []
            for future in as_completed(futures):
                node = futures[future]
                try:
                    dirs[node["host_id"]] = future.result()
                except Exception as exc:
                    errors.append((node["host_id"], type(exc).__name__))
            if errors:
                raise RuntimeError(f"staging failed: {errors}")
    results = {}
    first_error: tuple[str, int] | None = None
    def terminated(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"dispatcher signal {signum}; cancel owned ranks")

    previous_term = signal.signal(signal.SIGTERM, terminated)
    try:
        with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = {}
            for node in nodes:
                dest = dirs[node["host_id"]]
                cmd = remote_command(node, nodes, runner, dest, stamp,
                                     case["timeout_seconds"] - 120, test_case)
                futures[executor.submit(ssh, node["ssh"], cmd,
                                        timeout=case["timeout_seconds"] - 30,
                                        check=False)] = node
            try:
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        outcome = future.result()
                        rc = outcome.returncode
                    except (OSError, subprocess.TimeoutExpired):
                        rc = 124
                    results[node["host_id"]] = rc
                    print(f"NODE_EXIT: {node['host_id']}={rc}", flush=True)
                    if rc != 0 and first_error is None:
                        first_error = (node["host_id"], rc)
                        # An early rank failure can leave other nodes blocked in
                        # HCCL indefinitely. Stop siblings immediately, do not
                        # wait for their SSH timeouts to finish.
                        pending = [other for other in nodes
                                   if other["host_id"] not in results]
                        if pending:
                            with ThreadPoolExecutor(max_workers=len(pending)) as cleaner:
                                list(cleaner.map(lambda other: cancel(other,
                                    dirs[other["host_id"]], stamp), pending))
            except BaseException:
                # Cleanup begins BEFORE the thread pool waits for SSH clients.
                # Parallel SSH cancellation bounds eight-node cleanup latency.
                with ThreadPoolExecutor(max_workers=len(nodes)) as cleaners:
                    list(cleaners.map(lambda node: cancel(node, dirs[node["host_id"]], stamp), nodes))
                raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    if any(rc for rc in results.values()):
        for node in nodes:
            if results.get(node["host_id"]) in (124, 255):
                cancel(node, dirs[node["host_id"]], stamp)
    rc = first_error[1] if first_error else 0
    if rc == 0:
        rc = verify(nodes, runner, base, min(360, case["timeout_seconds"] // 5), test_case)
    # Dispatcher's log selector chooses a success/error excerpt from this path.
    failed = next((n for n in nodes if first_error and n["host_id"] == first_error[0]), nodes[0])
    path = dirs[failed["host_id"]] + "/run.log"
    print(f"RESULT_HOST: {failed['ssh']}", flush=True)
    print(f"RESULT_LOG: {path}", flush=True)
    print(f"RESULT_EXIT_CODE: {rc}", flush=True)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--test-case")
    parser.add_argument("--request-file")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--remote-dir", required=True,
                        help="Human-readable remote task directory")
    parser.add_argument("--allocation-file", help="Trusted dispatcher allocation JSON")
    args = parser.parse_args()
    case = load_config(ROOT).get(args.case)
    if not case or not case["enabled"] or case["runner"]["kind"] != "ssh":
        raise ValueError("unsupported or disabled SSH pipeline")
    if "resources" in case:
        if not args.allocation_file:
            raise ValueError("dynamic CI requires runtime allocation")
        from ci_core.scheduler import validate_allocation
        runtime = json.loads(Path(args.allocation_file).read_text())
        if set(runtime) != {"allocation"}:
            raise ValueError("unrecognized allocation document")
        validate_allocation(case, json.loads((ROOT / "config/pools.json").read_text()),
                            runtime["allocation"])
        case = {**case, "allocation": runtime["allocation"]}
    elif args.allocation_file:
        raise ValueError("unexpected dynamic allocation for static CI")
    if args.request_file:
        if args.test_case or not args.run_id or not args.run_attempt:
            raise ValueError("manifest dispatch requires run_id and attempt, not test-case")
        request = json.loads(Path(args.request_file).read_text())
        items = parse_request(request, {"id": args.run_id,
                                        "run_attempt": args.run_attempt,
                                        "head_sha": args.sha})
        max_cases = 1 if case["name"] == "a5-64p" else 2
        if len(items) > max_cases:
            raise ValueError("CI request exceeds per-channel runtime budget")
        # Resolve validated per-case environment before running. In particular,
        # after a case exits, preflight uses its toolkit path and never a stale
        # environment from a different suite member.
        from ci_core.registry import resolve_request
        with tempfile.TemporaryDirectory(prefix='lite-ci-resolve-plan-') as tmp:
            archive=model_archive(args.sha,Path(tmp))
            nodes=allocations(case,json.loads((ROOT/'config/pools.json').read_text()))
            resolved=resolve_request(archive,items,nodes=len(nodes),
                                     ngpu=len(nodes[0]['devices']),max_cases=max_cases)
        print("CASE_PLAN " + ",".join(x['test_id'] for x in items),flush=True)
        overall=0
        for index,item in enumerate(items):
            dirname=args.remote_dir if len(items)==1 else f"{args.remote_dir}_case-{index+1:02d}_{item['test_id']}"
            print(f"CASE_START {index+1}/{len(items)} {item['test_id']}",flush=True)
            try:
                rc=run(case,args.sha,args.stamp,test_case=item['test_id'],
                       remote_dir=dirname)
            except Exception as exc:
                print(f"CASE_EXCEPTION {item['test_id']} {type(exc).__name__}",flush=True)
                rc=125
            print(f"CASE_RESULT {item['test_id']} {'PASS' if rc==0 else 'FAIL'} {rc}",flush=True)
            if rc!=0:
                overall=1
            if index+1 < len(items):
                # Even a successful Python exit does not prove the model's NPU
                # child processes are gone. Require an idle preflight before
                # each next test under the same resource lease.
                from ci_core.dispatch import remote_preflight
                pools=json.loads((ROOT/'config/pools.json').read_text())
                try:
                    remote_preflight(case,pools,
                                     ascend_env=resolved[index]['env_vars']['ASCEND_SET_ENV_PATH'])
                except Exception:
                    for remaining in items[index+1:]:
                        print(f"CASE_NOT_RUN {remaining['test_id']}",flush=True)
                    print('CASE_ABORT cleanup unverified',flush=True)
                    return 125
        return overall
    if not args.test_case or not SELECTOR.fullmatch(args.test_case):
        raise ValueError("missing registered CI test")
    return run(case, args.sha, args.stamp, test_case=args.test_case,
               remote_dir=args.remote_dir)


if __name__ == "__main__":
    sys.exit(main())
