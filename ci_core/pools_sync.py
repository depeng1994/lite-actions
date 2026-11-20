"""Validate published Pools and calculate removals before a scheduler switch."""

import copy
import hashlib
import json

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
    failed = False

    def reason(error):
        value = str(error).strip().replace("\\n", " ")
        return (value or type(error).__name__)[:200]

    def mark_unavailable(node, failure_reason):
        node["paused"] = True
        node["probe_paused"] = True
        node["probe_unavailable"] = True
        node["probe_status"] = "unavailable"
        node["probe_reason"] = failure_reason
        for group in node.get("allocation_groups", []):
            group["paused"] = True
            group["probe_paused"] = True
            group["probe_unavailable"] = True
            group["verified"] = False
            group["probe_reason"] = failure_reason

    def clear_probe_marker(node):
        if node.pop("probe_paused", False):
            node.pop("paused", None)
        node.pop("probe_unavailable", None)
        node.pop("probe_status", None)
        node.pop("probe_reason", None)
        for group in node.get("allocation_groups", []):
            if group.pop("probe_paused", False):
                group.pop("paused", None)
            group.pop("probe_unavailable", None)
            group.pop("probe_reason", None)

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
            changed = any(
                node.get(field) != former.get(field) for field in binding_fields
            )
            if not changed:
                if former.get("probe_unavailable"):
                    mark_unavailable(
                        node,
                        former.get("probe_reason", "previous probe failed"),
                    )
                continue
            try:
                from scripts.preflight import probe_machine, probe_node

                if not fingerprint:
                    raise ValueError("MACHINE_FINGERPRINT_REQUIRED")
                environments = node.get("environments", {})
                if environments:
                    for environment in environments:
                        probe_node(node, environment)
                else:
                    probe_machine(node)
                clear_probe_marker(node)
            except Exception as error:
                failed = True
                mark_unavailable(node, reason(error))

    if failed:
        doc = copy.deepcopy(doc)
        doc["source_sha256"] = doc["sha256"]
        doc["pools"] = new
        config_json = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
        doc["config_json"] = config_json
        doc["sha256"] = hashlib.sha256(config_json.encode()).hexdigest()
    return doc


def affected_resources(groups):
    return set(
        resource_keys(
            [{"host_id": host, "devices": list(ids)} for _, host, ids in groups]
        )
    )
