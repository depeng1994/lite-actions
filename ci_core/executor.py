"""Execute one pinned model case while Router holds its complete resource lease."""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ci_core.broadcast import broadcast_job
from ci_core.contract import GITCODE_REPOS, SUPPORTED_REPOS, TORCHTITAN_REPO, VLLM_REPO
from ci_core.logs import metadata, select_remote
from ci_core.registry import resolve_test

BASE = Path(__file__).resolve().parents[1]
CHIP_PROCESS = re.compile(r"^\|\s*(\d+)\s*([01])\s*\|\s*(\d+)\s*\|")
MARKER_START = "RESULT_LAST_20_LINES_BEGIN"
MARKER_END = "RESULT_LAST_20_LINES_END"


class ExternalNpuOccupancy(RuntimeError):
    """External activity detected before any CI process has started."""


@dataclass
class Execution:
    exit_code: int
    tail: list[str]
    log: Path
    release_verified: bool = True


def _local_run_time(run: dict) -> dt.datetime:
    """GitHub timestamps are UTC; paths use Beijing time (UTC+08:00)."""
    timestamp = dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    return timestamp.astimezone(dt.timezone(dt.timedelta(hours=8)))


def _case_instance_token(run: dict) -> str | None:
    """Bounded identity for one instance, retaining legacy paths when absent."""
    if "_ci_case_id" not in run:
        return None
    case_id = run["_ci_case_id"]
    test_id = run.get("_ci_test_id")
    if not isinstance(case_id, str) or not isinstance(test_id, str):
        raise ValueError("invalid CI case identity")
    match = re.fullmatch(r"case_([0-9]{3})_([a-z][a-z0-9_]{0,79})", case_id)
    if not match or int(match.group(1)) > 255 or match.group(2) != test_id:
        raise ValueError("invalid CI case identity")
    provider = run.get("_ci_provider", "github")
    repo = run.get("_ci_repo", TORCHTITAN_REPO)
    if provider not in {"github", "gitcode"} or repo not in (
        GITCODE_REPOS if provider == "gitcode" else SUPPORTED_REPOS
    ):
        raise ValueError("untrusted CI case source")
    run_id = str(run["id"])
    attempt = run.get("run_attempt", 1)
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id)
        or type(attempt) is not int
        or attempt < 1
    ):
        raise ValueError("invalid CI run identity")
    identity = [provider, repo, run_id, attempt, case_id]
    dispatch = run.get("_ci_dispatch_id")
    if dispatch is not None:
        if not isinstance(dispatch, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", dispatch
        ):
            raise ValueError("invalid GitCode dispatch identity")
        identity.append(dispatch)
    payload = json.dumps(identity, separators=(",", ":")).encode()
    return f"{match.group(1)}_{hashlib.sha256(payload).hexdigest()}"


