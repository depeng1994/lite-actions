"""Execute retained GitCode merge/runner bundles under the existing Blue lease.

Only the immutable trusted Runner executes in NPU containers. The controller
never imports proposed source Python or downloads a model from GitHub. Captured
container IDs, job markers and fences are also sufficient for offline cleanup.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from ci_core.executor import Execution, ExternalNpuOccupancy, _process_rows
from ci_core.gitcode_contract import canonical, load_json
from ci_core.gitcode_interfaces import inspect_directory
from ci_core.gitcode_source import extract_verified_archive, extract_verified_source
from ci_core.gitcode_store import StoredExecution
from ci_core.gitcode_timing import timed_stage
from ci_core.ssh_runner import DEST_ROOT, owned_processes_gone
from ci_core.ssh_runner import cancel as stop_owned
from ci_core.transport import run as transport_run

DOCKER = "cann_master_torchtitan_master_xyj"
ENV_KEYS = {
    "ASCEND_SET_ENV_PATH",
    "HF_ASSETS_PATH",
    "HF_HOME",
    "HF_DATASETS_CACHE",
    "CKPT_INIT_LOAD_PATH",
    "CONFIG",
    "OPTIMIZER_OVERRIDES",
    "TORCHINDUCTOR_NPU_BACKEND",
}
PORT_FIELDS = {
    "master_port": "LITE_CI_MASTER_PORT",
    "hccl_if_base_port": "LITE_CI_HCCL_IF_BASE_PORT",
    "hccl_host_port_range": "LITE_CI_HCCL_HOST_PORT_RANGE",
    "hccl_npu_port_range": "LITE_CI_HCCL_NPU_PORT_RANGE",
}
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")


def quote(value):
    return shlex.quote(str(value))


def verify_node(node):
    """Never follow a Docker name to a replacement container during recovery."""
    if (
        not isinstance(node.get("container_id"), str)
        or not HEX.fullmatch(node["container_id"])
        or node.get("ssh") != node.get("host_ssh", "") + "/" + node["container_id"]
        or node.get("container_name") != DOCKER
    ):
        raise ValueError("GITCODE_CAPTURED_CONTAINER_BINDING_INVALID")
    result = transport_run(
        node["host_ssh"],
        "docker inspect --format "
        + quote(
            '{"Name":{{json .Name}},"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}}}}'
        )
        + " "
        + quote(node["container_id"]),
        timeout=30,
    )
    data = json.loads(result.stdout)
    if (
        data.get("Id") != node["container_id"]
        or str(data.get("Name", "")).removeprefix("/") != node["container_name"]
        or data.get("Image") != node["image_id"]
        or data.get("State", {}).get("Running") is not True
    ):
        raise ValueError("GITCODE_CAPTURED_CONTAINER_REPLACED_OR_STOPPED")


def capture_nodes(case, pools):
    from ci_core.config import execution_node
    from ci_core.scheduler import validate_allocation

    pool = pools[case["pool"]]
    if (
        case["docker"] != DOCKER
        or case["pool"] != "a3_shared_blue"
        or pool["owner"] != "blue"
        or pool.get("force_reclaim", False)
    ):
        raise ValueError("GITCODE_BLUE_ONLY_NON_RECLAIMING_EXECUTION")
    validate_allocation(case, pools, case["allocation"])
    inventory = {node["host_id"]: node for node in pool["nodes"]}
    nodes = []
    for position, allocation in enumerate(case["allocation"]):
        host = inventory[allocation["host_id"]]
        selected = execution_node(host, DOCKER)
        binding = host["environments"][DOCKER]
        node = {
            "host_id": host["host_id"],
            "host_ssh": host["ssh"],
            "ssh": selected["ssh"],
            "container_name": DOCKER,
            "container_id": binding["container_id"],
            "image_id": binding["image_id"],
            "image_ref": binding["image_ref"],
            "node_ip": host["node_ip"],
            "network_interface": host["network_interface"],
            "pythonpath": host.get("pythonpath", ""),
            "position": position,
            "device_ids": list(allocation["devices"]),
        }
        verify_node(node)
        python = transport_run(
            node["ssh"], "command -v python3", timeout=20
        ).stdout.strip()
        if not python.startswith("/") or any(c in python for c in "\n\r\0"):
            raise ValueError("TRUSTED_CONTAINER_PYTHON_REQUIRED")
        node["python"] = python
        nodes.append(node)
    return nodes


def _scan_audit(node, audit_identity, status, rows=()):
    """Pre-capture evidence: fixed fields only, never raw npu-smi or command."""
    if audit_identity is None:
        return
    execution_id, case_id = audit_identity
    numeric = [
        {"chip": chip, "pid": pid}
        for chip, pid, _ in rows
        if type(chip) is int and type(pid) is int
    ]
    evidence = {
        "execution_id": execution_id,
        "case_id": case_id,
        "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "monotonic_ns": time.monotonic_ns(),
        "node": node["position"],
        "allocated_chips": list(node["device_ids"]),
        "processes": numeric[:256],
        "process_count": len(numeric),
        "truncated": len(numeric) > 256,
        "status": status,
    }
    # WARNING is intentional: the service's existing default journal level
    # must retain first-scan IDLE as well as refusal before pipeline.log exists.
    logging.warning(
        "GITCODE_DEVICE_SCAN_AUDIT %s", json.dumps(evidence, sort_keys=True)
    )


def scan_idle(nodes, setup, indexing, stream, *, audit_identity=None):
    """Read-only Blue occupancy checks; never kill another user's process."""
    if audit_identity is not None:
        if (
            not isinstance(audit_identity, tuple)
            or len(audit_identity) != 2
            or any(
                not isinstance(value, str) or not IDENT.fullmatch(value)
                for value in audit_identity
            )
            or any(
                type(node.get("position")) is not int
                or not isinstance(node.get("device_ids"), list)
                or any(type(chip) is not int for chip in node["device_ids"])
                for node in nodes
            )
        ):
            raise ValueError("INVALID_SCAN_AUDIT_IDENTITY")
    for node in nodes:
        try:
            verify_node(node)
            response = transport_run(
                node["ssh"],
                "source " + quote(setup) + " >/dev/null 2>&1 && npu-smi info",
                timeout=40,
            )
        except Exception:
            _scan_audit(node, audit_identity, "COMMAND_ERROR")
            raise
        try:
            rows = _process_rows(response.stdout, node["device_ids"], indexing=indexing)
        except Exception:
            _scan_audit(node, audit_identity, "PARSE_ERROR")
            raise
        _scan_audit(node, audit_identity, "OCCUPIED" if rows else "IDLE", rows)
        stream.write(
            "GITCODE_DEVICE_SCAN "
            + json.dumps(
                {
                    "node": node["position"],
                    "devices": node["device_ids"],
                    "processes": rows,
                },
                sort_keys=True,
            )
            + "\n"
        )
        stream.flush()
        if rows:
            raise ExternalNpuOccupancy("Blue allocation has external NPU activity")


