"""Discover complete, verified resource allocations for the scheduler."""

from ci_core.resources import resource_keys


def candidates(case: dict, pools: dict) -> list[list[dict]]:
    spec = case["resources"]
    nodes, ngpu = spec["nodes"], spec["devices_per_node"]
    if nodes < 1 or ngpu < 1:
        raise ValueError("invalid requested topology")
    pool = pools[case["pool"]]
    eligible = []
    for host in sorted(
        pool["nodes"], key=lambda node: (node.get("priority", 0), node["host_id"])
    ):
        for group in host.get("allocation_groups", []):
            devices = group["devices"]
            if group.get("verified") and len(devices) == ngpu:
                eligible.append({"host_id": host["host_id"], "devices": list(devices)})
    if nodes == 1:
        # Round-robin verified groups across hosts before taking a second
        # group on one host. This works even when two workers allocate at the
        # same instant, before either has persisted its state record.
        group_index: dict[str, int] = {}
        spread = []
        for group in eligible:
            host = group["host_id"]
            ordinal = group_index.get(host, 0)
            group_index[host] = ordinal + 1
            spread.append((ordinal, group))
        return [[group] for _, group in sorted(spread, key=lambda pair: pair[0])]
    # Do not guess that an arbitrary pair of individually valid groups is a
    # verified distributed topology. Multi-node combinations are allowlisted.
    known = list(pool.get("verified_multinode_allocations", []))
    allowed_groups = {(g["host_id"], tuple(g["devices"])) for g in eligible}
    result = []
    for bundle in known:
        if (
            len(bundle) == nodes
            and len({g["host_id"] for g in bundle}) == nodes
            and all(
                len(g["devices"]) == ngpu
                and (g["host_id"], tuple(g["devices"])) in allowed_groups
                for g in bundle
            )
        ):
            result.append(
                [
                    {"host_id": g["host_id"], "devices": list(g["devices"])}
                    for g in bundle
                ]
            )
    return result


def validate_allocation(case: dict, pools: dict, allocation: list[dict]) -> None:
    expected = resource_keys(allocation)
    if not expected or not any(
        resource_keys(item) == expected for item in candidates(case, pools)
    ):
        raise ValueError("allocation outside verified topology or pool")