def task_dir(run: dict, root: Path) -> Path:
    """Keep globally unique run ID in dispatcher logs/state for correlation."""
    local = _local_run_time(run)
    suffix = ""
    if "_ci_test_id" in run:
        from ci_core.registry import SELECTOR

        test_id = run["_ci_test_id"]
        if not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id):
            raise ValueError("invalid CI task Test ID")
        suffix = f"_test-{test_id}"
    source_repo = run.get("_ci_repo", TORCHTITAN_REPO)
    provider = run.get("_ci_provider", "github")
    if source_repo not in (GITCODE_REPOS if provider == "gitcode" else SUPPORTED_REPOS):
        raise ValueError("untrusted model repo")
    logs = root / "logs/runs"
    if source_repo == VLLM_REPO:
        logs = logs / "vllm-ascend"
    if provider == "gitcode":
        logs = logs / "gitcode" / source_repo
    run_id = str(run["id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("invalid run ID")
    instance = _case_instance_token(run)
    if instance is not None:
        # SHA-256 includes the full run identity even when the display part is
        # truncated, so maximum-length IDs remain safe as filesystem names.
        return logs / f"{local:%Y-%m-%d_%H-%M-%S}_run-{run_id[:24]}_case-{instance}"
    return logs / (
        f"{local:%Y-%m-%d_%H-%M-%S}_run-{run_id}"
        f"_attempt-{int(run.get('run_attempt', 1))}{suffix}"
    )


def remote_task_name(run: dict) -> str:
    """Human-readable execution-node folder: time + workflow + run_number.

    GitHub's workflow run_number is the #6 shown in Actions, not the global
    run ID and not run_attempt. Append attempt only for reruns to avoid a
    collision with the original execution's immutable training logs.
    """
    name = run.get("name")
    instance = _case_instance_token(run)
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 256
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("invalid GitHub workflow name for execution directory")
    safe_name = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-") or "workflow"
    if safe_name != name or len(safe_name) > 64:
        safe_name = f"{safe_name[:48].rstrip('-')}-{hashlib.sha256(name.encode()).hexdigest()[:12]}"
    name = safe_name
    number = run.get("run_number")
    attempt = run.get("run_attempt", 1)
    if type(number) is not int or number < 1:
        raise ValueError("GitHub run_number must be a positive integer")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("GitHub run_attempt must be a positive integer")
    local = _local_run_time(run)
    suffix = f"_attempt-{attempt}" if attempt > 1 else ""
    if "_ci_test_id" in run:
        from ci_core.registry import SELECTOR

        test_id = run["_ci_test_id"]
        if not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id):
            raise ValueError("invalid CI test ID")
        suffix += f"_test-{test_id}"
    prefix = (
        "gitcode_"
        if run.get("_ci_provider") == "gitcode"
        else ("vllm_" if run.get("_ci_repo") == VLLM_REPO else "")
    )
    if run.get("_ci_provider") == "gitcode":
        repo = run.get("_ci_repo", "").replace("/", "_")
        run_id = str(run["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", repo
        ):
            raise ValueError("invalid GitCode remote identity")
        if instance is not None:
            return f"{prefix}{repo[:32]}_{run_id[:24]}_case-{instance}"
        return f"{prefix}{repo}_{run_id}_attempt-{attempt}{suffix}"
    if instance is not None:
        return f"{prefix}{local:%Y-%m-%d_%H-%M-%S}_run_{name[:32]}_{str(number)[:20]}_case-{instance}"
    if len(name) > 64:
        name = f"{name[:48]}-{hashlib.sha256(name.encode()).hexdigest()[:12]}"
    return f"{prefix}{local:%Y-%m-%d_%H-%M-%S}_run_{name}_{number}{suffix}"


def _verified_inventory(output, indexing, expected_devices):
    lines = output.splitlines()
    header = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("|")
            and " ".join(line.split("|")[1].split()) == "NPU Name"
        ),
        None,
    )
    if header is None or header + 1 >= len(lines):
        raise ValueError("NPU hardware inventory header missing")
    chip_header = lines[header + 1]
    if (
        not chip_header.startswith("|")
        or " ".join(chip_header.split("|")[1].split()) != "Chip Phy-ID"
    ):
        raise ValueError("NPU hardware mapping header missing")
    end = next(
        (
            i
            for i in range(header + 2, len(lines))
            if "Process id in container" in lines[i]
        ),
        None,
    )
    if end is None:
        raise ValueError("NPU process table missing")
    section = [line.strip() for line in lines[header + 2 : end] if line.strip()]
    if (
        len(section) < 4
        or not re.fullmatch(r"\+={3,}(?:\+={3,})*\+", section[-2])
        or not re.fullmatch(r"\+-{3,}(?:\+-{3,})*\+", section[-1])
    ):
        raise ValueError("NPU hardware inventory boundary missing")
    data = [line for line in section if not re.fullmatch(r"[+=-]+", line)]
    if len(data) % 2:
        raise ValueError("incomplete NPU hardware mapping")
    mapping, buses = {}, set()
    for i in range(0, len(data), 2):
        first, second = data[i : i + 2]
        if not (
            first.startswith("|")
            and first.endswith("|")
            and second.startswith("|")
            and second.endswith("|")
        ):
            raise ValueError("unrecognized NPU hardware row")
        npu_cells = [cell.strip() for cell in first[1:-1].split("|")]
        chip_cells = [cell.strip() for cell in second[1:-1].split("|")]
        npu = re.fullmatch(r"(\d+)\s+([A-Za-z][A-Za-z0-9_.-]*)", npu_cells[0])
        chip = re.fullmatch(r"(\d+)\s+(\d+)", chip_cells[0])
        if (
            len(npu_cells) < 2
            or len(chip_cells) < 2
            or not npu
            or not chip
            or npu_cells[1] != "OK"
            or not re.fullmatch(
                r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", chip_cells[1]
            )
        ):
            raise ValueError("unrecognized NPU hardware mapping")
        card, die, phy = int(npu[1]), int(chip[1]), int(chip[2])
        key = (card, die)
        if key in mapping or phy in mapping.values() or chip_cells[1].lower() in buses:
            raise ValueError("duplicate NPU hardware mapping")
        if (
            indexing == "card-die" and (die not in (0, 1) or phy != card * 2 + die)
        ) or (indexing == "card" and (die != 0 or phy != card)):
            raise ValueError("unverified NPU device mapping")
        mapping[key] = phy
        buses.add(chip_cells[1].lower())
    if not mapping or set(mapping.values()) != set(expected_devices):
        raise ValueError("NPU hardware inventory differs from configured devices")
    if indexing == "card-die" and any(
        (card, die) not in mapping for card, _ in mapping for die in (0, 1)
    ):
        raise ValueError("incomplete NPU chip mapping")


