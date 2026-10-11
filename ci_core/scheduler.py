"""Verified topology-aware all-or-nothing NPU and rendezvous-port leases.

Disjoint allocations can run simultaneously, even across independent nodes.
Overlapping distributed cases wait without reserving partial resources.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import time

from ci_core.resources import (
    ResourceBusy,
    reserve,
    resource_keys,
    reserve_ports,
    shared_resource_lock_root,
)
from ci_core.dispatch import remote_preflight


class ResourcesUnavailable(RuntimeError):
    pass


class UnsafeResourceState(ResourcesUnavailable):
    """Previous training may still be active or NPU cleanup was unverified."""


def candidates(case: dict, pools: dict) -> list[list[dict]]:
    spec = case["resources"]
    nodes, ngpu = spec["nodes"], spec["devices_per_node"]
    if nodes < 1 or ngpu < 1:
        raise ValueError("invalid requested topology")
    pool = pools[case["pool"]]
    eligible = []
    for host in sorted(pool["nodes"], key=lambda node: (node.get("priority", 0),
                                                         node["host_id"])):
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
    # Explicit, operator-only experimental route: must NEVER be reported as
    # verified until the two-node real 5-step/TB test has succeeded. Do not
    # enable this flag on production. All normal device/lease checks still apply.
    if os.environ.get("LITE_ACTIONS_ALLOW_EXPERIMENTAL_TOPOLOGY", "0") == "1":
        known += pool.get("experimental_multinode_allocations", [])
    allowed_groups = {(g["host_id"], tuple(g["devices"])) for g in eligible}
    result = []
    for bundle in known:
        if (len(bundle) == nodes
                and len({g["host_id"] for g in bundle}) == nodes
                and all(len(g["devices"]) == ngpu
                        and (g["host_id"], tuple(g["devices"])) in allowed_groups
                        for g in bundle)):
            result.append([{"host_id": g["host_id"], "devices": list(g["devices"])}
                           for g in bundle])
    return result


def validate_allocation(case: dict, pools: dict, allocation: list[dict]) -> None:
    expected = resource_keys(allocation)
    if not expected or not any(resource_keys(item) == expected
                               for item in candidates(case, pools)):
        raise ValueError("allocation outside verified topology or pool")


@contextmanager
def claim_dynamic(root: Path, case: dict, *, ascend_env: str | None,
                  timeout: float = 900, sleep: float = 3.0,
                  owner_key: str | None = None, canceled=None):
    """Hold device and port leases throughout staging, training and cleanup."""
    with _claim_dynamic_locked(root, case, ascend_env=ascend_env,
                               timeout=timeout, sleep=sleep,
                               owner_key=owner_key, canceled=canceled) as runtime:
        yield runtime


@contextmanager
def _claim_dynamic_locked(root: Path, case: dict, *, ascend_env: str | None,
                          timeout: float, sleep: float,
                          owner_key: str | None, canceled=None):
    """Choose an entire verified candidate, lock, preflight, then hold to exit.

    Candidate probing never blocks a partial device set. After normal expiry,
    callers distinguish resource timeout from model/test failures.
    """
    pools = json.loads((root / "config/pools.json").read_text())
    possibilities = candidates(case, pools)
    if not possibilities:
        raise ValueError("no VERIFIED candidate supports this topology")
    deadline = time.monotonic() + timeout
    already_reclaimed = False
    while True:
        if canceled is not None and canceled():
            raise ResourcesUnavailable("GitHub Run canceled before allocation")
        # NPU leases protect concurrently running cases; unresolved remote
        # children still block any overlapping allocation (fail closed).
        from ci_core.job_state import JobState
        quarantined_keys: set[tuple[str, int]] = set()
        active_keys: set[tuple[str, int]] = set()
        active_hosts: set[str] = set()
        for key, record in JobState(root).read().items():
            owner_run_key = (owner_key.rsplit(":", 1)[0]
                             if owner_key and owner_key.count(":") >= 2
                             else owner_key)
            if key == owner_key or key == owner_run_key:
                continue
            prior_allocation = record.get("allocation")
            if record.get("phase") == "blocked_recovery" and not prior_allocation:
                raise UnsafeResourceState("unscoped recovery requires reconciliation")
            if record.get("quarantined") or record.get("phase") == "blocked_recovery":
                if not prior_allocation:
                    raise UnsafeResourceState("unscoped device quarantine")
                quarantined_keys.update(resource_keys(prior_allocation))
            elif record.get("phase") == "running" and prior_allocation:
                # A live overlapping CI job is temporary contention, not an
                # unsafe orphan. Wait and retry rather than fail or reclaim it.
                active_keys.update(resource_keys(prior_allocation))
                active_hosts.update(item["host_id"] for item in prior_allocation)
        usable = [allocation for allocation in possibilities
                  if not quarantined_keys.intersection(resource_keys(allocation))]
        if not usable:
            raise UnsafeResourceState("all verified candidates overlap quarantined devices")
        # Prefer an unused host when two disjoint 8P groups are available.
        # This reduces shared CPU/HCCL-port contention without forbidding
        # same-node parallelism when all alternative hosts are occupied.
        usable.sort(key=lambda allocation: sum(n["host_id"] in active_hosts
                                              for n in allocation))
        blocked = []
        for allocation in usable:
            if active_keys.intersection(resource_keys(allocation)):
                blocked.append((allocation, "leased"))
                continue
            # ResourceBusy from a running task is not a device-lock conflict.
            # Never swallow execution failures and retry that training Case.
            entered_execution = False
            try:
                with reserve(shared_resource_lock_root() / "devices", allocation):
                    with reserve_ports(shared_resource_lock_root() / "ports", allocation) as ports:
                        runtime = {**case, "allocation": allocation, **ports}
                        try:
                            remote_preflight(runtime, pools, ascend_env=ascend_env)
                        except Exception as exc:
                            # Never start on unverified external occupancy.
                            blocked.append((allocation, type(exc).__name__))
                            continue
                        if canceled is not None and canceled():
                            raise ResourcesUnavailable("GitHub Run canceled after allocation")
                        logging.info("allocated Case %s: %s ports=%s",
                                     case["name"], allocation, ports)
                        entered_execution = True
                        yield runtime
                        return
            except ResourceBusy:
                if entered_execution:
                    raise
                blocked.append((allocation, "leased"))
                continue
        # Prefer genuinely idle legal groups before reclaiming auto/vllm.
        # Only one bounded reclaim pass per task to avoid a watchdog kill-loop.
        # Only operator-approved resource pools can reclaim external auto/vLLM.
        # The global toggle can disable this capability, never enable an
        # unapproved pool (including the disabled A5 laboratory pool).
        if (not already_reclaimed
                and pools[case["pool"]].get("reclaim_external_processes", False) is True
                and os.environ.get("LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES", "1") == "1"):
            from ci_core.occupancy import reclaim_candidate
            for allocation, reason in blocked:
                if reason == "leased":
                    continue
                try:
                    runtime = {**case, "allocation": allocation}
                    if reclaim_candidate(runtime, pools, ascend_env=ascend_env):
                        logging.warning("reclaimed trusted external occupying task: %s", allocation)
                        break
                except Exception as exc:
                    logging.warning("reclaim candidate rejected: %s", type(exc).__name__)
            already_reclaimed = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResourcesUnavailable("waited for verified NPU capacity; still busy")
        time.sleep(min(sleep, remaining))