def bundle_profile(stored, item, workspace):
    if not isinstance(stored, StoredExecution):
        raise ValueError("RETAINED_VERIFIED_SOURCE_REQUIRED")
    stored.verify_files()
    plan = stored.verified.plan
    if item not in plan["cases"] or item["kind"] == "worker_probe":
        raise ValueError("VERIFIED_NPU_CASE_REQUIRED")
    source = workspace / "source"
    extract_verified_source(stored.source, source)
    inspect_directory(source, [item["test_id"]])
    runner = workspace / "runner"
    extract_verified_archive(
        stored.runner_path, plan["control"]["runner_sha256"], runner
    )
    allowed = {
        "catalog.json",
        "runner.py",
        "smoke_rank.py",
        "_lite_gitcode_train.py",
        "torchrun_lease.py",
    }
    if {
        p.relative_to(runner).as_posix() for p in runner.rglob("*") if p.is_file()
    } != allowed:
        raise ValueError("UNRECOGNIZED_TRUSTED_RUNNER_BUNDLE")
    document = load_json((runner / "catalog.json").read_bytes())
    if (
        document.get("schema") != 1
        or document.get("runner_protocol") != "lite-gitcode-torchtitan-v1"
    ):
        raise ValueError("UNRECOGNIZED_TRUSTED_RUNNER_PROTOCOL")
    case = document["cases"][item["test_id"]]
    if {k: v for k, v in item.items() if k not in {"case_id", "order"}} != case:
        raise ValueError("RUNNER_CATALOG_AND_PLAN_DIFFER")
    profile = document["profiles"][item["test_id"]]
    environment = profile["env_vars"]
    if set(environment) - ENV_KEYS or any(
        not isinstance(v, str) or any(ord(c) < 32 for c in v)
        for v in environment.values()
    ):
        raise ValueError("UNAPPROVED_RUNNER_ENVIRONMENT")
    setup = environment["ASCEND_SET_ENV_PATH"]
    if not setup.startswith("/"):
        raise ValueError("TRUSTED_TOOLKIT_PATH_REQUIRED")
    return profile