def _process_rows(
    output: str,
    devices: list[int],
    *,
    indexing: str,
    require_exclusive=True,
    host_pids=False,
    coverage_devices=None,
    require_end=False,
    require_coverage=True,
) -> list[tuple[int, int, str]]:
    """Return (chip, PID visible in the kill namespace, container PID evidence).

    A5 "card" mode reports the *host* PID in column two, regardless of
    whether the process belongs to the bound CI container. The last column
    may be NA or a PID from a different container's namespace; never kill it.
    Preserve the established A3 card-die PID and chip mapping.
    """
    if "Process id in container" not in output:
        raise ValueError("npu-smi process table absent")
    if indexing not in ("card", "card-die"):
        raise ValueError("unknown NPU process index mapping")
    if host_pids:
        _verified_inventory(output, indexing, coverage_devices)

    selected = set(devices)
    rows: list[tuple[int, int, str]] = []
    has_process_data = False
    covered_cards = set()
    lines = output.split("Process id in container", 1)[1].splitlines()[1:]
    if require_end:
        nonempty = [line.strip() for line in lines if line.strip()]
        if not nonempty or not re.fullmatch(
            r"\+[=-]{3,}(?:\+[=-]{3,})*\+", nonempty[-1]
        ):
            if not require_exclusive:
                raise ExternalNpuOccupancy("NPU process table has no verified end")
            raise ValueError("NPU process table has no verified end")
    if indexing == "card-die" and not host_pids:
        for line in lines:
            stripped = line.strip()
            if not stripped or re.fullmatch(r"[+=-]+", stripped):
                continue
            empty = re.fullmatch(
                r"\|\s*No running processes found in NPU (\d+)[^|]*\|", stripped
            )
            if empty:
                has_process_data = True
                covered_cards.add(int(empty[1]))
                continue
            match = CHIP_PROCESS.match(line)
            if match:
                has_process_data = True
                covered_cards.add(int(match.group(1)))
                chip = int(match.group(1)) * 2 + int(match.group(2))
                pid = int(match.group(3))
                rows.append((chip, pid, None))
            elif not require_exclusive:
                raise ExternalNpuOccupancy("NPU process row cannot be resolved")
            else:
                raise ValueError("unrecognized NPU process row")
    else:
        # The A3 host table uses the first cell for both card and chip, and
        # its second cell is the host PID. Never infer it from a container PID.
        if host_pids:
            header = next(
                line
                for line in output.splitlines()
                if "Process id in container" in line
            )
            columns = [
                " ".join(x.split()) for x in header.strip().strip("|").split("|")
            ]
            if columns != [
                "NPU Chip" if indexing == "card-die" else "NPU ID",
                "Process id",
                "Process name",
                "Process memory(MB)",
                "Process id in container",
            ]:
                raise ValueError("unrecognized host PID table")
        # Ignore the remainder of the header line; all following data rows
        # must match the five-column table. Unknown rows are not idle.
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if re.fullmatch(r"[+=-]+", line):
                continue
            empty = re.fullmatch(
                r"\|\s*No running processes found in NPU (\d+)[^|]*\|", line
            )
            if empty:
                has_process_data = True
                covered_cards.add(int(empty[1]))
                continue
            if not (line.startswith("|") and line.endswith("|")):
                raise ValueError("unrecognized NPU process row")
            cells = [part.strip() for part in line[1:-1].split("|")]
            chip = cells[0].split()
            valid_chip = (
                len(chip) == 2
                and re.fullmatch(r"[0-9]+", chip[0])
                and chip[1] in ("0", "1")
                if host_pids and indexing == "card-die"
                else re.fullmatch(r"[0-9]+", cells[0])
            )
            if (
                len(cells) != 5
                or not valid_chip
                or not re.fullmatch(r"[0-9]+", cells[1])
                or int(cells[1]) <= 1
                or not cells[2]
                or not re.fullmatch(r"[0-9]+", cells[3])
                or not (cells[4] == "NA" or re.fullmatch(r"[0-9]+", cells[4]))
            ):
                raise ValueError("unrecognized NPU process row")
            index = (
                int(chip[0]) * 2 + int(chip[1])
                if host_pids and indexing == "card-die"
                else int(cells[0])
            )
            rows.append((index, int(cells[1]), cells[4]))
            has_process_data = True
            covered_cards.add(
                int(chip[0]) if host_pids and indexing == "card-die" else index
            )

    expected_cards = {
        device // 2 if indexing == "card-die" else device
        for device in (devices if coverage_devices is None else coverage_devices)
    }
    if require_coverage and (
        not has_process_data or not expected_cards.issubset(covered_cards)
    ):
        if not require_exclusive:
            raise ExternalNpuOccupancy("NPU process table is empty or truncated")
        raise ValueError("NPU process table is empty or truncated")

    # A shared process cannot be terminated if it also occupies a chip
    # outside this lease. In A5 mode this is always the *host* PID.
    targets = {pid for chip, pid, _ in rows if chip in selected}
    if require_exclusive and any(
        chip not in selected and pid in targets for chip, pid, _ in rows
    ):
        raise ValueError("PID spans allocated and unallocated NPU devices")
    if any(pid <= 1 for chip, pid, _ in rows if chip in selected):
        raise ValueError("unsafe NPU process PID")
    return [
        (chip, pid, container_pid)
        for chip, pid, container_pid in rows
        if chip in selected
    ]


