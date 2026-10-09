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

ROOT = Path(__file__).resolve().parents[1]
REPO = "git@github.com:depeng1994/torchtitan-npu.git"
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
                   stamp: str, timeout: int) -> str:
    """Remote job owns its own process group; its complete log stays remote."""
    module = runner["module"]  # strictly validated in ci_core.config
    entry = f"python3 -u -m {module} "
    if runner["mode"] == "distributed":
        entry += f"launch {q(dest+'/output')}"
    else:
        entry += q(dest + "/output")
    env = {
        "NODE_IPS": ",".join(x["node_ip"] for x in nodes),
        "NGPU": str(len(node["devices"])),
        "NNODES": str(len(nodes)),
        "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, node["devices"])),
        "PYTHONUNBUFFERED": "1",
        "LITE_CI_JOB_ID": stamp,
        **runner.get("env", {}),
    }
    if node.get("network_interface"):
        env["Network_Interface"] = node["network_interface"]
    if node.get("hf_assets_path"):
        env["HF_ASSETS_PATH"] = node["hf_assets_path"]
    if node.get("ckpt_init_path"):
        env["CKPT_INIT_LOAD_PATH"] = node["ckpt_init_path"]
    env["CKPT_SAVE_LOAD_PATH"] = dest + "/checkpoints"
    setup = (f"source {q(node['ascend_env'])} >/dev/null 2>&1 && "
             if node.get("ascend_env") else "")
    # PID file enables bounded cancellation. Python on the dispatcher always
    # validates job identity before killing its *own* remote process group.
    exports = " ".join(f"{key}={q(value)}" for key, value in env.items())
    inner = (f"cd {q(dest+'/repo')} && {setup}"
             f"export PYTHONPATH=\"$PWD:${{PYTHONPATH:-}}\" && "
             f"exec timeout --signal=TERM --kill-after=30s {int(timeout)}s {entry}")
    return (
        f"cd {q(dest)} || exit 125; "
        f"setsid env {exports} bash -lc {q(inner)} > {q(dest+'/run.log')} 2>&1 & "
        f"pid=$!; printf '%s\\n' \"$pid\" > {q(dest+'/job.pgid')}; "
        f"wait \"$pid\"; rc=$?; "
        f"printf '%s\\n' \"$rc\" > {q(dest+'/exit_code.txt')}; "
        f"printf 'NODE_EXIT: %s\\n' \"$rc\"; exit \"$rc\""
    )


def cancel(node: dict, dest: str, stamp: str) -> None:
    """Terminate only a verified job-owned process group, never unknown PIDs."""
    code = (
        "import os,pathlib,signal,sys\n"
        "folder=pathlib.Path(sys.argv[1]); marker=sys.argv[2]\n"
        "pid=int((folder/'job.pgid').read_text().strip())\n"
        "if pid <= 1: raise SystemExit('invalid pgid')\n"
        "environ=pathlib.Path(f'/proc/{pid}/environ').read_bytes()\n"
        "if b'LITE_CI_JOB_ID='+marker.encode()+b'\\x00' not in environ: "
        "raise SystemExit('refuse unrelated process')\n"
        "if os.getpgid(pid)!=pid: raise SystemExit('not group leader')\n"
        "os.killpg(pid,signal.SIGTERM)\n"
    )
    try:
        ssh(node["ssh"], f"python3 -c {q(code)} {q(dest)} {q(stamp)}", timeout=30)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"CLEANUP_UNVERIFIED: {node['host_id']} {type(exc).__name__}", flush=True)


def verify(nodes: list[dict], runner: dict, base: str, timeout: int) -> int:
    if runner["mode"] != "distributed":
        return 0
    node = nodes[0]
    dest = base + "/node0"
    module = runner["module"]
    command = (f"cd {q(dest+'/repo')} && "
               + (f"source {q(node['ascend_env'])} >/dev/null 2>&1 && " if node.get("ascend_env") else "")
               + f"export PYTHONPATH=\"$PWD:${{PYTHONPATH:-}}\" && "
               + f"python3 -u -m {module} verify {q(dest+'/output')} > {q(dest+'/verify.log')} 2>&1; "
               + f"rc=$?; if test $rc -ne 0; then cat {q(dest+'/verify.log')} >> {q(dest+'/run.log')}; fi; exit $rc")
    try:
        return ssh(node["ssh"], command, timeout=timeout, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def run(case: dict, sha: str, stamp: str, *, root: Path = ROOT) -> int:
    if not STAMP.fullmatch(stamp) or not SHA.fullmatch(sha):
        raise ValueError("unsafe run identifier / commit")
    pools = json.loads((root / "config/pools.json").read_text())
    nodes = allocations(case, pools)
    runner = case["runner"]
    base = f"{DEST_ROOT}/{stamp}"
    with tempfile.TemporaryDirectory(prefix="lite-ci-stage-") as tmp:
        archive = model_archive(sha, Path(tmp))
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
    def terminated(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"dispatcher signal {signum}; cancel owned ranks")

    previous_term = signal.signal(signal.SIGTERM, terminated)
    try:
        with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = {}
            for node in nodes:
                dest = dirs[node["host_id"]]
                cmd = remote_command(node, nodes, runner, dest, stamp, case["timeout_seconds"] - 120)
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
    rc = next((results[n["host_id"]] for n in nodes if results[n["host_id"]]), 0)
    if rc == 0:
        rc = verify(nodes, runner, base, min(360, case["timeout_seconds"] // 5))
    # Dispatcher's log selector chooses a success/error excerpt from this path.
    failed = next((n for n in nodes if results[n["host_id"]]), nodes[0])
    path = dirs[failed["host_id"]] + "/run.log"
    print(f"RESULT_HOST: {failed['ssh']}", flush=True)
    print(f"RESULT_LOG: {path}", flush=True)
    print(f"RESULT_EXIT_CODE: {rc}", flush=True)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args()
    case = load_config(ROOT).get(args.case)
    if not case or not case["enabled"] or case["runner"]["kind"] != "ssh":
        raise ValueError("unsupported or disabled SSH pipeline")
    return run(case, args.sha, args.stamp)


if __name__ == "__main__":
    sys.exit(main())
