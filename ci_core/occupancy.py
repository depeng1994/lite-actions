"""Verified host-PID reclamation for operator-approved auto-occupy/vllm.

The A3 npu-smi first PID column is a HOST namespace PID. The optional
'Process id in container' column must NEVER be used as host PID.
Always use the separate HOST SSH entry, not the execution Docker shell.
"""
from __future__ import annotations

import logging
import re
import shlex

from ci_core.dispatch import CHIP_PROCESS
from ci_core.ssh_runner import ssh


def selected_pids(output: str, devices: list[int], *, indexing: str) -> list[int]:
    if "Process id in container" not in output:
        raise ValueError("NPU process table missing")
    if indexing not in {"card", "card-die"}:
        raise ValueError("unrecognized indexing mode")
    selected = set(devices)
    pids = set()
    for line in output.split("Process id in container")[-1].splitlines():
        match = CHIP_PROCESS.match(line)
        if match:
            card, die, pid = map(int, match.groups())
            chip = card * 2 + die if indexing == "card-die" else card
            if chip in selected:
                if pid <= 1:
                    raise ValueError("invalid external process PID")
                pids.add(pid)
        elif re.match(r"^\|\s*\d+\s+\d+\s*\|", line):
            raise ValueError("NPU process row not recognized")
    if len(pids) > 64:
        raise ValueError("too many PID occupants to classify")
    return sorted(pids)


# Every remote operation re-reads PID/starttime and refuses an unknown process.
# No watchdog/service stop/restart; each PID is addressed independently.
_REMOTE_TERMINATE = r"""
import os,re,signal,sys,time
pid=int(sys.argv[1])
if pid<=1 or pid==os.getpid(): raise SystemExit('REFUSED protected PID')
def proc(pid):
    folder=f'/proc/{pid}'
    try:
        stat=open(folder+'/stat').read().rsplit(')',1)[1].split()
        # A terminated zombie has released NPU contexts; do not mistake the
        # parent's unreaped /proc entry for a still-running workload.
        if stat[0]=='Z': return None
        raw=open(folder+'/cmdline','rb').read().split(b'\0')
        argv=tuple(x.decode('utf-8','replace') for x in raw if x)
        return (stat[19],int(stat[1]),argv)
    except (FileNotFoundError,PermissionError,ValueError,IndexError):
        return None
def direct_class(argv):
    if not argv: return None
    exe=os.path.basename(argv[0]).lower()
    if exe in ('auto-occupy','auto_occupy','auto-occupy.sh','auto_occupy.sh'):
        return 'auto-occupy'
    if exe in ('vllm','vllm-server','vllm_server'):
        return 'vllm'
    if exe.startswith(('python','python3','pypy')) or exe=='uv':
        args=list(argv[1:5])
        if '-m' in args:
            i=args.index('-m')
            if i+1<len(args):
                module=args[i+1].lower()
                if module=='vllm' or module.startswith('vllm.'): return 'vllm'
                if module in ('auto_occupy','auto-occupy'): return 'auto-occupy'
        # Only an executed SCRIPT positional argument, never a model/data
        # path appearing later in the command's arbitrary flags.
        scripts=[os.path.basename(x).lower() for x in args[:2] if x.endswith(('.py','.sh'))]
        if any(x in ('auto_occupy.py','auto-occupy.py','auto-occupy.sh') for x in scripts):
            return 'auto-occupy'
        # Explicit trusted worker entrypoint: not an arbitrary flag/data path.
        if any(os.path.basename(x).lower() == 'auto_occupy_worker.py'
               and os.path.basename(os.path.dirname(x)).lower() in ('auto-occupy','auto_occupy')
               for x in args[:2]):
            return 'auto-occupy'
        if any(x in ('vllm.py','vllm_server.py') for x in scripts):
            return 'vllm'
    return None
def classify(pid):
    seen=set()
    for depth in range(6):
        if pid in seen or pid<=1: break
        seen.add(pid)
        value=proc(pid)
        if value is None: break
        found=direct_class(value[2])
        if found: return found
        pid=value[1]
    return None
before=proc(pid)
if before is None:
    raise SystemExit('REFUSED disappeared or unreadable')
category=classify(pid)
if category not in ('auto-occupy','vllm'):
    raise SystemExit('REFUSED not approved class')
# Detect PID reuse even between discovery and this kill.
if proc(pid)!=before: raise SystemExit('REFUSED changed process')
os.kill(pid,signal.SIGTERM)
for _ in range(20):
    time.sleep(.15)
    value=proc(pid)
    if value is None or value[0]!=before[0]:
        print('CLEARED '+category)
        raise SystemExit(0)
# PID-specific escalation only after repeating all identity checks.
if proc(pid)!=before or classify(pid)!=category:
    raise SystemExit('REFUSED changed before escalation')
os.kill(pid,signal.SIGKILL)
print('SIGNALLED '+category)
"""


def reclaim_candidate(case: dict, pools: dict, *, ascend_env: str | None) -> bool:
    pool = pools[case["pool"]]
    mode = pool["process_indexing"]
    nodes = {n["host_id"]: n for n in pool["nodes"]}
    any_cleared = False
    for assigned in case["allocation"]:
        node = nodes[assigned["host_id"]]
        # First broadcast target is the verified physical HOST shell. The
        # container /proc PID namespace may have the same PID numerically but
        # refer to an unrelated process; NEVER run host PID termination there.
        targets = node.get("broadcast_ssh", [])
        if not isinstance(targets, list) or not targets:
            raise ValueError("host SSH must be explicitly configured")
        host = targets[0]
        if not isinstance(host, str) or not host or host == node.get("ssh"):
            raise ValueError("host PID cleanup cannot use Docker SSH")
        info = ssh(host, "npu-smi info", timeout=45, check=True)
        for pid in selected_pids(info.stdout, assigned["devices"], indexing=mode):
            try:
                outcome = ssh(host, "python3 -c " + shlex.quote(_REMOTE_TERMINATE)
                              + " " + str(pid), timeout=20, check=False)
            except Exception as exc:
                logging.warning("external occupancy PID %d inspection failed: %s",
                                pid, type(exc).__name__)
                continue
            if outcome.returncode == 0 and (
                    "CLEARED " in outcome.stdout or "SIGNALLED " in outcome.stdout):
                logging.warning("reclaimed approved NPU occupant host=%s pid=%d status=%s",
                                assigned["host_id"], pid, outcome.stdout.strip())
                any_cleared = True
            else:
                logging.info("retained external occupant host=%s pid=%d (not approved)",
                             assigned["host_id"], pid)
    return any_cleared
