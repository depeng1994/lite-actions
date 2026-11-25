"""One trusted SSH execution engine for A3/A5, 1..N nodes.

Only trusted local JSON selects the entrypoint. Training and validation live in
model repository at the exact GitHub SHA. No GitHub credential reaches nodes.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ci_core.config import execution_pools
from ci_core.contract import REPO as MODEL_REPO
from ci_core.registry import SELECTOR
from ci_core.scheduler import validate_allocation

ROOT = Path(__file__).resolve().parents[1]

REPO = f"https://github.com/{MODEL_REPO}.git"
VLLM = MODEL_REPO == "cann-framework/vllm-ascend"
DEST_ROOT = "/mnt/share/ci_tests"
SHA = re.compile(r"[0-9a-f]{40}\Z")
STAMP = re.compile(r"[0-9A-Za-z_-]+\Z")
LEASE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")


def remote_lease_dir(explicit: str | None = None) -> str:
    raw = (
        explicit
        if explicit is not None
        else os.environ.get("LITE_ACTIONS_REMOTE_LEASE_DIR", "")
    ).strip() or "/run/lite-actions-leases"
    if (
        not raw.startswith("/")
        or ".." in Path(raw).parts
        or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", raw)
    ):
        raise ValueError("unsafe remote lease directory")
    return raw


def q(value: object) -> str:
    return shlex.quote(str(value))


def pythonpath_export(node: dict) -> str:
    dependency = q(node["pythonpath"]) + ":" if node.get("pythonpath") else ""
    return f'export PYTHONPATH="$PWD":{dependency}"${{PYTHONPATH:-}}" && '


def ssh(
    host: str, command: str, *, timeout: int = 90, check: bool = True
) -> subprocess.CompletedProcess:
    from ci_core.transport import run

    return run(host, command, timeout=timeout, check=check)


def model_archive(
    sha: str,
    work: Path,
    *,
    source_repo: str = MODEL_REPO,
    provider: str = "github",
    fallback_repo: str | None = None,
    source_pr: int | None = None,
) -> Path:
    """Return a SHA-verified source archive, cached to avoid double Git fetches."""
    from ci_core.contract import trusted_gitcode_repositories, trusted_repositories
    from ci_core.source_cache import metadata_path, seal, verified

    if (
        not SHA.fullmatch(sha)
        or provider not in {"github", "gitcode"}
        or source_repo
        not in (
            trusted_gitcode_repositories()
            if provider == "gitcode"
            else trusted_repositories()
        )
    ):
        raise ValueError("invalid commit SHA / model repository")
    if fallback_repo and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
        fallback_repo,
    ):
        raise ValueError("invalid PR source repository")
    if source_pr is not None and (
        provider != "github" or type(source_pr) is not int or source_pr < 1
    ):
        raise ValueError("invalid GitHub merge reference")
    cache_dir = ROOT / "cache" / "model-archives" / provider / source_repo
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{sha}.tar.gz"
    lock_path = cache_dir / f"{sha}.lock"
    git_env = os.environ.copy()
    if provider == "gitcode":
        token = git_env.get("LITE_ACTIONS_GITCODE_TOKEN")
        if token:
            config_count = int(git_env.get("GIT_CONFIG_COUNT", "0"))
            git_env["GIT_CONFIG_COUNT"] = str(config_count + 1)
            git_env[f"GIT_CONFIG_KEY_{config_count}"] = "http.extraheader"
            git_env[f"GIT_CONFIG_VALUE_{config_count}"] = f"PRIVATE-TOKEN: {token}"
    if provider == "gitcode":
        # GitCode's web front door rejects Git's default User-Agent with 418.
        # Keep the override scoped to GitCode source fetches and pass it through
        # Git's isolated config environment instead of changing host config.
        config_count = int(git_env.get("GIT_CONFIG_COUNT", "0"))
        git_env["GIT_CONFIG_COUNT"] = str(config_count + 1)
        git_env[f"GIT_CONFIG_KEY_{config_count}"] = "http.userAgent"
        git_env[f"GIT_CONFIG_VALUE_{config_count}"] = (
            "Mozilla/5.0 (compatible; lite-actions)"
        )
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if verified(cached, source_repo, sha):
            return cached

        repo = work / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=25)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                f"https://{'gitcode.com' if provider == 'gitcode' else 'github.com'}/{source_repo}.git",
            ],
            check=True,
        )
        try:
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "--depth=1", "-q", "origin", sha],
                check=True,
                timeout=180,
                capture_output=True,
                env=git_env,
            )
        except subprocess.CalledProcessError:
            if source_pr is not None:
                alternate, ref = "origin", f"refs/pull/{source_pr}/merge"
            elif fallback_repo:
                alternate, ref = (
                    f"https://{'gitcode.com' if provider == 'gitcode' else 'github.com'}/{fallback_repo}.git",
                    sha,
                )
            else:
                raise
            # The fallback is the source repository checked by PR admission.
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "fetch",
                    "--depth=1",
                    "-q",
                    alternate,
                    ref,
                ],
                check=True,
                timeout=180,
                capture_output=True,
                env=git_env,
            )
        actual = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "FETCH_HEAD"], text=True
        ).strip()
        if actual != sha:
            raise RuntimeError("Source SHA mismatch")

        # PR export-ignore/export-subst attributes must not change the tested tree.
        (repo / ".git/info/attributes").write_text("* -export-ignore -export-subst\n")
        temp_archive = cache_dir / f".{sha}.{os.getpid()}.tmp"
        try:
            with temp_archive.open("wb") as output:
                pack = subprocess.Popen(
                    ["git", "-C", str(repo), "archive", "--format=tar", sha],
                    stdout=subprocess.PIPE,
                )
                assert pack.stdout is not None
                with gzip.GzipFile(
                    fileobj=output, mode="wb", compresslevel=1, mtime=0
                ) as zipped:
                    shutil.copyfileobj(pack.stdout, zipped)
                pack.stdout.close()
                if pack.wait(timeout=90) != 0:
                    raise RuntimeError("git archive failed")
            if temp_archive.stat().st_size == 0:
                raise RuntimeError("empty source archive")
            os.replace(temp_archive, cached)
            seal(cached, source_repo, sha)
        finally:
            temp_archive.unlink(missing_ok=True)

        # Bound disk growth during iterative development while preserving the
        # current archive. The dispatcher serializes hardware jobs globally.
        archives = sorted(
            cache_dir.glob("[0-9a-f]" * 40 + ".tar.gz"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in archives[20:]:
            if stale != cached:
                stale.unlink(missing_ok=True)
                metadata_path(stale).unlink(missing_ok=True)
        return cached


def allocations(case: dict, pools: dict) -> list[dict]:
    pool = pools[case["pool"]]
    lookup = {node["host_id"]: node for node in pool["nodes"]}
    nodes = []
    for position, allocated in enumerate(case["allocation"]):
        node = lookup[allocated["host_id"]]
        if case.get("docker"):
            from ci_core.config import execution_node

            node = execution_node(node, case["docker"])
        if not node.get("ssh") or not node.get("node_ip"):
            raise ValueError(f"missing SSH/HCCL address for {allocated['host_id']}")
        nodes.append(
            {**node, "devices": list(allocated["devices"]), "position": position}
        )
    if not nodes or len({n["host_id"] for n in nodes}) != len(nodes):
        raise ValueError("empty or duplicate multi-node allocation")
    if len({len(n["devices"]) for n in nodes}) != 1:
        raise ValueError("mixed devices-per-node not supported")
    return nodes


def stage(
    node: dict, base: str, archive: Path, sha: str, test_case: str | None = None
) -> str:
    host = node["ssh"]
    dest = f"{base}/node{node['position']}"
    ssh(host, f"mkdir -p {q(base)} && mkdir {q(dest)} && mkdir {q(dest + '/repo')}")
    from ci_core.transport import run as transport_run

    with archive.open("rb") as source:
        transport_run(
            host, "cat > " + q(dest + "/source.tar.gz"), stdin=source, timeout=180
        )
    import hashlib

    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    ssh(
        host,
        "test "
        + q(checksum)
        + ' = "$(sha256sum '
        + q(dest + "/source.tar.gz")
        + " | cut -d ' ' -f1)\"",
    )
    ssh(
        host,
        f"cd {q(dest)} && tar -xzf source.tar.gz -C repo && "
        f"printf '%s\\n' {q(sha)} > source_sha.txt && "
        "test -f repo/tests/integration_tests/__init__.py",
    )
    print(f"STAGED: {node['host_id']} SHA={sha[:12]} dir={dest}", flush=True)
    return dest


def remote_command(
    node: dict,
    nodes: list[dict],
    runner: dict,
    dest: str,
    stamp: str,
    timeout: int,
    test_case: str,
    *,
    lease_id: str | None = None,
    lease_dir: str | None = None,
) -> str:
    """Remote job owns its own process group; its complete log stays remote."""
    # Generic, fixed model-owned entrypoint; never interpolate GitHub modules.
    entry = (
        "python3 -u -m tests.integration_tests.tools.lite_actions.entrypoint launch --test-id "
        + q(test_case)
        + " --output-dir "
        + q(dest + "/output")
    )
    env = {
        "NODE_IPS": ",".join(x["node_ip"] for x in nodes),
        "NGPU": str(len(node["devices"])),
        "NNODES": str(len(nodes)),
        "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, node["devices"])),
        "PYTHONUNBUFFERED": "1",
        "LITE_CI_JOB_ID": stamp,
        **runner["env_vars"],
    }
    if runner.get("kind") == "npu_smoke":
        position = node.get("position")
        if type(position) is not int or not 0 <= position < len(nodes):
            raise ValueError("invalid allocated node rank")
        # The verified allocation already determines each node's position.
        # Do not depend on container hostname or interface discovery.
        env["NODE_RANK"] = str(position)
    if node.get("network_interface"):
        env["Network_Interface"] = node["network_interface"]
        iface_value = (
            "=" + node["network_interface"] if VLLM else node["network_interface"]
        )
        env["HCCL_SOCKET_IFNAME"] = iface_value
        env["GLOO_SOCKET_IFNAME"] = iface_value
        if node.get("node_ip"):
            env["HCCL_IF_IP"] = node["node_ip"]
    for key in ("LITE_CI_MASTER_PORT", "LITE_CI_HCCL_IF_BASE_PORT"):
        if key in runner:
            port = runner[key]
            if type(port) is not int or not 1024 <= port < 65536:
                raise ValueError("invalid dispatcher-issued rendezvous port")
            env[key] = str(port)
            # The 8P single-node runner inherits the standard environment,
            # whereas the 16P adapter applies LITE_CI_* after recipe defaults.
            env[
                "MASTER_PORT" if key == "LITE_CI_MASTER_PORT" else "HCCL_IF_BASE_PORT"
            ] = str(port)
    for key, hccl_key in (
        ("LITE_CI_HCCL_HOST_PORT_RANGE", "HCCL_HOST_SOCKET_PORT_RANGE"),
        ("LITE_CI_HCCL_NPU_PORT_RANGE", "HCCL_NPU_SOCKET_PORT_RANGE"),
    ):
        if key in runner:
            value = runner[key]
            if not isinstance(value, str) or not re.fullmatch(
                r"[1-9][0-9]{3,4}-[1-9][0-9]{3,4}", value
            ):
                raise ValueError("invalid dispatcher-issued HCCL port range")
            start, end = map(int, value.split("-"))
            if not (1024 <= start <= end <= 65535 and end - start >= 31):
                raise ValueError("unsafe dispatcher-issued HCCL range")
            env[key] = value
            env[hccl_key] = value
    env["CKPT_SAVE_LOAD_PATH"] = dest + "/checkpoints"
    setup = f"source {q(runner['env_vars']['ASCEND_SET_ENV_PATH'])} >/dev/null 2>&1 && "
    # PID file enables bounded cancellation. Python on the dispatcher always
    # validates job identity before killing its *own* remote process group.
    exports = " ".join(f"{key}={q(value)}" for key, value in env.items())
    # Source the selected CANN toolkit first, then apply trusted
    # NODE_IPS / HCCL / device variables so the toolkit cannot override them.
    inner = (
        f"cd {q(dest + '/repo')} && {setup}"
        f"export {exports} && "
        + pythonpath_export(node)
        + f"exec timeout --signal=TERM --kill-after=30s {int(timeout)}s {entry}"
    )
    # The previous `setsid ... & pid=$!` occasionally recorded a wrapper PID
    # instead of the process-group leader. Python's start_new_session creates
    # the exact PGID/PID that we persist and may safely cancel by job marker.
    if lease_id is not None:
        if not LEASE_ID.fullmatch(lease_id):
            raise ValueError("unsafe lease identifier")
        resolved_lease_dir = remote_lease_dir(lease_dir)
        meta_json = json.dumps(
            {
                "lease_id": lease_id,
                "marker": stamp,
                "dest": dest,
                "devices": list(node.get("devices") or []),
                "ports": {
                    k: runner[k]
                    for k in (
                        "LITE_CI_MASTER_PORT",
                        "LITE_CI_HCCL_IF_BASE_PORT",
                        "LITE_CI_HCCL_HOST_PORT_RANGE",
                        "LITE_CI_HCCL_NPU_PORT_RANGE",
                    )
                    if k in runner
                },
            },
            separators=(",", ":"),
        )
        supervisor = """\
