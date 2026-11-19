"""Validate published Pools and calculate removals before a scheduler switch."""

from ci_core.config import checked_pools
from ci_core.resources import resource_keys


def removed_groups(worker, old, new):
    def groups(pools):
        return {
            (name, node["host_id"], tuple(sorted(group["devices"])))
            for name, pool in pools.items()
            if pool["owner"] == worker
            for node in pool["nodes"]
            for group in node.get("allocation_groups", [])
        }

    return groups(old) - groups(new)


def verify(worker, workers, old, envelope, command):
    revision = command["revision"]
    if (
        type(revision) is not int
        or envelope["revision"] != revision
        or envelope["sha256"] != command["sha256"]
    ):
        raise ValueError("POOL_REVISION_MISMATCH")
    doc = {
        "revision": revision,
        "sha256": envelope["sha256"],
        "pools": envelope["config"],
    }
    if "config_json" in envelope:
        doc["config_json"] = envelope["config_json"]
    new = checked_pools(doc, workers)
    local = {
        node["host_id"]: node
        for pool in old.values()
        if pool["owner"] == worker
        for node in pool["nodes"]
    }
    for pool in new.values():
        if pool["owner"] != worker:
            continue
        for node in pool["nodes"]:
            fingerprint = node.get("machine_fingerprint")
            host = node["host_id"]
            former = local.get(host, {})
            binding_fields = (
                "ssh",
                "host_ssh",
                "node_ip",
                "device_ids",
                "environments",
                "machine_fingerprint",
            )
            if any(node.get(field) != former.get(field) for field in binding_fields):
                from scripts.preflight import probe_machine, probe_node

                if not fingerprint:
                    raise ValueError("MACHINE_FINGERPRINT_REQUIRED")
                environments = node.get("environments", {})
                if environments:
                    for environment in environments:
                        probe_node(node, environment)
                else:
                    probe_machine(node)
    return doc


def affected_resources(groups):
    return set(
        resource_keys(
            [{"host_id": host, "devices": list(ids)} for _, host, ids in groups]
        )
    )