def process_evidence(
    output: str, devices: list[int], *, indexing: str
) -> list[tuple[int, int]]:
    """Selected (chip, kill PID): A5 host PID, A3 existing card-die PID."""
    return [
        (chip, pid)
        for chip, pid, _ in _process_rows(output, devices, indexing=indexing)
    ]


def selected_processes(
    output: str, devices: list[int], *, indexing: str = "card-die"
) -> list[str]:
    return [
        f"chip {chip}: pid {pid}"
        for chip, pid in process_evidence(output, devices, indexing=indexing)
    ]


def _bound_host_signal(pid, devices, fingerprint, indexing, coverage_devices):
    """Verify a host PID while holding a pidfd, then signal that process only."""
    import hashlib
    import os
    import select
    import signal
    import subprocess
    from pathlib import Path

    if not callable(getattr(signal, "pidfd_send_signal", None)):
        raise RuntimeError("host pidfd signalling is unavailable")
    actual = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
    if actual != fingerprint:
        raise RuntimeError("host identity changed")
    try:
        if callable(getattr(os, "pidfd_open", None)):
            pidfd = os.pidfd_open(pid, 0)
        else:
            import ctypes
            import platform
            import sys

            if sys.platform != "linux" or platform.machine() not in (
                "aarch64",
                "x86_64",
            ):
                raise RuntimeError("host pidfd signalling is unavailable")
            libc = ctypes.CDLL(None, use_errno=True)
            pidfd = libc.syscall(
                ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0)
            )
            if pidfd < 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))
    except ProcessLookupError:
        return False
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)

        def start_time():
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return int(fields[19])

        if poller.poll(0):
            return False
        try:
            before = start_time()
            probe = subprocess.run(
                ["npu-smi", "info"], capture_output=True, text=True, timeout=40
            )
            if probe.returncode:
                raise RuntimeError("host NPU confirmation failed")
            found = _process_rows(
                probe.stdout,
                devices,
                indexing=indexing,
                host_pids=True,
                coverage_devices=coverage_devices,
                require_end=True,
            )
            if not any(current == pid for _, current, _ in found):
                return False
            if start_time() != before or poller.poll(0):
                return False
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            return False
        return True
    finally:
        os.close(pidfd)


