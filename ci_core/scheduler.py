"""Phase 1 topology-aware allocation, with one global active training slot.

Reuses existing file locks. Only explicitly verified groups are eligible.
No partial distributed allocation is ever retained while waiting.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import time

from ci_core.resources import ResourceBusy, reserve, resource_keys
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
        return [[group] for group in eligible]
    # Do not guess that an arbitrary pair of individually valid groups is a
    # verified distributed topology. Multi-node combinations are allowlisted.
    known = list(pool.get("verified_multinode_allocations", []))
    # Explicit, operator-only experimental route: must NEVER be reported as
    # verified until the two-node real 5-step/TB test has succeeded. Do not
    # enable this flag on production. All normal device/lease checks still apply.
    if os.environ.get("A3_CI_ALLOW_EXPERIMENTAL_TOPOLOGY", "0") == "1":
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
        # Phase 1 is globally serial even after a dispatcher restart.
        # Unresolved prior Case training is not made safe by an unlocked flock.
        from ci_core.job_state import JobState
        quarantined_keys: set[tuple[str, int]] = set()
        for key, record in JobState(root).read().items():
            if key == owner_key or key.count(":") < 2:
                continue
            if record.get("phase") in ("running", "blocked_recovery"):
                raise UnsafeResourceState("prior Case execution requires reconciliation")
            if record.get("quarantined"):
                prior_allocation = record.get("allocation")
                if not prior_allocation:
                    raise UnsafeResourceState("unscoped device quarantine")
                quarantined_keys.update(resource_keys(prior_allocation))
        usable = [allocation for allocation in possibilities
                  if not quarantined_keys.intersection(resource_keys(allocation))]
        if not usable:
            raise UnsafeResourceState("all verified candidates overlap quarantined devices")
        blocked = []
        for allocation in usable:
            # ResourceBusy from a running task is not a device-lock conflict.
            # Never swallow execution failures and retry that training Case.
            entered_execution = False
            try:
                with reserve(root / "state/device-locks", allocation):
                    runtime = {**case, "allocation": allocation}
                    try:
                        remote_preflight(runtime, pools, ascend_env=ascend_env)
                    except Exception as exc:
                        # Formatting ambiguity and process ownership are both
                        # fail-closed: never run on an unverified occupied set.
                        blocked.append((allocation, type(exc).__name__))
                        continue
                    if canceled is not None and canceled():
                        raise ResourcesUnavailable("GitHub Run canceled after allocation")
                    logging.info("allocated Case %s: %s", case["name"], allocation)
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
        if not already_reclaimed and os.environ.get(
                "A3_CI_RECLAIM_EXTERNAL_PROCESSES", "0") == "1":
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
