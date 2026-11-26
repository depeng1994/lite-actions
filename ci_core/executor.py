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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ci_core.broadcast import broadcast_job
from ci_core.config import execution_pools
from ci_core.contract import (
    TORCHTITAN_REPO,
    VLLM_REPO,
    trusted_gitcode_repositories,
    trusted_repositories,
)
from ci_core.logs import metadata, select_remote

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
    locator: dict | None = None
    diagnostic: tuple[bytes, bool] | None = None


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
        trusted_gitcode_repositories()
        if provider == "gitcode"
        else trusted_repositories()
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
    if source_repo not in (
        trusted_gitcode_repositories()
        if provider == "gitcode"
        else trusted_repositories()
    ):
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


def _process_table_header(output, indexing, *, legacy_a3=False):
    """Find the process table by its required columns, not container PID."""
    required = [
        "NPU Chip" if indexing == "card-die" else "NPU ID",
        "Process id",
        "Process name",
        "Process memory(MB)",
    ]
    for i, line in enumerate(output.splitlines()):
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [" ".join(cell.split()) for cell in line[1:-1].split("|")]
        if cells == required or cells == required + ["Process id in container"]:
            return i, len(cells)
        if (
            legacy_a3
            and indexing == "card-die"
            and cells
            == [
                "NPU ID",
                "Chip ID",
                "Process id in container",
            ]
        ):
            return i, len(cells)
    raise ValueError("npu-smi process table absent")