def stage_node(node, stored, runtime):
    verify_node(node)
    dest = node["destination"]
    transport_run(
        node["ssh"],
        "mkdir -p "
        + quote(str(Path(dest).parent))
        + " && mkdir "
        + quote(dest)
        + " && mkdir "
        + quote(dest + "/source")
        + " "
        + quote(dest + "/runner"),
        timeout=30,
    )
    for filename, path, expected, directory in (
        (
            "source.tar.gz",
            stored.source.verify(),
            stored.source.bundle_sha256,
            "source",
        ),
        (
            "runner.tar.gz",
            stored.runner_path,
            stored.verified.plan["control"]["runner_sha256"],
            "runner",
        ),
    ):
        with Path(path).open("rb") as stream:
            transport_run(
                node["ssh"],
                "cat > " + quote(dest + "/" + filename),
                stdin=stream,
                timeout=180,
            )
        check = (
            "test "
            + quote(expected)
            + ' = "$(sha256sum '
            + quote(dest + "/" + filename)
            + " | cut -d ' ' -f1)\""
        )
        transport_run(
            node["ssh"],
            check
            + " && tar --no-same-owner --no-same-permissions -xzf "
            + quote(dest + "/" + filename)
            + " -C "
            + quote(dest + "/" + directory),
            timeout=120,
        )
    return dest


def runner_command(node, runtime, item, phase):
    if phase not in {"launch", "verify"}:
        raise ValueError("INVALID_RUNNER_PHASE")
    nodes = runtime["cleanup"]["nodes"]
    dest = node["destination"]
    arguments = {
        "test-id": item["test_id"],
        "source-root": dest + "/source",
        "output-root": dest + "/output",
        "source-sha": runtime["source_sha"],
        "tree-sha": runtime["tree_sha"],
        "lease-id": runtime["lease_id"],
        "fencing-token": runtime["fencing_token"],
        "task-marker": node["task_marker"],
        "node-count": len(nodes),
        "devices-per-node": len(node["device_ids"]),
        "node-rank": node["position"],
        "master-addr": nodes[0]["node_ip"],
        "master-port": runtime["ports"]["master_port"],
    }
    entry = [node["python"], "-I", dest + "/runner/runner.py", phase]
    for key, value in arguments.items():
        entry.extend(["--" + key, str(value)])
    environment = {
        "LITE_CI_JOB_ID": node["task_marker"],
        "ASCEND_RT_VISIBLE_DEVICES": ",".join(map(str, node["device_ids"])),
        "NODE_IPS": ",".join(n["node_ip"] for n in nodes),
        "Network_Interface": node["network_interface"],
        "HCCL_SOCKET_IFNAME": "=" + node["network_interface"],
        "GLOO_SOCKET_IFNAME": "=" + node["network_interface"],
        "HCCL_IF_IP": node["node_ip"],
        "PYTHONPATH": node["pythonpath"],
        "PYTHONUNBUFFERED": "1",
        "CKPT_SAVE_LOAD_PATH": dest + "/checkpoints",
        **{env: str(runtime["ports"][field]) for field, env in PORT_FIELDS.items()},
    }
    remaining = int(runtime["deadline"] - time.time())
    if remaining < 1:
        raise TimeoutError("GITCODE_REMOTE_DEADLINE_EXPIRED")
    limit = min(
        remaining, item["execution_timeout_seconds"] if phase == "launch" else 90
    )
    exports = " ".join(key + "=" + quote(value) for key, value in environment.items())
    setup = runtime["cleanup"]["ascend_set_env_path"]
    inner = (
        "source "
        + quote(setup)
        + " >/dev/null 2>&1 && export "
        + exports
        + " && cd "
        + quote(dest + "/runner")
        + " && exec timeout --signal=TERM --kill-after=30s "
        + str(limit)
        + "s "
        + shlex.join(entry)
    )
    # Clear container inheritance as well as the dispatcher's environment.
    return (
        "env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin bash --noprofile --norc -c "
        + quote(inner)
    )


def launch_node(node, runtime, item):
    from ci_core.gitcode_supervisor import program

    verify_node(node)
    command = shlex.join(
        [
            node["python"],
            "-I",
            "-c",
            program(),
            "launch",
            node["destination"],
            node["task_marker"],
            runner_command(node, runtime, item, "launch"),
        ]
    )
    return transport_run(
        node["ssh"],
        "env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        + command,
        timeout=item["execution_timeout_seconds"] + 90,
        check=False,
    ).returncode