def _bound_host_command(pid, devices, fingerprint, indexing, coverage_devices):
    from inspect import getsource

    program = (
        "import json,re,sys\n"
        "class ExternalNpuOccupancy(RuntimeError): pass\n"
        + "CHIP_PROCESS = re.compile("
        + repr(CHIP_PROCESS.pattern)
        + ")\n"
        + getsource(_verified_inventory)
        + "\n"
        + getsource(_process_rows)
        + "\n"
        + getsource(_bound_host_signal)
        + "\nprint('SIGNALED' if _bound_host_signal(int(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3], sys.argv[4], json.loads(sys.argv[5])) else 'GONE')\n"
    )
    return (
        "python3 -c "
        + shlex.quote(program)
        + " "
        + str(pid)
        + " "
        + shlex.quote(json.dumps(devices))
        + " "
        + shlex.quote(fingerprint)
        + " "
        + shlex.quote(indexing)
        + " "
        + shlex.quote(json.dumps(coverage_devices))
    )


def remote_preflight(
    case: dict,
    pools: dict,
    *,
    ascend_env: str | None = None,
    evidence=None,
    cleanup: bool = False,
    cancel=None,
) -> None:
    """Check all selected nodes before applying their occupancy policies."""
    if not case.get("allocation"):
        return
    from ci_core.config import execution_node, node_preempts
    from ci_core.transport import run as transport_run

    pool = pools[case["pool"]]
    reclaim = pool.get("force_reclaim", False)
    if type(reclaim) is not bool or (
        reclaim and (pool.get("owner") != "yellow" or pool.get("type") != "ascend-a5")
    ):
        raise ValueError("invalid force_reclaim pool")
    by_host = {x["host_id"]: x for x in pool["nodes"]}
    indexing = pool.get("process_indexing")
    if indexing not in ("card", "card-die"):
        raise ValueError("NPU process index mode must be configured")

    def check_cancel(*, signalling=False):
        if cancel is not None and cancel.is_set():
            if cleanup and signalling:
                raise ExternalNpuOccupancy("cancelled cleanup cannot preempt")
            if cleanup:
                return
            raise InterruptedError("cancelled during NPU preflight")

    command = "npu-smi info"
    if ascend_env:
        command = "source " + shlex.quote(ascend_env) + " >/dev/null 2>&1 && " + command
    targets = []
    for request in case["allocation"]:
        check_cancel()
        node = by_host[request["host_id"]]
        legacy_a5 = pool.get("type") == "ascend-a5" and "occupancy_policy" not in node
        reclaim = node_preempts(pool, node)
        if cleanup and (
            "occupancy_policy" in node or (cancel is not None and cancel.is_set())
        ):
            reclaim = False
        host_pids = reclaim and node.get("occupancy_policy") == "preempt"
        if host_pids and not set(request["devices"]).issubset(node["device_ids"]):
            raise ValueError("allocation outside verified node devices")
        probe_host = node.get("ssh")
        if not isinstance(probe_host, str) or not probe_host:
            raise ValueError("NPU preflight host SSH is not configured")
        kill_host = None
        if reclaim:
            kill_host = (
                node["ssh"] if host_pids else node.get("host_ssh") or node["ssh"]
            )
            if not isinstance(kill_host, str) or not kill_host or "/" in kill_host:
                raise ValueError("NPU reclaim requires a host-only SSH alias")
        if case.get("docker"):
            probe_host = execution_node(node, case["docker"])["ssh"]
        # Explicit preemption is bound to the execution host and its PID table.
        if host_pids:
            probe_host = kill_host
        targets.append((request, probe_host, kill_host, reclaim, host_pids, legacy_a5))
        if evidence is not None:
            evidence.write(
                f"OCCUPANCY_POLICY host={request['host_id']} "
                f"policy={'preempt' if reclaim else 'wait'} "
                f"phase={'cleanup' if cleanup else 'preflight'}\n"
            )
            evidence.flush()
    targets.sort(key=lambda target: target[3])

    def inspect(target, round_number, stage):
        request, probe_host, _, reclaim, host_pids, legacy_a5 = target
        check_cancel()
        probe_command = "npu-smi info" if host_pids else command
        proc = transport_run(probe_host, probe_command, timeout=40, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"NPU occupancy query failed on {request['host_id']}")
        found = _process_rows(
            proc.stdout,
            request["devices"],
            indexing=indexing,
            require_exclusive=reclaim or legacy_a5,
            host_pids=host_pids,
            coverage_devices=by_host[request["host_id"]]["device_ids"]
            if host_pids
            else None,
            require_end=not legacy_a5,
            require_coverage=not legacy_a5,
        )
        if evidence is not None:
            details = (
                [{"chip": chip, "observed_pid": pid} for chip, pid, _ in found]
                if indexing == "card-die" and not host_pids
                else [
                    {"chip": chip, "host_pid": pid, "container_pid": container_pid}
                    for chip, pid, container_pid in found
                ]
            )
            evidence.write(
                f"RECLAIM_EVIDENCE host={request['host_id']} "
                f"round={round_number} stage={stage} "
                f"allocated={request['devices']} "
                f"processes={json.dumps(details, sort_keys=True)}\n"
            )
            evidence.flush()
        return found

    def wait_if_busy(target, found):
        request = target[0]
        if found:
            raise ExternalNpuOccupancy(
                f"External NPU activity on {request['host_id']}: "
                f"{[(chip, pid) for chip, pid, _ in found][:3]}"
            )

    # A blocked/invalid node must be discovered before any other node is killed.
    observed = []
    for target in targets:
        found = inspect(target, 0, "scan")
        if not target[3]:
            wait_if_busy(target, found)
        observed.append(found)
    for target, initial in zip(targets, observed):
        request, _, kill_host, reclaim, host_pids, _ = target
        if not reclaim:
            continue
        for attempt in range(4):
            found = initial if attempt == 0 else inspect(target, attempt, "scan")
            if not found:
                break
            for waiting in targets:
                if not waiting[3]:
                    wait_if_busy(waiting, inspect(waiting, attempt, "preempt_gate"))
            confirmed = inspect(target, attempt, "confirm")
            stable = {(chip, pid) for chip, pid, _ in found} & {
                (chip, pid) for chip, pid, _ in confirmed
            }
            for pid in sorted({pid for _, pid in stable}):
                chips = sorted(chip for chip, p in stable if p == pid)
                container_pids = sorted(
                    {cp for chip, p, cp in confirmed if p == pid and chip in chips}
                )
                signal_command = (
                    _bound_host_command(
                        pid,
                        request["devices"],
                        by_host[request["host_id"]]["machine_fingerprint"],
                        indexing,
                        by_host[request["host_id"]]["device_ids"],
                    )
                    if host_pids
                    else "kill -9 -- " + str(pid)
                )
                check_cancel(signalling=True)
                result = transport_run(
                    kill_host,
                    signal_command,
                    timeout=60 if host_pids else 20,
                    check=False,
                )
                if host_pids and (
                    result.returncode
                    or result.stdout.strip() not in ("SIGNALED", "GONE")
                ):
                    raise RuntimeError("PID-bound host reclaim could not be verified")
                if evidence is not None:
                    action = (
                        "KILL"
                        if not host_pids or result.stdout.strip() == "SIGNALED"
                        else "SKIP"
                    )
                    evidence.write(
                        f"RECLAIM_{action} host={request['host_id']} "
                        f"ssh={kill_host} chips={chips} host_pid={pid} "
                        f"container_pids={container_pids} rc={result.returncode}\n"
                    )
                    evidence.flush()
                if result.returncode != 0:
                    remaining = inspect(target, attempt, "post_kill_nonzero")
                    if any(host_pid == pid for _, host_pid, _ in remaining):
                        raise RuntimeError(
                            f"NPU reclaim kill failed on {request['host_id']} "
                            f"host_pid={pid}"
                        )
            if attempt == 3:
                if inspect(target, attempt, "final"):
                    raise RuntimeError(f"NPU reclaim not idle on {request['host_id']}")
                break

    # A node can become busy while another node is being reclaimed.
    if len(targets) > 1 and any(target[3] for target in targets):
        for target in targets:
            wait_if_busy(target, inspect(target, 0, "recheck"))