import fcntl, json, os, pathlib, subprocess, sys
dest = pathlib.Path(sys.argv[1])
marker = sys.argv[2]
inner = sys.argv[3]
ldir = pathlib.Path(sys.argv[4])
lid = sys.argv[5]
meta = json.loads(sys.argv[6])
ldir.mkdir(parents=True, exist_ok=True)
(dest / 'job.intent').write_text(marker)
log = (dest / 'run.log').open('wb')
lock = (ldir / f'{lid}.lock').open('a+b')
state_file = ldir / f'{lid}.state'
meta_file = ldir / f'{lid}.json'
def wsync(p, text):
    tmp = ldir / f'{p.name}.tmp.{os.getpid()}'
    tmp.write_text(text)
    with tmp.open('r+b') as fh:
        os.fsync(fh.fileno())
    tmp.replace(p)
    dfd = os.open(ldir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
try:
    if state_file.exists() and state_file.read_text().strip() == 'REVOKED':
        log.write(b'REMOTE_LEASE_REVOKED\\n')
        log.flush()
        log.close()
        (dest / 'exit_code.txt').write_text('130\\n')
        print('REMOTE_LEASE_REVOKED:', lid, flush=True)
        sys.exit(130)
    wsync(state_file, 'ACTIVE\\n')
    env = dict(os.environ, LITE_CI_JOB_ID=marker, LITE_CI_LEASE_ID=lid)
    proc = subprocess.Popen(
        ['bash', '-lc', inner],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    (dest / 'job.pgid').write_text(str(proc.pid) + '\\n')
    st = ''
    try:
        st = pathlib.Path(f'/proc/{proc.pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except Exception:
        pass
    meta['pgid'] = proc.pid
    meta['start_time'] = st
    try:
        cg = pathlib.Path('/sys/fs/cgroup/lite-actions-leases') / lid
        cg.mkdir(parents=True, exist_ok=True)
        (cg / 'cgroup.procs').write_text(str(proc.pid))
        meta['cgroup_dir'] = str(cg)
    except OSError:
        pass
    wsync(meta_file, json.dumps(meta, separators=(',', ':')) + '\\n')
finally:
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()
rc = proc.wait()
log.close()
(dest / 'exit_code.txt').write_text(str(rc) + '\\n')
print('NODE_EXIT:', rc, flush=True)
sys.exit(rc)
"""
        return (
            f"cd {q(dest)} || exit 125; "
            f"exec env LITE_CI_JOB_ID={q(stamp)} LITE_CI_LEASE_ID={q(lease_id)} "
            f"python3 -c {q(supervisor)} {q(dest)} {q(stamp)} {q(inner)} "
            f"{q(resolved_lease_dir)} {q(lease_id)} {q(meta_json)}"
        )
    supervisor = (
        "import os,pathlib,subprocess,sys; "
        "dest=pathlib.Path(sys.argv[1]); marker=sys.argv[2]; inner=sys.argv[3]; "
        "(dest/'job.intent').write_text(marker); "
        "log=(dest/'run.log').open('wb'); "
        "env=dict(os.environ,LITE_CI_JOB_ID=marker); "
        "proc=subprocess.Popen(['bash','-lc',inner],stdout=log,"
        "stderr=subprocess.STDOUT,env=env,start_new_session=True); "
        "(dest/'job.pgid').write_text(str(proc.pid)+'\\n'); "
        "rc=proc.wait(); log.close(); "
        "(dest/'exit_code.txt').write_text(str(rc)+'\\n'); "
        "print('NODE_EXIT:',rc,flush=True); sys.exit(rc)"
    )
    return (
        f"cd {q(dest)} || exit 125; "
        f"exec env LITE_CI_JOB_ID={q(stamp)} python3 -c {q(supervisor)} {q(dest)} {q(stamp)} {q(inner)}"
    )


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
leader = int((folder / 'job.pgid').read_text().strip()) if (folder / 'job.pgid').exists() else None
if leader is not None and leader <= 1:
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
if leader is not None and Path(f'/proc/{leader}').exists() and identity(leader) is None:
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
if (folder / 'job.intent').exists() and (folder / 'job.intent').read_text() == marker:
    (folder / 'cleanup_verified.txt').write_text(marker)
"""
    try:
        ssh(node["ssh"], f"python3 -c {q(code)} {q(dest)} {q(stamp)}", timeout=35)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"CLEANUP_UNVERIFIED: {node['host_id']} {type(exc).__name__}", flush=True)


def owned_processes_gone(node, dest, stamp):
    code = r"""
import os
from pathlib import Path
import sys
folder=Path(sys.argv[1]); marker=sys.argv[2]
if os.geteuid()!=0: raise SystemExit('full PID visibility is required for NPU cleanup proof')
if not (folder/'job.intent').exists() or (folder/'job.intent').read_text()!=marker:
    raise SystemExit('launch intent is unverified')
finished=(folder/'exit_code.txt').exists()
cleaned=(folder/'cleanup_verified.txt').exists() and (folder/'cleanup_verified.txt').read_text()==marker
if not (finished or cleaned): raise SystemExit('completion/cleanup acknowledgement missing')
tag=('LITE_CI_JOB_ID='+marker).encode()
for entry in Path('/proc').glob('[0-9]*'):
    try:
        if tag in (entry/'environ').read_bytes().split(b'\x00'):
            raise SystemExit('owned process remains')
    except (FileNotFoundError,ProcessLookupError): pass
"""
    ssh(node["ssh"], f"python3 -c {q(code)} {q(dest)} {q(stamp)}", timeout=30)


def revoke_remote_lease(
    lease: dict,
    *,
    execute=None,
    lease_dir: str | None = None,
    check_npu: bool = True,
    strict: bool = False,
) -> set[str]:
    """Atomically fence future launches and terminate ONLY this lease's processes on all nodes."""
    lease_id = lease.get("lease_id")
    if not isinstance(lease_id, str) or not LEASE_ID.fullmatch(lease_id):
        raise ValueError("unsafe or missing lease identifier")
    nodes = list(lease.get("remote_nodes") or lease.get("nodes") or ())
    if not nodes:
        return set()
    resolved_dir = remote_lease_dir(lease.get("remote_lease_dir") or lease_dir)
    runner = execute if execute is not None else ssh
    grouped: dict[str, dict] = {}
    for node in nodes:
        host_ssh = node.get("ssh")
        if not host_ssh:
            continue
        entry = grouped.setdefault(
            host_ssh,
            {
                "ssh": host_ssh,
                "devices": set(),
                "indexing": node.get("indexing", "card-die"),
            },
        )
        entry["devices"].update(int(d) for d in (node.get("devices") or ()))

    code = """\
import fcntl, json, os, re, shutil, signal, subprocess, sys, time
from pathlib import Path

ldir = Path(sys.argv[1])
lid = sys.argv[2]
devices = set(json.loads(sys.argv[3]))
indexing = sys.argv[4]
do_npu = sys.argv[5] == '1'

ldir.mkdir(parents=True, exist_ok=True)
lock_file = (ldir / f'{lid}.lock').open('a+b')
state_file = ldir / f'{lid}.state'
meta_file = ldir / f'{lid}.json'

def wsync(path, text):
    tmp = ldir / f'{path.name}.tmp.{os.getpid()}'
    tmp.write_text(text)
    with tmp.open('r+b') as fh:
        os.fsync(fh.fileno())
    tmp.replace(path)
    dfd = os.open(ldir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)

def stat_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
        return None

tag = b'LITE_CI_LEASE_ID=' + lid.encode()

def owned_start(pid, *, recorded_pgid=None, recorded_start=''):
    try:
        env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\\x00')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        env = []
    start = stat_start(pid)
    if start is None:
        return None
    if tag in env:
        return start
    if recorded_pgid is not None and pid == recorded_pgid and recorded_start and start == recorded_start:
        return start
    return None

fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
try:
    wsync(state_file, 'REVOKED\\n')
    meta = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            meta = {}
    rec_pgid = meta.get('pgid')
    rec_start = str(meta.get('start_time') or '')
    cg_dir = meta.get('cgroup_dir')
    owned = {}
    if isinstance(rec_pgid, int) and rec_pgid > 1:
        st = owned_start(rec_pgid, recorded_pgid=rec_pgid, recorded_start=rec_start)
        if st is not None:
            owned[rec_pgid] = st
    if isinstance(cg_dir, str) and cg_dir:
        cfile = Path(cg_dir) / 'cgroup.procs'
        if cfile.exists():
            for line in cfile.read_text().splitlines():
                if line.strip().isdecimal():
                    pid = int(line.strip())
                    if pid > 1 and owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start) is not None:
                        owned[pid] = stat_start(pid)
    for entry in os.scandir('/proc'):
        if entry.name.isdecimal():
            pid = int(entry.name)
            if pid > 1:
                st = owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start)
                if st is not None:
                    owned[pid] = st
    for pid, st in list(owned.items()):
        if owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start) == st:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for _ in range(12):
        if not any(owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start) == st for pid, st in owned.items()):
            break
        time.sleep(1)
    for pid, st in list(owned.items()):
        if owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start) == st:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(0.2)
    left = [pid for pid, st in owned.items() if owned_start(pid, recorded_pgid=rec_pgid, recorded_start=rec_start) == st]
    if left:
        raise SystemExit('owned lease processes remain: ' + str(left))
    dest_dir = meta.get('dest')
    marker = meta.get('marker')
    if isinstance(dest_dir, str) and isinstance(marker, str) and dest_dir:
        dp = Path(dest_dir)
        if (dp / 'job.intent').exists() and (dp / 'job.intent').read_text() == marker:
            (dp / 'cleanup_verified.txt').write_text(marker)
    if do_npu and devices and shutil.which('npu-smi'):
        pat = re.compile(r'^\\|\\s*(\\d+)(?:\\s+([01]))?\\s*\\|\\s*(\\d+)\\s*\\|')
        for attempt in range(3):
            proc = subprocess.run(['npu-smi', 'info'], capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise SystemExit('npu-smi info failed')
            busy = []
            for raw in proc.stdout.splitlines():
                m = pat.match(raw)
                if not m:
                    continue
                card = int(m.group(1))
                die = int(m.group(2) or 0)
                pid = int(m.group(3))
                chip = card * 2 + die if (indexing == 'card-die' and m.group(2) is not None) else card
                if chip in devices and pid > 1:
                    busy.append((chip, pid))
            if not busy:
                break
            if attempt < 2:
                time.sleep(2)
            else:
                raise SystemExit('leased npu remains occupied: ' + str(busy))
    print('REVOKED:' + lid, flush=True)
finally:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()
"""

    def revoke_one(target: dict) -> tuple[str, bool, str]:
        host_ssh = target["ssh"]
        cmd = (
            f"python3 -c {q(code)} {q(resolved_dir)} {q(lease_id)} "
            f"{q(json.dumps(sorted(target['devices'])))} {q(target['indexing'])} "
            f"{'1' if check_npu else '0'}"
        )
        try:
            res = runner(host_ssh, cmd, timeout=45, check=False)
            out = (
                (getattr(res, "stdout", "") or "")
                + "\n"
                + (getattr(res, "stderr", "") or "")
            )
            ok = getattr(res, "returncode", 1) == 0 and f"REVOKED:{lease_id}" in out
            return host_ssh, ok, out.strip()
        except Exception as exc:
            return host_ssh, False, str(exc)

    revoked: set[str] = set()
    failed: dict[str, str] = {}
    targets = list(grouped.values())
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for host_ssh, ok, err in pool.map(revoke_one, targets):
            if ok:
                revoked.add(host_ssh)
            else:
                failed[host_ssh] = err
    if strict and failed:
        raise RuntimeError("remote lease revoke failed on " + ",".join(sorted(failed)))
    return revoked


def verify(
    nodes: list[dict], runner: dict, base: str, timeout: int, test_case: str
) -> int:
    if runner["mode"] != "distributed":
        return 0
    node = nodes[0]
    dest = base + "/node0"
    exports = runner["env_vars"]
    exported = " ".join(f"{key}={q(value)}" for key, value in exports.items())
    command = (
        f"cd {q(dest + '/repo')} && "
        + f"source {q(runner['env_vars']['ASCEND_SET_ENV_PATH'])} >/dev/null 2>&1 && "
        + f"export {exported} && "
        + pythonpath_export(node)
        + f"python3 -u -m tests.integration_tests.tools.lite_actions.entrypoint verify --test-id {q(test_case)} --output-dir {q(dest + '/output')} > {q(dest + '/verify.log')} 2>&1; "
        + f"rc=$?; if test $rc -ne 0; then cat {q(dest + '/verify.log')} >> {q(dest + '/run.log')}; fi; exit $rc"
    )
    try:
        return ssh(node["ssh"], command, timeout=timeout, check=False).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 124


def run(
    case: dict,
    sha: str,
    stamp: str,
    *,
    test_case: str,
    remote_dir: str | None = None,
    root: Path = ROOT,
) -> int:
    # stamp is the internal immutable GitHub run identity used for safe process
    # termination; remote_dir is only the operator-facing folder name.
    remote_dir = remote_dir if remote_dir is not None else stamp
    if (
        not STAMP.fullmatch(stamp)
        or not SHA.fullmatch(sha)
        or not STAMP.fullmatch(remote_dir)
        or not SELECTOR.fullmatch(test_case)
    ):
        raise ValueError("unsafe run identifier / commit / execution directory")
    pools = execution_pools(root, case)
    validate_allocation(case, pools, case["allocation"], include_paused=True)
    nodes = allocations(case, pools)
    base = f"{DEST_ROOT}/{remote_dir}"
    with tempfile.TemporaryDirectory(prefix="lite-ci-stage-") as tmp:
        stage_archive = (
            case.get("_ci_stage_archive")
            or os.environ.get("LITE_ACTIONS_STAGE_ARCHIVE_PATH", "").strip()
        )
        if stage_archive:
            archive = Path(stage_archive)
            if not archive.is_file():
                raise ValueError("missing staged source archive")
        else:
            provider = os.environ.get("LITE_ACTIONS_SOURCE_PROVIDER", "github")
            source_repo = os.environ.get("LITE_ACTIONS_SOURCE_REPO", MODEL_REPO)
            archive = model_archive(
                sha,
                Path(tmp),
                source_repo=source_repo,
                provider=provider,
                fallback_repo=os.environ.get("LITE_ACTIONS_SOURCE_FALLBACK_REPO")
                or None,
                source_pr=(
                    case["native_source"]["pr_number"]
                    if case.get("native_source", {}).get("mode") == "merge_result"
                    else None
                ),
            )
        runner = {
            "kind": case.get("kind"),
            "mode": "single" if len(nodes) == 1 else "distributed",
            "env_vars": case["verified_env"],
            "LITE_CI_MASTER_PORT": case.get("master_port", 16000),
            "LITE_CI_HCCL_IF_BASE_PORT": case.get("hccl_if_base_port", 20000),
            "LITE_CI_HCCL_HOST_PORT_RANGE": case.get(
                "hccl_host_port_range", "24000-24063"
            ),
            "LITE_CI_HCCL_NPU_PORT_RANGE": case.get(
                "hccl_npu_port_range", "28000-28063"
            ),
        }
        dirs = {}
        with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = {
                executor.submit(stage, node, base, archive, sha, test_case): node
                for node in nodes
            }
            errors = []
            for future in as_completed(futures):
                node = futures[future]
                try:
                    dirs[node["host_id"]] = future.result()
                except Exception as exc:
                    errors.append((node["host_id"], type(exc).__name__))
            if errors:
                raise RuntimeError(f"staging failed: {errors}")
    if case.get("native_source"):
        from ci_core.config import deployment
        from ci_core.egress_policy import Egress
        from ci_core.github import GitHubClient, authorization
        from ci_core.github_source import check_current

        worker, cfg = deployment()
        check_current(
            GitHubClient(authorization(), MODEL_REPO, policy=Egress(worker, cfg)),
            case["native_source"],
            check_run=True,
        )
    # Preserve a trusted log locator if cancellation interrupts the rank wait.
    # A normal multi-node failure replaces this with the failing node below.
    print(f"RESULT_HOST: {nodes[0]['ssh']}", flush=True)
    print(f"RESULT_LOG: {dirs[nodes[0]['host_id']]}/run.log", flush=True)
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
                cmd = remote_command(
                    node,
                    nodes,
                    runner,
                    dest,
                    stamp,
                    case["timeout_seconds"] - 120,
                    test_case,
                    lease_id=case.get("lease_id"),
                    lease_dir=case.get("remote_lease_dir"),
                )
                futures[
                    executor.submit(
                        ssh,
                        node["ssh"],
                        cmd,
                        timeout=case["timeout_seconds"] - 30,
                        check=False,
                    )
                ] = node
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
                        pending = [
                            other for other in nodes if other["host_id"] not in results
                        ]
                        if pending:
                            with ThreadPoolExecutor(
                                max_workers=len(pending)
                            ) as cleaner:
                                list(
                                    cleaner.map(
                                        lambda other: cancel(
                                            other, dirs[other["host_id"]], stamp
                                        ),
                                        pending,
                                    )
                                )
            except BaseException:
                # Cleanup begins BEFORE the thread pool waits for SSH clients.
                # Parallel SSH cancellation bounds eight-node cleanup latency.
                with ThreadPoolExecutor(max_workers=len(nodes)) as cleaners:
                    list(
                        cleaners.map(
                            lambda node: cancel(node, dirs[node["host_id"]], stamp),
                            nodes,
                        )
                    )
                raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    if any(rc for rc in results.values()):
        for node in nodes:
            if results.get(node["host_id"]) in (124, 255):
                cancel(node, dirs[node["host_id"]], stamp)
    rc = first_error[1] if first_error else 0
    if rc == 0:
        rc = verify(
            nodes, runner, base, min(360, case["timeout_seconds"] // 5), test_case
        )
    # Dispatcher's log selector chooses a success/error excerpt from this path.
    failed = next(
        (n for n in nodes if first_error and n["host_id"] == first_error[0]), nodes[0]
    )
    path = dirs[failed["host_id"]] + "/run.log"
    print(f"RESULT_HOST: {failed['ssh']}", flush=True)
    print(f"RESULT_LOG: {path}", flush=True)
    print(f"RESULT_EXIT_CODE: {rc}", flush=True)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-file", required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--lease-id", default=None)
    parser.add_argument("--lease-dir", default=None)
    args = parser.parse_args()
    case = json.loads(Path(args.case_file).read_text())
    if args.lease_id:
        case["lease_id"] = args.lease_id
    if args.lease_dir:
        case["remote_lease_dir"] = args.lease_dir
    if not case.get("enabled") or not SELECTOR.fullmatch(args.test_id):
        raise ValueError("disabled or invalid test")
    from ci_core.resources import port_profile

    validate_allocation(
        case, execution_pools(ROOT, case), case["allocation"], include_paused=True
    )
    approved = port_profile(case["port_slot"])
    if any(case.get(field) != value for field, value in approved.items()):
        raise ValueError("allocation/port lease mismatch")
    print("CASE_START " + args.test_id, flush=True)
    rc = run(
        case, args.sha, args.stamp, test_case=args.test_id, remote_dir=args.remote_dir
    )
    print(
        f"CASE_RESULT {args.test_id} {'PASS' if rc == 0 else 'FAIL'} {rc}", flush=True
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