def request_stop(node):
    from ci_core.gitcode_supervisor import program

    # Publish a durable remote fence before scanning/killing owned processes.
    # A delayed SSH launcher that arrives after this call cannot Popen anymore.
    command = shlex.join(
        [
            node["python"],
            "-I",
            "-c",
            program(),
            "cancel-intent",
            node["destination"],
            node["task_marker"],
        ]
    )
    transport_run(
        node["ssh"],
        "env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
        + command,
        timeout=30,
    )
    stop_owned(node, node["destination"], node["task_marker"])


def verify_case_node(node, runtime, item):
    verify_node(node)
    transport_run(
        node["ssh"], runner_command(node, runtime, item, "verify"), timeout=120
    )
    script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); assert not p.is_symlink() and 0<p.stat().st_size<=131072; sys.stdout.write(p.read_text())"
    response = transport_run(
        node["ssh"],
        shlex.join(
            [
                node["python"],
                "-I",
                "-c",
                script,
                node["destination"] + "/output/case-evidence.json",
            ]
        ),
        timeout=30,
    )
    evidence = load_json(response.stdout.encode(), limit=131072)
    expected = {
        "schema": 1,
        "test_id": item["test_id"],
        "status": "PASS",
        "exit_code": 0,
        "source_sha": runtime["source_sha"],
        "tree_sha": runtime["tree_sha"],
        "lease_id": runtime["lease_id"],
        "fencing_token": runtime["fencing_token"],
        "task_marker": node["task_marker"],
        "node_rank": node["position"],
        "world_size": item["resources"]["nodes"]
        * item["resources"]["devices_per_node"],
    }
    if any(
        type(evidence.get(k)) is not type(v) or evidence.get(k) != v
        for k, v in expected.items()
    ):
        raise ValueError("GITCODE_NODE_EXECUTION_EVIDENCE_MISMATCH")
    if (
        item["kind"] == "npu_smoke"
        and evidence.get("all_reduce_sum")
        != expected["world_size"] * (expected["world_size"] - 1) // 2
    ):
        raise ValueError("GITCODE_HCCL_SUM_MISMATCH")
    return evidence


def _cleanup_captured_impl(runtime, *, stop, stream):
    """No source/Artifact/API reads; only immutable captured ID + process marker."""
    nodes = runtime["cleanup"]["nodes"]
    clean = True
    for node in nodes:
        try:
            verify_node(node)
            if stop:
                request_stop(node)
            owned_processes_gone(node, node["destination"], node["task_marker"])
        except Exception as exc:
            clean = False
            stream.write("GITCODE_CLEANUP_UNVERIFIED " + type(exc).__name__ + "\n")
    try:
        scan_idle(
            nodes,
            runtime["cleanup"]["ascend_set_env_path"],
            runtime["cleanup"]["process_indexing"],
            stream,
        )
    except Exception as exc:
        clean = False
        stream.write("GITCODE_DEVICE_RELEASE_UNVERIFIED " + type(exc).__name__ + "\n")
    stream.flush()
    return clean


def cleanup_captured(runtime, *, stop, stream, run_id=0, case_id=None):
    with timed_stage("cleanup", run_id=run_id, case_id=case_id):
        return _cleanup_captured_impl(runtime, stop=stop, stream=stream)


