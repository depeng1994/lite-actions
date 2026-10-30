"""Trusted worker ownership, canonical nodes, and existing-container bindings."""

import json
import re

from ci_core.transport import run


def format_pools(pools):
    text = json.dumps(pools, indent=2) + "\n"
    return re.sub(
        r'("(?:device_ids|devices)"\s*:\s*)(\[[^\]]*\])',
        lambda match: match[1] + json.dumps(json.loads(match[2])),
        text,
    )


def load(root, worker):
    workers = json.loads((root / "config/workers.json").read_text())
    pools = json.loads((root / "config/pools.json").read_text())
    if worker not in workers or not workers[worker].get("enabled"):
        raise ValueError("unknown/disabled worker")
    seen = {}
    fingerprints = {}
    for name, pool in pools.items():
        if pool.get("owner") not in workers:
            raise ValueError("unknown pool owner")
        reclaim = pool.get("force_reclaim", False)
        if type(reclaim) is not bool or (reclaim and (pool["owner"] != "yellow" or pool.get("type") != "ascend-a5")):
            raise ValueError("force_reclaim requires yellow ascend-a5 pool")
        for node in pool["nodes"]:
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
    return workers, pools


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
        'docker inspect --format \'{"Image":{{json .Image}},"State":{"Running":{{json .State.Running}}}}\' '
        + container
    )
    data = json.loads(run(node["ssh"], script, timeout=30).stdout)
    if not data.get("State", {}).get("Running") or data.get("Image") != env["image_id"]:
        raise ValueError("CONTAINER_IDENTITY_MISMATCH")
    import shlex

    actual = run(
        node["ssh"],
        "docker image inspect --format '{{.Id}}' " + shlex.quote(env["image_ref"]),
        timeout=30,
    ).stdout.strip()
    if actual != env["image_id"]:
        raise ValueError("IMAGE_TAG_CHANGED")
    execution_ssh = node["ssh"] + "/" + container
    if run(execution_ssh, "id -u", timeout=30).stdout.strip() != "0":
        raise ValueError("EXECUTION_PID_VISIBILITY_REQUIRED")
    return {**node, "ssh": execution_ssh}


def runtime_case(item, pools, worker):
    pool = pools.get(item["target_pool"])
    if not pool:
        raise ValueError("UNKNOWN_POOL")
    if pool["owner"] != worker:
        raise ValueError("POOL_OWNER_MISMATCH")
    if not pool.get("enabled"):
        raise ValueError("POOL_DISABLED")
    return dict(
        name=item["case_id"],
        pool=item["target_pool"],
        resources=item["resources"],
        enabled=True,
        docker=item["docker"],
        kind=item["kind"],
        timeout_seconds=item["execution_timeout_seconds"] + 780,
        execution_timeout_seconds=item["execution_timeout_seconds"],
        force_reclaim=pool.get("force_reclaim", False),
    )
