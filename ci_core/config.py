"""Trusted worker ownership, canonical nodes, and existing-container bindings."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from ci_core.contract import parse_worker_identity
from ci_core.permissions import NON_OWNER_WRITE_BITS
from ci_core.transport import run


def format_pools(pools):
    text = json.dumps(pools, indent=2) + "\n"
    return re.sub(
        r'("(?:device_ids|devices)"\s*:\s*)(\[[^\]]*\])',
        lambda match: match[1] + json.dumps(json.loads(match[2])),
        text,
    )


def legacy_reclaim(pool, node):
    """Preserve the existing Yellow A5 reclaim path for unmodified nodes."""
    return (
        pool.get("owner") == "yellow"
        and pool.get("type") == "ascend-a5"
        and "occupancy_policy" not in node
    )


def node_preempts(pool, node):
    policy = node.get("occupancy_policy", "inherit")
    if policy not in ("inherit", "wait", "preempt") or (
        policy == "inherit" and "occupancy_policy" in node
    ):
        raise ValueError("invalid node occupancy policy")
    reclaim = (
        pool.get("force_reclaim", False) if policy == "inherit" else policy == "preempt"
    )
    expected = {"ascend-a3": "card-die", "ascend-a5": "card"}
    kind = pool.get("type")
    if reclaim and (
        kind not in ("ascend-a3", "ascend-a5")
        or expected[kind] != pool.get("process_indexing")
    ):
        raise ValueError("preemption requires a verified NPU process index")
    if reclaim and not legacy_reclaim(pool, node):
        devices = node.get("device_ids")
        if (
            not isinstance(node.get("machine_fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", node["machine_fingerprint"])
            or not isinstance(devices, list)
            or not devices
            or any(type(d) is not int or d < 0 for d in devices)
            or len(set(devices)) != len(devices)
        ):
            raise ValueError(
                "node preemption requires host fingerprint and device list"
            )
    return reclaim


def load(root, worker):
    zone = parse_worker_identity(worker)["zone"]
    workers = json.loads((root / "config/workers.json").read_text())
    pools = json.loads((root / "config/pools.json").read_text())
    if zone not in workers or not workers[zone].get("enabled"):
        raise ValueError("unknown/disabled worker")
    validate_pools(pools, workers)
    return workers, pools


def validate_pools(pools, workers):
    if not isinstance(pools, dict) or len(pools) > 32:
        raise ValueError("invalid Pools document")
    seen = {}
    fingerprints = {}
    for pool in pools.values():
        if "default_docker" in pool and (
            not isinstance(pool["default_docker"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", pool["default_docker"])
        ):
            raise ValueError("invalid pool default_docker")
        if "paused" in pool and pool["paused"] is not True:
            raise ValueError("paused must be true when specified")
        if pool.get("owner") not in workers:
            raise ValueError("unknown pool owner")
        reclaim = pool.get("force_reclaim", False)
        if type(reclaim) is not bool:
            raise ValueError("force_reclaim must be a boolean")
        for node in pool["nodes"]:
            if "paused" in node and node["paused"] is not True:
                raise ValueError("paused must be true when specified")
            for group in node.get("allocation_groups", []):
                if "paused" in group and group["paused"] is not True:
                    raise ValueError("paused must be true when specified")
            node_preempts(pool, node)
            pythonpath = node.get("pythonpath")
            if pythonpath is not None and (
                not isinstance(pythonpath, str)
                or not pythonpath.startswith("/")
                or any(c in pythonpath for c in ":\r\n\0")
            ):
                raise ValueError(
                    "node pythonpath must be one absolute dependency directory"
                )
            capabilities = node.get("capabilities", ["train", "npu_smoke"])
            if (
                not isinstance(capabilities, list)
                or any(
                    not isinstance(k, str) or k not in {"train", "npu_smoke"}
                    for k in capabilities
                )
                or len(capabilities) != len(set(capabilities))
            ):
                raise ValueError("invalid node capabilities")
            ident = node["host_id"]
            if not re.fullmatch(
                r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[1-9][0-9]{0,4}", ident
            ):
                raise ValueError("canonical node must be IPv4:port")
            import ipaddress

            address, port = ident.rsplit(":", 1)
            ipaddress.ip_address(address)
            if int(port) > 65535:
                raise ValueError("invalid node port")
            if ident in seen:
                raise ValueError("duplicate physical node")
            seen[ident] = pool["owner"]
            fp = node.get("machine_fingerprint")
            if fp:
                if fp in fingerprints:
                    raise ValueError("duplicate physical fingerprint")
                fingerprints[fp] = ident


def checked_pools(document, workers):
    config_json = document.get("config_json")
    pools = document["pools"]
    if config_json is None:
        config_json = json.dumps(pools, ensure_ascii=False, separators=(",", ":"))
    if (
        not isinstance(config_json, str)
        or hashlib.sha256(config_json.encode()).hexdigest() != document.get("sha256")
        or json.loads(config_json) != pools
    ):
        raise ValueError("POOLS_SHA256_MISMATCH")
    validate_pools(pools, workers)
    return pools


def load_active_pools(root, worker, state_dir=None):
    identity = parse_worker_identity(worker)
    zone = identity["zone"]
    override_pools = (
        os.environ.get("LITE_ACTIONS_ACTIVE_POOLS_PATH", "").strip()
        if identity["is_test"]
        else ""
    )
    directory = Path(
        state_dir or os.environ.get("LITE_ACTIONS_STATE_DIR") or (root / "state")
    )
    path = Path(override_pools) if override_pools else (directory / "pools-active.json")
    if not path.exists():
        workers, pools = load(root, worker)
        return workers, pools, 0
    workers = json.loads((root / "config/workers.json").read_text())
    if zone not in workers or not workers[zone].get("enabled"):
        raise ValueError("unknown/disabled worker")
    document = json.loads(path.read_text())
    revision = document.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("INVALID_POOLS_REVISION")
    return workers, checked_pools(document, workers), revision


def execution_pools(root, case):
    if "pools_snapshot" in case:
        return case["pools_snapshot"]
    return json.loads((root / "config/pools.json").read_text())


def _run_node_probe(host, script, *, timeout=45):
    """Run one read-only container check with retries on transient SSH failure."""
    import time

    for attempt, delay in enumerate((3, 6, 12, 0)):
        try:
            return run(host, script, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt == 3:
                raise
            time.sleep(delay)
        except RuntimeError as exc:
            if attempt == 3 or "exit=255" not in str(exc):
                raise
            time.sleep(delay)


def execution_node(node, environment):
    env = node.get("environments", {}).get(environment)
    if not env or not all(
        env.get(k) for k in ("container_id", "image_id", "image_ref")
    ):
        raise ValueError("CONTAINER_BINDING_MISSING")
    container = env["container_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise ValueError("container must be pinned by ID")
    script = (
        'docker inspect --format \'{"Name":{{json .Name}},"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}}}}\' '
        + container
    )
    data = json.loads(_run_node_probe(node["ssh"], script, timeout=45).stdout)
    if not data.get("State", {}).get("Running") or data.get("Image") != env["image_id"]:
        raise ValueError("CONTAINER_IDENTITY_MISMATCH")
    import shlex

    actual = _run_node_probe(
        node["ssh"],
        "docker image inspect --format '{{.Id}}' " + shlex.quote(env["image_ref"]),
        timeout=45,
    ).stdout.strip()
    if actual != env["image_id"]:
        raise ValueError("IMAGE_TAG_CHANGED")
    execution_ssh = node["ssh"] + "/" + container
    if _run_node_probe(execution_ssh, "id -u", timeout=45).stdout.strip() != "0":
        raise ValueError("EXECUTION_PID_VISIBILITY_REQUIRED")
    return {**node, "ssh": execution_ssh}


class PoolDefaultError(ValueError):
    """A missing or ambiguous deployment-owned container binding."""


def resolve_pool_default_docker(pool, *, kind):
    if "default_docker" in pool:
        return pool["default_docker"]
    bindings = {
        name
        for node in pool["nodes"]
        if not node.get("paused")
        and not node.get("probe_unavailable")
        and kind in node.get("capabilities", ["train", "npu_smoke"])
        and any(
            group.get("verified")
            and not group.get("paused")
            and not group.get("probe_unavailable")
            for group in node.get("allocation_groups", [])
        )
        for name, binding in node.get("environments", {}).items()
        if binding
    }
    if len(bindings) != 1:
        raise PoolDefaultError(
            "DEFAULT_DOCKER_AMBIGUOUS" if bindings else "CONTAINER_BINDING_MISSING"
        )
    return next(iter(bindings))


def runtime_case(item, pools, worker):
    identity = parse_worker_identity(worker)
    if "worker" in item and item["worker"] != worker:
        raise ValueError("CASE_WORKER_MISMATCH")
    pool = pools.get(item["target_pool"])
    if not pool:
        raise ValueError("UNKNOWN_POOL")
    if pool["owner"] != identity["zone"]:
        raise ValueError("POOL_OWNER_MISMATCH")
    docker = item["docker"]
    if docker is None and item["kind"] != "worker_probe":
        docker = resolve_pool_default_docker(pool, kind=item["kind"])
    return dict(
        name=item["case_id"],
        pool=item["target_pool"],
        resources=item["resources"],
        enabled=True,
        docker=docker,
        kind=item["kind"],
        timeout_seconds=item["execution_timeout_seconds"] + 780,
        execution_timeout_seconds=item["execution_timeout_seconds"],
        force_reclaim=pool.get("force_reclaim", False),
    )


def identify_worker(addresses, assigned, asserted=None):
    """Derive the role from exact host IPv4s; never infer it from network reachability."""
    from ipaddress import IPv4Address

    if set(assigned) != {"blue", "yellow"} or len(set(assigned.values())) != 2:
        raise ValueError("WORKER_IDENTITY_CONFIG_INVALID")
    for ip in assigned.values():
        if not isinstance(ip, str) or str(IPv4Address(ip)) != ip:
            raise ValueError("WORKER_IDENTITY_CONFIG_INVALID")
    expected_zone = None
    if asserted is not None:
        expected_zone = parse_worker_identity(asserted)["zone"]
    matched = [w for w, ip in assigned.items() if ip in addresses]
    if len(matched) != 1 or expected_zone is not None and expected_zone != matched[0]:
        raise ValueError("WORKER_IDENTITY_MISMATCH")
    return asserted if asserted is not None else matched[0]


def deployment(asserted=None):
    """Read operator-owned configuration before any provider credentials or HTTP."""
    import os
    import stat
    import subprocess
    from pathlib import Path

    path = Path("/etc/lite-actions/deployment.json")
    for parent in path.parents:
        st = parent.lstat()
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != 0
            or st.st_mode & NON_OWNER_WRITE_BITS
        ):
            raise ValueError("UNTRUSTED_DEPLOYMENT_DIRECTORY")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        data = f.read(65537) if stat.S_ISREG(st.st_mode) else b""
        if (
            not data
            or len(data) > 65536
            or st.st_uid != 0
            or st.st_mode & NON_OWNER_WRITE_BITS
        ):
            raise ValueError("UNTRUSTED_DEPLOYMENT_CONFIG")
    from ci_core.registry import _unique

    doc = json.loads(data, object_pairs_hook=_unique)
    locals_ = json.loads(
        subprocess.check_output(["ip", "-j", "-4", "address", "show"], timeout=5)
    )
    ips = {
        a["local"]
        for link in locals_
        for a in link["addr_info"]
        if a.get("family") == "inet"
    }
    worker = identify_worker(ips, doc["addresses"], asserted)
    identity = parse_worker_identity(worker)
    zone = identity["zone"]
    is_test = identity["is_test"]
    cfg = doc["workers"][zone]
    if "control_center" in cfg:
        from ci_core.egress_policy import control_origin, control_proxy

        control = cfg["control_center"]
        if not isinstance(control, dict) or set(control) - {
            "url",
            "ci_reserve_bytes",
            "proxy",
        }:
            raise ValueError("INVALID_CONTROL_CENTER_CONFIG")
        control_origin(control.get("url", ""))
        if "proxy" in control:
            control_proxy(control["proxy"])
        reserve = control.get("ci_reserve_bytes", 262144)
        if type(reserve) is not int or reserve < 0:
            raise ValueError("INVALID_CONTROL_CENTER_RESERVE")
    zone_state = Path(cfg["state_dir"])
    if is_test:
        raw_state = os.environ.get("LITE_ACTIONS_STATE_DIR", "").strip()
        if not raw_state:
            raise ValueError("STATE_OWNER_MISMATCH")
        state = Path(raw_state)
        if not state.is_absolute() or state.resolve() != state or state == zone_state:
            raise ValueError("STATE_OWNER_MISMATCH")
    else:
        state = zone_state
        if (
            not state.is_absolute()
            or state.resolve() != state
            or Path(os.environ.get("LITE_ACTIONS_STATE_DIR", str(state))) != state
        ):
            raise ValueError("STATE_OWNER_MISMATCH")
    # Preprovision this marker even for a fresh worker. Never adopt an old directory by writing a marker at startup.
    if (
        not state.is_dir()
        or state.stat().st_uid not in {0, os.geteuid()}
        or state.stat().st_mode & NON_OWNER_WRITE_BITS
    ):
        raise ValueError("UNTRUSTED_STATE_DIRECTORY")
    owner = state / "worker-id"
    st = owner.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid not in {0, os.geteuid()}
        or st.st_size > 64
        or st.st_mode & NON_OWNER_WRITE_BITS
        or owner.read_text().strip() != worker
    ):
        raise ValueError("STATE_OWNER_MISMATCH")
    if zone == "yellow" and (
        type(cfg.get("daily_upload_bytes")) is not int or cfg["daily_upload_bytes"] < 1
    ):
        raise ValueError("YELLOW_DAILY_QUOTA_REQUIRED")
    statefile = state / "unified-jobs.json"
    if statefile.exists():
        from ci_core.job_state import _META

        records = json.loads(statefile.read_bytes(), object_pairs_hook=_unique)
        for key, record in records.items():
            if (
                key != _META
                and record.get("phase") != "REPORTED"
                and record.get("item", {}).get("worker") != worker
            ):
                raise ValueError("STATE_OWNER_MISMATCH")
    return worker, {
        **cfg,
        "state_dir": str(state),
        "zone_state_dir": str(zone_state),
        "zone": zone,
        "worker_id": worker,
        "is_test": is_test,
        "address": doc["addresses"][zone],
    }