def execute(
    *,
    stored,
    item,
    case,
    execution_id,
    pools,
    root,
    cancel,
    capture,
    before_launch,
    phase,
):
    if not IDENT.fullmatch(execution_id) or not IDENT.fullmatch(item["case_id"]):
        raise ValueError("INVALID_SCHEMA4_EXECUTION_IDENTITY")
    if case is None or not case.get("enabled"):
        raise ValueError("VERIFIED_PHYSICAL_LEASE_REQUIRED")
    plan = stored.verified.plan
    timing_run_id = plan.get("github", {}).get("run_id", 0)
    with tempfile.TemporaryDirectory(prefix="lite-gitcode-verified-") as tmp:
        profile = bundle_profile(stored, item, Path(tmp))
    nodes = capture_nodes(case, pools)
    setup = profile["env_vars"]["ASCEND_SET_ENV_PATH"]
    # A busy external allocation remains a queueing condition, before any durable
    # execution lease or remote directory is created.
    scan_idle(
        nodes,
        setup,
        pools[case["pool"]]["process_indexing"],
        io.StringIO(),
        audit_identity=(execution_id, item["case_id"]),
    )
    if cancel.is_set() or time.time() >= plan["deadline"]:
        raise InterruptedError("schema-4 request cancelled or expired before staging")
    lease_id = uuid.uuid4().hex
    marker = (
        "g4_"
        + hashlib.sha256(
            canonical([execution_id, item["case_id"], lease_id])
        ).hexdigest()
    )
    for node in nodes:
        node.update(
            task_marker=marker,
            destination=f"{DEST_ROOT}/gitcode-{marker}/node{node['position']}",
        )
    runtime = capture(
        {
            "lease_id": lease_id,
            "deadline": plan["deadline"],
            "source_sha": stored.source.tested_merge_sha,
            "tree_sha": stored.source.tested_tree_sha,
            "ports": {key: case[key] for key in PORT_FIELDS},
            "cleanup": {
                "nodes": nodes,
                "ascend_set_env_path": setup,
                "process_indexing": pools[case["pool"]]["process_indexing"],
            },
        }
    )
    if type(runtime.get("fencing_token")) is not int or runtime["fencing_token"] < 1:
        raise ValueError("DURABLE_FENCING_TOKEN_REQUIRED")
    directory = (
        Path(root) / "logs/runs/gitcode" / (execution_id + "_" + item["case_id"])
    )
    directory.mkdir(parents=True, exist_ok=True)
    logfile = directory / "pipeline.log"
    (directory / "runtime.json").write_bytes(canonical(runtime) + b"\n")
    rc, clean, launched = 125, True, False
    with logfile.open("w") as stream:
        try:
            phase("STAGING")
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                with timed_stage(
                    "stage_nodes",
                    run_id=timing_run_id,
                    case_id=item["case_id"],
                ):
                    list(
                        workers.map(
                            lambda node: stage_node(node, stored, runtime), nodes
                        )
                    )
            stored.verify_files()
            before_launch()  # API authority + current generation after ALL staging.
            if cancel.is_set() or time.time() >= plan["deadline"]:
                raise InterruptedError("schema-4 request expired after staging")
            phase("RUNNING")
            deadline = min(
                float(plan["deadline"]), time.time() + item["execution_timeout_seconds"]
            )
            next_authority = time.monotonic() + 10
            with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
                pending = set()
                try:
                    with timed_stage(
                        "os_launch_and_wait",
                        run_id=timing_run_id,
                        case_id=item["case_id"],
                    ):
                        for node in nodes:
                            before_launch()
                            if cancel.is_set():
                                raise InterruptedError(
                                    "schema-4 cancellation during launch"
                                )
                            pending.add(
                                workers.submit(launch_node, node, runtime, item)
                            )
                            launched = True
                        rc = 0
                        while pending:
                            if cancel.is_set():
                                raise InterruptedError(
                                    "schema-4 cancelled while running"
                                )
                            if time.time() >= deadline:
                                raise TimeoutError("schema-4 execution deadline")
                            if time.monotonic() >= next_authority:
                                before_launch()
                                next_authority = time.monotonic() + 10
                            done, pending = wait(
                                pending, timeout=1, return_when=FIRST_COMPLETED
                            )
                            for future in done:
                                code = future.result()
                                if code != 0:
                                    rc = code
                                    raise ChildProcessError("a captured node failed")
                except BaseException:
                    # Stop siblings before the executor waits for blocked SSH threads.
                    clean = cleanup_captured(
                        runtime,
                        stop=True,
                        stream=stream,
                        run_id=timing_run_id,
                        case_id=item["case_id"],
                    )
                    raise
            if rc == 0:
                before_launch()
                evidence = [verify_case_node(node, runtime, item) for node in nodes]
                (directory / "case-evidence.json").write_bytes(
                    canonical(evidence) + b"\n"
                )
                stream.write(
                    "GITCODE_CASE_PASS "
                    + json.dumps(
                        {
                            "test_id": item["test_id"],
                            "source_sha": runtime["source_sha"],
                            "tree_sha": runtime["tree_sha"],
                            "nodes": len(nodes),
                        }
                    )
                    + "\n"
                )
        except InterruptedError:
            rc = 130
        except (TimeoutError, subprocess.TimeoutExpired):
            rc = 124
        except ChildProcessError:
            rc = rc or 1
        except Exception as exc:
            logging.warning("schema-4 execution failed (%s)", type(exc).__name__)
            stream.write("GITCODE_EXECUTION_ERROR " + type(exc).__name__ + "\n")
            rc = 125
        finally:
            phase("CLEANING")
            if launched:
                clean = (
                    cleanup_captured(
                        runtime,
                        stop=rc != 0,
                        stream=stream,
                        run_id=timing_run_id,
                        case_id=item["case_id"],
                    )
                    and clean
                )
            stream.write(
                f"CASE_RESULT {item['test_id']} {'PASS' if rc == 0 and clean else 'FAIL'} {rc}\n"
            )
            stream.flush()
    lines = logfile.read_text().splitlines()[-20:]
    return Execution(rc if clean else (rc or 125), lines, logfile, clean)