def check_a5_multinode_rootinfo(case: dict, pools: dict) -> None:
    """Fail before any NPU reclaim/launch if any multi-node A5 rootinfo is broken."""
    if len(case.get("allocation", [])) <= 1:
        return
    pool = pools[case["pool"]]
    if pool.get("type") != "ascend-a5":
        return
    from ci_core.config import execution_node
    from ci_core.transport import run as ssh_run

    nodes = {n["host_id"]: n for n in pool["nodes"]}
    checker = (
        "import json,sys; "
        "p='/etc/hccl_rootinfo.json'; "
        "d=json.load(open(p)); "
        "n=d.get('rank_count') if isinstance(d,dict) else None; "
        "assert type(n) in (int,str) and str(n)=='8', "
        "f'rank_count expected 8, got {n!r}'"
    )
    failures = []
    for allocated in case["allocation"]:
        host_id = allocated["host_id"]
        node = nodes[host_id]
        binding = node.get("environments", {}).get(case.get("docker"), {})
        container_id = binding.get("container_id", "UNBOUND")
        try:
            # execution_node validates the pinned container and tag first.
            bound = execution_node(node, case["docker"])
            proc = ssh_run(
                bound["ssh"],
                "python3 -c " + shlex.quote(checker),
                timeout=30,
                check=False,
            )
            if proc.returncode:
                failures.append(
                    f"{host_id} container={container_id[:16]}: "
                    f"invalid /etc/hccl_rootinfo.json ({proc.stderr.strip()[:120]})"
                )
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(
                f"{host_id} container={container_id[:16]}: "
                f"rootinfo validation failed: {type(exc).__name__}: {str(exc)[:100]}"
            )
    if failures:
        raise ValueError(
            "A5_MULTI_NODE_ROOTINFO_INVALID: /etc/hccl_rootinfo.json must have "
            "valid JSON rank_count=8. FIX: restore inside listed A5 Docker "
            "containers, then rerun; " + "; ".join(failures)
        )