def _verified_inventory(output, indexing, expected_devices):
    lines = output.splitlines()
    header = None
    a5_inventory = False
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = (
            [cell.strip() for cell in line[1:-1].split("|")]
            if line.endswith("|")
            else []
        )
        if cells and " ".join(cells[0].split()) == "NPU Name":
            header = i
            break
        if len(cells) >= 2 and cells[0] == "NPU ID" and cells[1] == "Name":
            header = i
            a5_inventory = True
            break
    if header is None or header + 1 >= len(lines):
        raise ValueError("NPU hardware inventory header missing")
    end, _ = _process_table_header(output, indexing)
    if end <= header + 1:
        raise ValueError("NPU process table missing")
    data = [
        line.strip()
        for line in lines[header + 2 : end]
        if line.strip() and not re.fullmatch(r"[+=-]+", line.strip())
    ]
    if not data or len(data) % 2:
        raise ValueError("incomplete NPU hardware mapping")

    if a5_inventory:
        if indexing != "card":
            raise ValueError("Ascend950DT inventory requires card indexing")
        second_header = (
            [cell.strip() for cell in lines[header + 1][1:-1].split("|")]
            if lines[header + 1].strip().startswith("|")
            else []
        )
        if len(second_header) < 3 or second_header[2] != "Bus-Id":
            raise ValueError("NPU hardware mapping header missing")
        mapping = {}
        buses = set()
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
            status_cells = [cell.strip() for cell in second[1:-1].split("|")]
            if (
                len(npu_cells) < 3
                or len(status_cells) < 3
                or not re.fullmatch(r"[0-9]+", npu_cells[0])
                or not re.fullmatch(r"Ascend950[A-Za-z0-9_.-]*", npu_cells[1])
            ):
                raise ValueError("unrecognized NPU hardware mapping")
            card = int(npu_cells[0])
            bus = status_cells[2]
            if bus != "NA" and not re.fullmatch(
                r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", bus
            ):
                raise ValueError("unrecognized NPU hardware mapping")
            if card in mapping or (bus != "NA" and bus.lower() in buses):
                raise ValueError("duplicate NPU hardware mapping")
            mapping[card] = card
            if bus != "NA":
                buses.add(bus.lower())
        if not mapping or set(mapping) != set(expected_devices):
            raise ValueError("NPU hardware inventory differs from configured devices")
        return

    chip_header = lines[header + 1]
    if (
        not chip_header.startswith("|")
        or " ".join(chip_header.split("|")[1].split()) != "Chip Phy-ID"
    ):
        raise ValueError("NPU hardware mapping header missing")
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
    whether the process belongs to the bound CI container. The optional
    container PID is diagnostic only and never used for validation or signalling.
    Preserve the established A3 card-die PID and chip mapping.
    """
    if indexing not in ("card", "card-die"):
        raise ValueError("unknown NPU process index mapping")
    header_line, width = _process_table_header(
        output, indexing, legacy_a3=not host_pids
    )
    if host_pids:
        _verified_inventory(output, indexing, coverage_devices)

    selected = set(devices)
    rows: list[tuple[int, int, str]] = []
    has_process_data = False
    covered_cards = set()
    lines = output.splitlines()[header_line + 1 :]
    if require_end:
        nonempty = [line.strip() for line in lines if line.strip()]
        if not nonempty or not re.fullmatch(
            r"\+[=-]{3,}(?:\+[=-]{3,})*\+", nonempty[-1]
        ):
            if not require_exclusive:
                raise ExternalNpuOccupancy("NPU process table has no verified end")
            raise ValueError("NPU process table has no verified end")
    for line in lines:
        line = line.strip()
        if not line or re.fullmatch(r"[+=-]+", line):
            continue
        empty = re.fullmatch(
            r"\|\s*No running processes found in NPU (\d+)(?:\.\.(\d+))?\s*\|",
            line,
        )
        if empty:
            first = int(empty[1])
            last = int(empty[2]) if empty[2] is not None else first
            if last < first or last - first > 255:
                raise ValueError("invalid empty NPU range")
            has_process_data = True
            covered_cards.update(range(first, last + 1))
            continue
        if width == 3:
            # Legacy A3 container-side layout; preserve its PID mapping.
            match = CHIP_PROCESS.match(line)
            if not match:
                if not require_exclusive:
                    raise ExternalNpuOccupancy("NPU process row cannot be resolved")
                raise ValueError("unrecognized NPU process row")
            card, die, pid = map(int, match.groups())
            rows.append((card * 2 + die, pid, None))
            has_process_data = True
            covered_cards.add(card)
            continue

        if not (line.startswith("|") and line.endswith("|")):
            if indexing == "card-die" and not host_pids and not require_exclusive:
                raise ExternalNpuOccupancy("NPU process row cannot be resolved")
            raise ValueError("unrecognized NPU process row")
        cells = [part.strip() for part in line[1:-1].split("|")]
        chip = cells[0].split()
        valid_chip = (
            len(chip) == 2
            and re.fullmatch(r"[0-9]+", chip[0])
            and chip[1] in ("0", "1")
            if indexing == "card-die"
            else re.fullmatch(r"[0-9]+", cells[0])
        )
        if (
            not 4 <= len(cells) <= width
            or not valid_chip
            or not re.fullmatch(r"[0-9]+", cells[1])
            or int(cells[1]) <= 1
            or not re.fullmatch(r"[0-9]+", cells[3])
        ):
            if indexing == "card-die" and not host_pids and not require_exclusive:
                raise ExternalNpuOccupancy("NPU process row cannot be resolved")
            raise ValueError("unrecognized NPU process row")
        card = int(chip[0])
        index = card * 2 + int(chip[1]) if indexing == "card-die" else card
        # The optional container PID is diagnostic evidence only. Missing or
        # nonnumeric values must never invalidate the host-PID observation.
        container_pid = (
            cells[4] if len(cells) == 5 and re.fullmatch(r"[0-9]+", cells[4]) else "NA"
        )
        if indexing == "card-die" and not host_pids:
            container_pid = None
        rows.append((index, int(cells[1]), container_pid))
        has_process_data = True
        covered_cards.add(card)

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
    import errno as errno_mod
    import hashlib
    import os
    import select
    import signal
    import subprocess
    import time
    from pathlib import Path

    if not callable(getattr(signal, "pidfd_send_signal", None)):
        raise RuntimeError("host pidfd signalling is unavailable")
    actual = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
    if actual != fingerprint:
        raise RuntimeError("host identity changed")

    def open_pidfd(target_pid):
        if callable(getattr(os, "pidfd_open", None)):
            return os.pidfd_open(target_pid, 0)
        import ctypes
        import platform
        import sys

        if sys.platform != "linux" or platform.machine() not in (
            "aarch64",
            "x86_64",
        ):
            raise RuntimeError("host pidfd signalling is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.syscall(
            ctypes.c_long(434), ctypes.c_int(target_pid), ctypes.c_uint(0)
        )
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return fd

    try:
        pidfd = open_pidfd(pid)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno != errno_mod.EINVAL:
            raise
        # Linux pidfd_open returns EINVAL when the PID is a non-leader thread
        # or already tearing down; fall back to its Tgid or treat as exited so
        # the outer preflight loop re-verifies NPU idleness.
        try:
            status_text = Path(f"/proc/{pid}/status").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return False
        tgid = None
        for line in status_text.splitlines():
            if line.startswith("Tgid:"):
                parts = line.split()
                if len(parts) == 2 and parts[1].isdecimal():
                    tgid = int(parts[1])
                break
        if tgid is None or tgid <= 1 or tgid == pid:
            return False
        try:
            pidfd = open_pidfd(tgid)
        except ProcessLookupError:
            return False
        except OSError as exc2:
            if exc2.errno == errno_mod.EINVAL:
                return False
            raise
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)

        if poller.poll(0):
            return False
        try:
            found = None
            for retry in range(3):
                probe = subprocess.run(
                    ["npu-smi", "info"], capture_output=True, text=True, timeout=40
                )
                try:
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
                    break
                except (RuntimeError, ValueError, ExternalNpuOccupancy) as exc:
                    if poller.poll(0):
                        return False
                    msg = str(exc)
                    if (
                        "spans allocated" in msg
                        or "unsafe NPU process PID" in msg
                        or retry == 2
                    ):
                        raise
                    time.sleep(1)
            if not found or not any(current == pid for _, current, _ in found):
                return False
            # pidfd remains bound to the process opened above, even if the
            # numeric PID is reused after the NPU probe.
            if poller.poll(0):
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
        + getsource(_process_table_header)
        + "\n"
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
    from ci_core.config import execution_node, legacy_reclaim, node_preempts
    from ci_core.transport import run as transport_run

    pool = pools[case["pool"]]
    reclaim = pool.get("force_reclaim", False)
    if type(reclaim) is not bool:
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
        legacy_a5 = legacy_reclaim(pool, node)
        reclaim = node_preempts(pool, node)
        if reclaim and legacy_a5 and pool.get("type") == "ascend-a5":
            devices = node.get("device_ids")
            if (
                isinstance(node.get("machine_fingerprint"), str)
                and re.fullmatch(r"[0-9a-f]{64}", node["machine_fingerprint"])
                and isinstance(devices, list)
                and devices
                and all(type(device) is int and device >= 0 for device in devices)
                and len(set(devices)) == len(devices)
            ):
                # A5 host-PID reclaim is safe when the node carries the same
                # verified identity required by the explicit preempt policy.
                legacy_a5 = False
        if cleanup and (not legacy_a5 or (cancel is not None and cancel.is_set())):
            reclaim = False
        host_pids = reclaim and not legacy_a5
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
        try:
            proc = transport_run(probe_host, probe_command, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            check_cancel()
            proc = transport_run(probe_host, probe_command, timeout=60, check=False)
        if proc.returncode == 255:
            check_cancel()
            proc = transport_run(probe_host, probe_command, timeout=60, check=False)
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
                if host_pids and result.returncode == 255:
                    check_cancel(signalling=True)
                    result = transport_run(
                        kill_host,
                        signal_command,
                        timeout=60,
                        check=False,
                    )
                if host_pids and (
                    result.returncode
                    or result.stdout.strip() not in ("SIGNALED", "GONE")
                ):
                    detail = (
                        (getattr(result, "stderr", "") or result.stdout or "")
                        .strip()
                        .replace("\n", " ")[:240]
                    )
                    if evidence is not None:
                        evidence.write(
                            f"RECLAIM_UNVERIFIED host={request['host_id']} "
                            f"ssh={kill_host} chips={chips} host_pid={pid} "
                            f"rc={result.returncode} detail={detail}\n"
                        )
                        evidence.flush()
                    suffix = (
                        f" (rc={result.returncode}: {detail})"
                        if detail
                        else f" (rc={result.returncode})"
                    )
                    raise RuntimeError(
                        "PID-bound host reclaim could not be verified" + suffix
                    )
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

    def _check_one(allocated: dict) -> str | None:
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
                return (
                    f"{host_id} container={container_id[:16]}: "
                    f"invalid /etc/hccl_rootinfo.json ({proc.stderr.strip()[:120]})"
                )
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return (
                f"{host_id} container={container_id[:16]}: "
                f"rootinfo validation failed: {type(exc).__name__}: {str(exc)[:100]}"
            )
        return None

    allocation = case["allocation"]
    with ThreadPoolExecutor(max_workers=len(allocation)) as pool:
        failures = [msg for msg in pool.map(_check_one, allocation) if msg is not None]
    if failures:
        raise ValueError(
            "A5_MULTI_NODE_ROOTINFO_INVALID: /etc/hccl_rootinfo.json must have "
            "valid JSON rank_count=8. FIX: restore inside listed A5 Docker "
            "containers, then rerun; " + "; ".join(failures)
        )


def summarize(log: Path, rc: int) -> list[str]:
    """Use selected remote diagnostics; fallback to local dispatcher tail."""
    if not log.exists():
        return [f"Execution returned {rc} without local log"]
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


def execute(case: dict, run: dict, *, root: Path = BASE) -> Execution:
    if not case.get("enabled"):
        raise ValueError("disabled case")
    cancel = run.get("_ci_cancel_event")
    log_dir = run.get("_ci_pipeline_folder") or task_dir(run, root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "pipeline.log"
    if cancel is not None and cancel.is_set():
        logfile.write_text("Cancelled before launch\n", encoding="utf-8")
        return Execution(130, ["Cancelled before launch"], logfile, True)
    environment = case["verified_env"]
    setup = environment["ASCEND_SET_ENV_PATH"]
    case = {**case, "ascend_set_env_path": setup}
    lease_obj = run.get("_ci_lease")
    if lease_obj is not None and not case.get("lease_id"):
        case["lease_id"] = lease_obj.lease_id
    if run.get("_ci_remote_lease_dir") and not case.get("remote_lease_dir"):
        case["remote_lease_dir"] = run["_ci_remote_lease_dir"]
    if run.get("_ci_stage_archive") and not case.get("_ci_stage_archive"):
        case["_ci_stage_archive"] = run["_ci_stage_archive"]
    pools = execution_pools(root, case)
    from ci_core.scheduler import validate_allocation

    validate_allocation(case, pools, case["allocation"], include_paused=True)
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
    if case.get("lease_id"):
        command.extend(["--lease-id", str(case["lease_id"])])
    if case.get("remote_lease_dir"):
        command.extend(["--lease-dir", str(case["remote_lease_dir"])])
    remote_nodes_meta = []
    callback = run.get("_ci_phase_callback", lambda phase: None)
    release_verified = False
    announced = False
    reclaim_started = False
    child_started = False
    staged = False
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
            if lease_obj is not None:
                from ci_core.ssh_runner import allocations as allocated_nodes

                indexing = case.get(
                    "indexing", pools[case["pool"]].get("indexing", "card-die")
                )
                remote_nodes_meta = [
                    {
                        "host_id": n["host_id"],
                        "ssh": n["ssh"],
                        "devices": list(n.get("devices") or ()),
                        "indexing": indexing,
                    }
                    for n in allocated_nodes(case, pools)
                ]
                lease_obj.mark_launching(remote_nodes=remote_nodes_meta)
            callback("LAUNCHING")
            proc = subprocess.Popen(
                command,
                cwd=root,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env={
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if k != "LITE_ACTIONS_MODEL_REPOS"
                    },
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
                while True:
                    if not staged:
                        stream.flush()
                        staged = logfile.read_text(errors="replace").count(
                            "STAGED: "
                        ) >= len(case["allocation"])
                        if staged:
                            if lease_obj is not None:
                                lease_obj.mark_active()
                            callback("RUNNING")
                            exec_budget = max(
                                case["execution_timeout_seconds"] + 90, 180
                            )
                            deadline = time.monotonic() + exec_budget
                    if proc.poll() is not None:
                        rc = proc.returncode
                        break
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("GitHub Run cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            command,
                            max(case["execution_timeout_seconds"] + 90, 180)
                            if staged
                            else 600,
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
            defer_revoke = bool(run.get("_ci_defer_lease_revoke"))
            launched_lease = (
                not defer_revoke
                and bool(case.get("lease_id"))
                and (
                    child_started
                    or (
                        lease_obj is not None
                        and lease_obj.state in {"LAUNCHING", "ACTIVE"}
                    )
                )
            )
            if announced or reclaim_started or launched_lease:
                from ci_core.ssh_runner import (
                    DEST_ROOT,
                    allocations,
                    owned_processes_gone,
                    revoke_remote_lease,
                )
                from ci_core.ssh_runner import cancel as cancel_owned

                all_revoked_hosts: set[str] = set()
                for attempt in range(3):
                    try:
                        revoke_errors: dict[str, str] = {}
                        revoke_ok = True
                        if child_started:
                            for node in allocations(case, pools):
                                destination = (
                                    f"{DEST_ROOT}/{remote_task_name(run)}"
                                    f"/node{node['position']}"
                                )
                                cancel_owned(node, destination, log_dir.name)
                        if launched_lease:
                            nodes_for_revoke = remote_nodes_meta or [
                                {
                                    "host_id": n["host_id"],
                                    "ssh": n["ssh"],
                                    "devices": list(n.get("devices") or ()),
                                    "indexing": case.get(
                                        "indexing",
                                        pools[case["pool"]].get("indexing", "card-die"),
                                    ),
                                }
                                for n in allocations(case, pools)
                            ]
                            expected_hosts = {n["ssh"] for n in nodes_for_revoke}
                            pending_nodes = [
                                n
                                for n in nodes_for_revoke
                                if n["ssh"] not in all_revoked_hosts
                            ]
                            if pending_nodes:
                                newly_revoked = revoke_remote_lease(
                                    {
                                        "lease_id": case["lease_id"],
                                        "remote_nodes": pending_nodes,
                                        "remote_lease_dir": case.get(
                                            "remote_lease_dir"
                                        ),
                                    },
                                    check_npu=False,
                                    errors=revoke_errors,
                                )
                                all_revoked_hosts.update(newly_revoked)
                            if lease_obj is not None:
                                lease_obj.set_remote_revoked_hosts(all_revoked_hosts)
                            revoke_ok = expected_hosts.issubset(all_revoked_hosts)
                        if child_started and (staged or not launched_lease):
                            for node in allocations(case, pools):
                                destination = f"{DEST_ROOT}/{remote_task_name(run)}/node{node['position']}"
                                owned_processes_gone(node, destination, log_dir.name)
                        if launched_lease and not revoke_ok:
                            for host, detail in sorted(revoke_errors.items()):
                                stream.write(
                                    f"REVOKE_ERROR host={host} detail={detail[:220]}\n"
                                )
                            stream.flush()
                            raise RuntimeError("UNVERIFIED_REMOTE_LEASE_REVOKE")
                        release_verified = True
                        break
                    except (
                        RuntimeError,
                        subprocess.SubprocessError,
                        ValueError,
                    ) as exc:
                        stream.write(
                            f"CLEANUP_RETRY attempt={attempt} "
                            f"error={type(exc).__name__}: {str(exc)[:180]}\n"
                        )
                        stream.flush()
                        if attempt < 2:
                            time.sleep(5)
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
        endpoints = {
            execution_node(n, case["docker"])["ssh"]: n["host_id"]
            for n in pools[case["pool"]]["nodes"]
            if n["host_id"] in selected_hosts
        }
        approved = set(endpoints)
        locator = None
        if "_ci_worker_address" in run:
            from ci_core.ssh_runner import DEST_ROOT

            managed = endpoints.get(location["host"])
            rank = next(
                (
                    i
                    for i, n in enumerate(case["allocation"])
                    if n["host_id"] == managed
                ),
                None,
            )
            path = f"{DEST_ROOT}/{remote_task_name(run)}/node{rank}/run.log"
            if rank is None or location["path"] != path:
                raise ValueError("LOG_LOCATOR_MISMATCH")
            locator = {"host": managed, "path": path}
        try:
            excerpt = select_remote(
                location["host"],
                location["path"],
                rc,
                approved_hosts=approved,
                require_loss=case["kind"] == "train",
            )
        except (RuntimeError, OSError, subprocess.SubprocessError):
            excerpt = select_remote(
                location["host"],
                location["path"],
                rc,
                approved_hosts=approved,
                require_loss=case["kind"] == "train",
            )
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        locator = None
        excerpt = summarize(logfile, rc)[-18:]
        excerpt.append(f"Log collection failed: {type(exc).__name__}")
        if rc == 0:
            rc = 125
    diagnostic = None
    if run.get("_ci_zone") == "blue":
        from ci_core.logs import blue_remote

        try:
            if locator:
                diagnostic = blue_remote(location["host"], location["path"], approved)
            # The internal management/preflight transcript is never a public asset.
        except (ValueError, OSError, subprocess.SubprocessError):
            pass  # Diagnostic transport must not rewrite the actual test outcome.
    if cancel is not None and cancel.is_set() and rc == 0:
        rc = 130
    if locator is None and "_ci_worker_address" in run:
        locator = {"host": run["_ci_worker_address"], "path": str(logfile)}
    return Execution(rc, excerpt[:20], logfile, release_verified, locator, diagnostic)