def summarize(log: Path, rc: int) -> list[str]:
    """Use selected remote diagnostics; fallback to local dispatcher tail."""
    inside = False
    chosen = collections.deque(maxlen=20)
    tail = collections.deque(maxlen=20)
    with log.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw.rstrip("\r\n"))[:180]
            tail.append(line)
            if line == MARKER_START:
                inside = True
                chosen.clear()
            elif line == MARKER_END and inside:
                return list(chosen) or ["No remote log lines returned"]
            elif inside:
                chosen.append(line)
    return list(tail) or [f"Execution returned {rc} without output"]


def prepare_profile(case: dict, run: dict) -> dict:
    from ci_core.ssh_runner import model_archive

    with tempfile.TemporaryDirectory(prefix="lite-model-") as tmp:
        source_repo = run.get("_ci_repo", TORCHTITAN_REPO)
        archive = model_archive(
            run.get("_ci_execution_sha", run["head_sha"]),
            Path(tmp),
            source_repo=source_repo,
            provider=run.get("_ci_provider", "github"),
            fallback_repo=run.get("_ci_source_repo"),
            source_pr=(
                run["_ci_pr_number"]
                if run.get("_ci_source", {}).get("mode") == "merge_result"
                else None
            ),
        )
        definition = resolve_test(
            archive,
            run["_ci_test_id"],
            nodes=case["resources"]["nodes"],
            ngpu=case["resources"]["devices_per_node"],
            repo=source_repo,
        )
    return definition["env_vars"]


def execute(case: dict, run: dict, *, root: Path = BASE) -> Execution:
    if not case.get("enabled"):
        raise ValueError("disabled case")
    cancel = run.get("_ci_cancel_event")
    log_dir = task_dir(run, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    if cancel is not None and cancel.is_set():
        logfile.write_text("Cancelled before launch\n", encoding="utf-8")
        return Execution(130, ["Cancelled before launch"], logfile, True)
    environment = prepare_profile(case, run)
    setup = environment["ASCEND_SET_ENV_PATH"]
    case = {**case, "ascend_set_env_path": setup}
    pools = json.loads((root / "config/pools.json").read_text())
    from ci_core.scheduler import validate_allocation

    validate_allocation(case, pools, case["allocation"])
    run.get("_ci_runtime_callback", lambda value: None)(case)
    runtime_file = log_dir / "runtime-case.json"
    runtime_file.write_text(json.dumps(case) + "\n")
    command = [
        sys.executable,
        "-u",
        "-m",
        "ci_core.ssh_runner",
        "--case-file",
        str(runtime_file),
        "--test-id",
        run["_ci_test_id"],
        "--sha",
        run.get("_ci_execution_sha", run["head_sha"]),
        "--stamp",
        log_dir.name,
        "--remote-dir",
        remote_task_name(run),
    ]
    callback = run.get("_ci_phase_callback", lambda phase: None)
    release_verified = False
    announced = False
    reclaim_started = False
    child_started = False
    with logfile.open("w", encoding="utf-8") as stream:
        rc = 125
        try:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled before launch")
            check_a5_multinode_rootinfo(case, pools)
            reclaim_started = True
            remote_preflight(
                case, pools, ascend_env=setup, evidence=stream, cancel=cancel
            )
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled before launch")
            failures = broadcast_job(case, pools, run, "begin")
            if failures:
                raise RuntimeError("CI begin wall failed on " + ",".join(failures))
            announced = True
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled before launch")
            callback("LAUNCHING")
            proc = subprocess.Popen(
                command,
                cwd=root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "LITE_ACTIONS_MODEL_REPO": TORCHTITAN_REPO
                    if run.get("_ci_provider") == "gitcode"
                    else run.get("_ci_repo", TORCHTITAN_REPO),
                    "LITE_ACTIONS_SOURCE_PROVIDER": run.get("_ci_provider", "github"),
                    "LITE_ACTIONS_SOURCE_REPO": run.get("_ci_repo", TORCHTITAN_REPO),
                    "LITE_ACTIONS_SOURCE_FALLBACK_REPO": run.get("_ci_source_repo", ""),
                },
                start_new_session=True,
            )
            child_started = True
            try:
                deadline = time.monotonic() + 600
                staged = False
                while True:
                    if not staged:
                        stream.flush()
                        staged = logfile.read_text(errors="replace").count(
                            "STAGED: "
                        ) >= len(case["allocation"])
                        if staged:
                            callback("RUNNING")
                            deadline = (
                                time.monotonic() + case["execution_timeout_seconds"]
                            )
                    if proc.poll() is not None:
                        rc = proc.returncode
                        break
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("GitHub Run cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            command,
                            case["execution_timeout_seconds"] if staged else 600,
                        )
                    try:
                        rc = proc.wait(timeout=min(5.0, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except (subprocess.TimeoutExpired, InterruptedError) as exc:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                rc = 130 if isinstance(exc, InterruptedError) else 124
                stream.write(
                    f"ERROR: case interrupted rc={rc}; verifying remote cleanup\n"
                )
        except ExternalNpuOccupancy as exc:
            if not announced and not child_started:
                reclaim_started = False
                raise
            stream.write(f"ERROR: {type(exc).__name__}: {str(exc)[:240]}\n")
            rc = 125
        except InterruptedError as exc:
            stream.write(f"ERROR: {exc}\n")
            rc = 130
        except Exception as exc:
            if "A5_MULTI_NODE_ROOTINFO_INVALID" in str(exc):
                for line in str(exc).replace("; ", "\n").splitlines():
                    stream.write("ERROR: " + line[:175] + "\n")
            else:
                stream.write(f"ERROR: {type(exc).__name__}: {str(exc)[:240]}\n")
            logging.exception("case execution failed run=%s", run["id"])
            rc = 125
        finally:
            callback("CLEANING")
            if announced or reclaim_started:
                from ci_core.ssh_runner import (
                    DEST_ROOT,
                    allocations,
                    owned_processes_gone,
                )

                for attempt in range(3):
                    try:
                        remote_preflight(
                            case,
                            pools,
                            ascend_env=setup,
                            evidence=stream,
                            cleanup=True,
                            cancel=cancel,
                        )
                        if child_started:
                            for node in allocations(case, pools):
                                destination = f"{DEST_ROOT}/{remote_task_name(run)}/node{node['position']}"
                                owned_processes_gone(node, destination, log_dir.name)
                        release_verified = True
                        break
                    except (RuntimeError, subprocess.SubprocessError, ValueError):
                        if attempt < 2:
                            time.sleep(2)
                try:
                    if announced:
                        broadcast_job(
                            case, pools, run, "end", released=release_verified
                        )
                except Exception:
                    logging.exception("CI exit notification failed")
                if not release_verified:
                    stream.write("ERROR: remote resource release unverified\n")
                    rc = rc or 125
            else:
                release_verified = True
            stream.flush()
    try:
        from ci_core.config import execution_node

        location = metadata(logfile)
        selected_hosts = {n["host_id"] for n in case["allocation"]}
        approved = {
            execution_node(n, case["docker"])["ssh"]
            for n in pools[case["pool"]]["nodes"]
            if n["host_id"] in selected_hosts
        }
        excerpt = select_remote(
            location["host"],
            location["path"],
            rc,
            approved_hosts=approved,
            require_loss=case["kind"] == "train",
        )
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        excerpt = summarize(logfile, rc)[-18:]
        excerpt.append(f"Log collection failed: {type(exc).__name__}: {str(exc)[:120]}")
        if rc == 0:
            rc = 125
    if cancel is not None and cancel.is_set() and rc == 0:
        rc = 130
    return Execution(rc, excerpt[:20], logfile, release_verified)
