"""Independent Pull workers sharing one schedule/execute implementation."""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path

from ci_core.config import PoolDefaultError, load_active_pools, runtime_case
from ci_core.contract import (
    LIFECYCLE_KINDS,
    PR_SMOKE_WORKFLOW,
    REPO,
    SELF_REPO,
    SELF_WORKFLOW,
    configured_repos,
    parse_plan,
    parse_worker_identity,
)
from ci_core.executor import ExternalNpuOccupancy, execute
from ci_core.gitcode import RunChanged
from ci_core.job_state import JobState, ordered_queued
from ci_core.plan import verify_definition
from ci_core.reporting import (
    body_for,
    post,
    progress,
    progress_body,
    report,
    result_sha,
)
from ci_core.resources import (
    ResourceBusy,
    reserve,
    reserve_host_budget,
    reserve_ports,
    resource_keys,
    shared_resource_lock_root,
)
from ci_core.scheduler import candidates
from ci_core.ssh_runner import model_archive
from ci_core.worker_probe import probe as probe_worker


def _is_self_smoke(run):
    return (
        run.get("_ci_provider", "github") == "github"
        and run.get("_ci_repo") == SELF_REPO
        and run.get("path") == f".github/workflows/{SELF_WORKFLOW}"
    )


def _is_pr_smoke(run):
    return (
        run.get("_ci_provider", "github") == "github"
        and run.get("_ci_repo") == SELF_REPO
        and run.get("path") == f".github/workflows/{PR_SMOKE_WORKFLOW}"
    )


def _verify_self_smoke_run(client, run, *, plan=None, expected_digest=None):
    if not _is_self_smoke(run):
        raise ValueError("self-smoke run identity invalid")
    event = run.get("_ci_platform_event", run.get("event"))
    branch = run.get("_ci_platform_head_branch", run.get("head_branch"))
    sha = run.get("_ci_platform_run_sha", run.get("head_sha"))
    if event not in {"push", "workflow_dispatch"} or branch != "main":
        raise ValueError("self-smoke requires push or workflow_dispatch on main")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("self-smoke commit SHA invalid")
    if client is None:
        raise ValueError("self-smoke GitHub client unavailable")
    main_head = client.request("GET", "/branches/main").get("commit", {}).get("sha")
    if not isinstance(main_head, str) or not re.fullmatch(r"[0-9a-f]{40}", main_head):
        raise ValueError("main branch SHA invalid")
    if sha != main_head:
        comparison = client.request("GET", f"/compare/{sha}...{main_head}?per_page=1")
        if comparison.get("merge_base_commit", {}).get("sha") != sha:
            raise ValueError("self-smoke commit is not on main")
    from ci_core.self_smoke import expected_plan

    approved = expected_plan(run)
    if plan is not None and plan != approved:
        raise ValueError("self-smoke plan differs from its pinned profile")
    if expected_digest is not None and expected_digest != approved["plan_digest"]:
        raise ValueError("self-smoke digest differs from its pinned profile")
    return approved


def task_key(run, item):
    prefix = "gitcode:" if run.get("_ci_provider") == "gitcode" else ""
    dispatch = f":{run['_ci_dispatch_id']}" if run.get("_ci_dispatch_id") else ""
    return f"{prefix}{run.get('_ci_repo', REPO)}:{run['id']}:{run.get('run_attempt', 1)}{dispatch}:{item['case_id']}"


def run_key(run):
    return (
        run.get("_ci_provider", "github"),
        run.get("_ci_repo", REPO),
        run["id"],
        run.get("run_attempt", 1),
        run.get("_ci_dispatch_id"),
    )


def eligible_topologies(case, pools, *, include_paused=False):
    options = candidates(case, pools, include_paused=include_paused)
    if not options:
        raise ValueError("NO_VERIFIED_TOPOLOGY")
    inventory = {n["host_id"]: n for n in pools[case["pool"]]["nodes"]}
    options = [
        allocation
        for allocation in options
        if all(
            case.get("kind", "train")
            in inventory[n["host_id"]].get("capabilities", ["train", "npu_smoke"])
            for n in allocation
        )
    ]
    if not options:
        raise ValueError("NO_CAPABLE_TOPOLOGY")
    if case.get("docker"):
        options = [
            a
            for a in options
            if all(
                inventory[n["host_id"]].get("environments", {}).get(case["docker"])
                for n in a
            )
        ]
        if not options:
            raise ValueError("CONTAINER_BINDING_MISSING")
    return options


def queue_telemetry(worker, pools, records, quarantine):
    from ci_core.idle_probe import group_id

    zone = parse_worker_identity(worker)["zone"]
    blocked = set()
    if quarantine:
        for record in records.values():
            if record.get("quarantined"):
                blocked.update(resource_keys(record.get("allocation") or []))
    rows = {}
    indexes = {
        name: {
            group_id(name, node["host_id"], group["devices"]): idx
            for idx, (node, group) in enumerate(
                (node, group)
                for node in pool["nodes"]
                for group in node.get("allocation_groups", [])
            )
        }
        for name, pool in pools.items()
        if pool["owner"] == zone
    }
    for key, record in ordered_queued(records):
        item = record["item"]
        pool, kind = item["target_pool"], item["kind"]
        if kind not in {"train", "npu_smoke"}:
            continue
        queue = rows.setdefault((pool, kind), [])
        try:
            case = runtime_case(item, pools, worker)
            options = eligible_topologies(case, pools)
        except ValueError:
            options = []
        indices = [
            [
                indexes[pool][group_id(pool, host["host_id"], host["devices"])]
                for host in allocation
            ]
            for allocation in options
            if not blocked.intersection(resource_keys(allocation))
        ]
        queue.append(
            {
                "tid": hashlib.sha256(key.encode()).hexdigest()[:12],
                "pos": len(queue),
                "ix": indices,
            }
        )
    return rows


TRAIN_QUEUE_TIMEOUT_SECONDS = 72 * 3600


def queue_timeout(kind):
    return {
        "train": TRAIN_QUEUE_TIMEOUT_SECONDS,
        "worker_probe": 3600,
        "test_worker_deploy": 3600,
        "test_worker_cleanup": 3600,
    }.get(kind)


class Router:
    def __init__(
        self,
        root,
        worker,
        client,
        author,
        *,
        deployment_config=None,
        broker_client=None,
    ):
        self.identity = parse_worker_identity(worker)
        self.zone = self.identity["zone"]
        self.is_test = self.identity["is_test"]
        self.broker = broker_client
        self._broker_active = broker_client is not None
        self.control = None
        self.idle_probe = None
        self.coordinator = None
        self._coordinator_active = False
        if not self.is_test and not self._broker_active:
            from ci_core.resources import PhysicalResourceCoordinator

            self.coordinator = PhysicalResourceCoordinator(shared_resource_lock_root())
            self._coordinator_active = True
            self.coordinator.clear_production_waiters_for_owner(worker)
            self.coordinator.clear_drain_state(self.zone)
        self.control_healthy = True
        self.deployment = deployment_config
        self.release_logs = None
        self.next_expiry = 0.0
        if (
            not self.is_test
            and self.zone == "blue"
            and deployment_config
            and deployment_config.get("public_release")
        ):
            from ci_core.github import ReleaseLogs

            self.release_logs = ReleaseLogs(
                self.zone,
                deployment_config,
                token=os.environ.get("LITE_ACTIONS_BLUE_RELEASE_TOKEN"),
            )
        if not author:
            raise ValueError("LITE_ACTIONS_RESULT_AUTHOR is required")
        self.root, self.worker, self.client, self.author = (
            Path(root),
            worker,
            client,
            author,
        )
        self.workers, self.pools, self.pools_revision = load_active_pools(
            self.root, worker
        )
        self.config_lock = threading.Lock()
        self.config_verification = None
        self.config_executor = None
        self.draining = set()
        if deployment_config is not None:
            self.workers[self.zone]["case_environments"] = deployment_config[
                "case_environments"
            ]
        self.release_sha = os.environ.get("LITE_ACTIONS_RELEASE_SHA", "").strip()
        if not self.release_sha and (self.root / ".git").exists():
            try:
                from ci_core.worker_ready import actual_sha

                self.release_sha = actual_sha(self.root)
            except (OSError, ValueError, subprocess.SubprocessError):
                self.release_sha = ""
        self.next_discovery = 0.0
        self.state = JobState(self.root)
        disabled_quarantine = {
            name.strip()
            for name in os.environ.get(
                "LITE_ACTIONS_DISABLE_QUARANTINE_WORKERS", ""
            ).split(",")
            if name.strip()
        }
        self.quarantine_enabled = (
            worker not in disabled_quarantine and self.zone not in disabled_quarantine
        )
        self.supervisor = None
        self._supervisor_active = False
        if not self.is_test and not self._broker_active:
            from ci_core.test_supervisor import TestSupervisor

            gitcode_repos = tuple(
                filter(
                    None, os.environ.get("LITE_ACTIONS_GITCODE_REPOS", "").split(",")
                )
            )
            repos = (
                tuple(deployment_config.get("repos") or configured_repos())
                if deployment_config
                else configured_repos()
            )
            self.supervisor = TestSupervisor(
                zone=self.zone,
                main_root=self.root,
                state_dir=self.state.path.parent,
                pools=self.pools,
                pools_revision=self.pools_revision,
                workers=self.workers,
                author=self.author,
                address=(
                    deployment_config.get("address", "127.0.0.1")
                    if deployment_config
                    else "127.0.0.1"
                ),
                release_sha=self.release_sha,
                client=self.client,
                coordinator=self.coordinator,
                repos=repos,
                gitcode_repos=gitcode_repos,
                control_provider=lambda: self.control,
                quarantine_enabled=self.quarantine_enabled,
            )
            self._supervisor_active = True
            if deployment_config is not None:
                self.supervisor.recover()
        if any(
            r.get("protocol", 3) != 3 and r.get("phase") != "REPORTED"
            for r in self.state.read().values()
        ):
            raise ValueError("legacy nonterminal state requires prior reconciliation")
        if any(
            r.get("run", {}).get("_ci_catalog")
            and r.get("phase") not in {"REPORTED", "PENDING_REPORT"}
            for r in self.state.read().values()
        ):
            raise ValueError(
                "retired JSON pilot has unfinished tasks; drain before upgrade"
            )
        self.training = ThreadPoolExecutor(max_workers=4)
        self.light = ThreadPoolExecutor(max_workers=1)
        self.active = {}
        self.preflight = {}
        self.plans = {}
        self.unavailable_runs = set()
        if not self.quarantine_enabled:
            owners = {
                node["host_id"]: pool["owner"]
                for pool in self.pools.values()
                for node in pool["nodes"]
            }
            clear = []
            for key, record in self.state.read().items():
                item = record.get("item")
                owner = item.get("worker") if isinstance(item, dict) else None
                allocation = record.get("allocation") or []
                belongs = (
                    owner == worker
                    and all(
                        owners.get(node.get("host_id"), worker) == worker
                        for node in allocation
                    )
                ) or (
                    owner is None
                    and all(
                        owners.get(node.get("host_id")) == worker for node in allocation
                    )
                )
                if record.get("quarantined") and belongs:
                    clear.append(key)
            if clear:
                self.state.update_many(clear, quarantined=False)
            logging.info("automatic quarantine disabled for worker %s", worker)
        # Record interrupted work according to this worker's configured policy.
        for key, r in self.state.read().items():
            if r.get("phase") in {"RESERVED", "STAGING", "RUNNING", "CLEANING"}:
                result = report(
                    r["run"],
                    r["item"],
                    r["digest"],
                    "INFRA_ERROR",
                    125,
                    ["Worker restarted; remote cleanup unverified"],
                    False,
                )
                if self.deployment:
                    path = self.case_folder(key) / "pipeline.log"
                    with path.open("a", encoding="utf8") as f:
                        f.write("INTERRUPTED_CLEANUP_UNVERIFIED\n")
                    self.finalize_report(
                        result, {"host": self.deployment["address"], "path": str(path)}
                    )
                self.state.update(
                    key,
                    phase="PENDING_REPORT",
                    quarantined=bool(r.get("allocation"))
                    and (self.quarantine_enabled or r["item"].get("worker") != worker),
                    report=result,
                )

    @staticmethod
    def _zone_name(instance):
        zone = getattr(instance, "zone", None)
        return zone if isinstance(zone, str) else instance.worker

    def control_view(self):
        from ci_core.idle_probe import group_id

        with self.config_lock:
            pools, revision = self.pools, self.pools_revision
        records = self.state.read()
        probe, probe_time, sampled_pools = (
            self.idle_probe.sample if self.idle_probe else ({}, None, None)
        )
        visibility = self.idle_probe.health if self.idle_probe else {}
        probe_round = self.idle_probe.probe_round if self.idle_probe else 0
        visibility_probe_time = (
            self.idle_probe.visibility_probe_time if self.idle_probe else None
        )
        if sampled_pools is not pools:
            probe, probe_time = {}, None
            visibility_probe_time = None
        blocked = {}
        if self.quarantine_enabled:
            for key, record in records.items():
                if record.get("quarantined"):
                    for resource in resource_keys(record.get("allocation") or []):
                        blocked[resource] = key
        devices = []
        for pool_name, pool in pools.items():
            if pool["owner"] != getattr(self, "zone", self.worker):
                continue
            for node in pool["nodes"]:
                host = node["host_id"]
                for group in node.get("allocation_groups", []):
                    ids = sorted(group["devices"])
                    resources = resource_keys([{"host_id": host, "devices": ids}])
                    state, ts = probe.get(group_id(pool_name, host, ids), (None, None))
                    health = visibility.get(
                        json.dumps([pool_name, host, ids], separators=(",", ":"))
                    )
                    keys = sorted(
                        {
                            blocked[resource]
                            for resource in resources
                            if resource in blocked
                        }
                    )
                    qid = (
                        "t_" + hashlib.sha256(keys[0].encode()).hexdigest()[:32]
                        if keys
                        else None
                    )
                    unavailable = bool(node.get("probe_unavailable"))
                    if unavailable:
                        state = "probe_error"
                        base_health = health or {}
                        health = {
                            "pr": max(int(base_health.get("pr", probe_round)), 1),
                            "v": 2,
                            "mc": max(int(base_health.get("mc", 0)), 1),
                            "lo": base_health.get("lo"),
                        }
                    skipped = state == "leased"
                    entry = {
                        "pool": pool_name,
                        "h": host,
                        "d": ids,
                        "st": None if skipped else state,
                        "ts": ts,
                        "qu": int(bool(keys) or unavailable),
                        "qid": qid,
                    }
                    if skipped:
                        entry["sk"] = 1
                    if health is not None:
                        entry.update(
                            {
                                "pr": health.get("pr", probe_round),
                                "v": health.get("v"),
                                "mc": health.get("mc", 0),
                                "lo": health.get("lo"),
                            }
                        )
                    devices.append(entry)
        return {
            "records": records,
            "sha": self.release_sha or os.environ.get("LITE_ACTIONS_RELEASE_SHA", ""),
            "healthy": self.control_healthy,
            "revision": revision,
            "idle_ts": probe_time,
            "probe_ts": visibility_probe_time,
            "devices": devices,
            "queues": queue_telemetry(
                self.worker, pools, records, self.quarantine_enabled
            ),
        }

    def consume_control(self):
        if not self.control:
            return
        for entry in self.control.commands():
            command = entry["cmd_id"]
            try:
                if entry["cmd_type"] == "SWAP_WORKFLOW":
                    self.control.complete(
                        command,
                        **self.state.swap_workflow(entry["payload"], command),
                    )
                elif entry["cmd_type"] == "SYNC_CONFIG":
                    if not self.sync_pools(entry):
                        break
                else:
                    self.control.complete(command, "FAILED", err="UNSUPPORTED_COMMAND")
            except Exception as exc:
                if entry["cmd_type"] == "SYNC_CONFIG":
                    self.draining = set()
                    self.config_verification = None
                    if getattr(self, "_coordinator_active", False) is True:
                        self.coordinator.clear_drain_state(self.zone)
                logging.warning("control command failed (%s)", type(exc).__name__)
                self.control.complete(
                    command,
                    "FAILED",
                    err=str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                )

    def sync_pools(self, entry):
        from ci_core.control_client import ACTIVE, write_json
        from ci_core.pools_sync import affected_resources, removed_groups, verify

        payload = entry["payload"]
        target = payload["revision"]
        cmd = entry["cmd_id"]
        path = self.state.path.parent / "pools-active.json"
        if type(target) is not int or target < self.pools_revision:
            raise ValueError("STALE_POOL_REVISION")
        if target == self.pools_revision:
            import json

            saved = json.loads(path.read_text())
            if saved.get("source_sha256", saved["sha256"]) != payload["sha256"]:
                raise ValueError("POOL_REVISION_MISMATCH")
            cancelled = saved.get("drain_pending", [])
            unavailable = saved.get("unavailable_nodes", [])
        else:
            if not self.config_verification:
                if not self.config_executor:
                    self.config_executor = ThreadPoolExecutor(max_workers=1)
                self.config_verification = self.config_executor.submit(
                    verify,
                    self.zone,
                    self.workers,
                    self.pools,
                    entry["config"],
                    payload,
                )
                self.config_verification.add_done_callback(
                    lambda _: self.control.wake.set()
                )
                return False
            if not self.config_verification.done():
                return False
            doc = self.config_verification.result()
            next_pools = doc["pools"]
            removed = removed_groups(self.zone, self.pools, next_pools)
            self.draining = affected_resources(removed)
            if getattr(self, "_coordinator_active", False) is True and self.draining:
                self.coordinator.set_drain_state(self.zone, self.draining)
            records = self.state.read()
            for record in records.values():
                resources = set(resource_keys(record.get("allocation") or []))
                if resources.intersection(self.draining) and record.get("quarantined"):
                    raise ValueError("QUARANTINED_RESOURCE_CANNOT_DELETE")
            for record in records.values():
                resources = set(resource_keys(record.get("allocation") or []))
                if resources.intersection(self.draining) and record.get(
                    "phase"
                ) in ACTIVE - {"QUEUED"}:
                    return False
            if getattr(self, "_coordinator_active", False) is True and self.draining:
                try:
                    self.coordinator.check_delete_allowed(self.draining)
                except ValueError as exc:
                    if str(exc) == "ACTIVE_LEASES_REMAINING":
                        return False
                    raise

            cancelled = []
            impacted = {name for name, _, _ in removed}
            for key, record in ordered_queued(records):
                item = record["item"]
                if item["target_pool"] not in impacted or item["kind"] in {
                    "worker_probe",
                    *LIFECYCLE_KINDS,
                }:
                    continue
                try:
                    old_case = runtime_case(item, self.pools, self.worker)
                    eligible_topologies(old_case, self.pools, include_paused=True)
                except ValueError:
                    continue
                try:
                    new_case = runtime_case(item, next_pools, self.worker)
                    eligible_topologies(new_case, next_pools, include_paused=True)
                except ValueError:
                    cancelled.append(key)
            for key in payload.get("drain_cancel_keys") or []:
                if key in cancelled:
                    continue
                record = records.get(key)
                if not record or record.get("phase") != "QUEUED":
                    continue
                item = record["item"]
                if item.get("kind") in {"worker_probe", *LIFECYCLE_KINDS}:
                    continue
                try:
                    new_case = runtime_case(item, next_pools, self.worker)
                    eligible_topologies(new_case, next_pools, include_paused=False)
                except ValueError:
                    cancelled.append(key)
            doc.update(applied_at=int(time.time()), drain_pending=cancelled)
            unavailable = doc.get("unavailable_nodes", [])
            write_json(path, doc)
            with self.config_lock:
                self.pools, self.pools_revision = next_pools, target
            if getattr(self, "_supervisor_active", False) is True:
                self.supervisor.pools = next_pools
                self.supervisor.pools_revision = target
            self.config_verification = None
            self.draining = set()
            if getattr(self, "_coordinator_active", False) is True:
                self.coordinator.clear_drain_state(self.zone)
        current = self.state.read()
        cancel_reason = (
            "Resource draining by cluster configuration"
            if payload.get("drain_action") == "DRAIN"
            else "Resource removed by cluster configuration"
        )
        for key in cancelled:
            if current.get(key, {}).get("phase") == "QUEUED":
                self.finish(key, "CANCELLED", 130, [cancel_reason])
        if unavailable:
            summary = "; ".join(
                f"{entry['host_id']}: {entry['reason']}" for entry in unavailable
            )[:200]
            self.control.complete(cmd, "PARTIAL_APPLIED", rev=target, err=summary)
        else:
            self.control.complete(cmd, "APPLIED", rev=target)
        return True

    def client_for(self, run):
        if getattr(self, "_broker_active", False) is True:
            return self.broker
        repo = run.get("_ci_repo", REPO)
        if hasattr(self.client, "for_run"):
            return self.client.for_run(run)
        if hasattr(self.client, "for_repo"):
            return self.client.for_repo(repo)
        if getattr(self.client, "repo", repo) != repo:
            raise ValueError("run belongs to another GitHub repository")
        return self.client

    def eligible(self, run):
        if not str(run.get("name", "")).startswith("lite-ci-") or run.get(
            "status"
        ) not in {"queued", "in_progress"}:
            return False
        if _is_pr_smoke(run):
            event = run.get("_ci_platform_event", run.get("event"))
            if event in {
                "workflow_dispatch",
                "pull_request_target",
                "issue_comment",
                "pull_request_review",
            }:
                return (
                    run.get("_ci_platform_head_branch", run.get("head_branch"))
                    == "main"
                )
            if event == "pull_request":
                return (run.get("head_repository") or {}).get("full_name") == SELF_REPO
            return False
        if _is_self_smoke(run):
            if getattr(self, "is_test", False) is True:
                return False
            event = run.get("_ci_platform_event", run.get("event"))
            branch = run.get("_ci_platform_head_branch", run.get("head_branch"))
            if event not in {"push", "workflow_dispatch"} or branch != "main":
                return False
            if getattr(self, "client", None) is not None:
                _verify_self_smoke_run(self.client_for(run), run)
            return True
        return True

    def transition(self, key, phase, **fields):
        self.state.update(key, phase=phase, **fields)
        r = self.state.read()[key]
        if Router._zone_name(self) == "yellow":
            if phase == "QUEUED":
                self.maybe_ack(key, r)
            return
        if r.get("last_sent_phase") == phase:
            return
        try:
            if getattr(self, "_broker_active", False) is True:
                ident = self.broker.post_progress(
                    r["run"],
                    r["item"],
                    r["digest"],
                    phase,
                    r.get("progress_id"),
                )
            else:
                ident = progress(
                    self.client_for(r["run"]),
                    r["run"],
                    r["item"],
                    r["digest"],
                    phase,
                    r.get("progress_id"),
                )
            self.state.update(key, progress_id=ident, last_sent_phase=phase)
        except Exception:
            logging.warning("progress pending for %s", key)

    def maybe_ack(self, key, r):
        """Persist intent before POST. A retry confirms uncertain delivery before writing."""
        if (
            r.get("progress_id")
            or r.get("ack_blocked")
            or r.get("ack_retry_at", 0) > time.time()
        ):
            return
        n = r.get("ack_attempts", 0)
        if n >= 8:
            return
        run = r["run"]
        item = r["item"]
        author = (
            os.environ.get("LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author)
            if run.get("_ci_provider") == "gitcode"
            else self.author
        )
        try:
            self.state.update(
                key, ack_attempts=n + 1, ack_retry_at=time.time() + min(900, 30 * 2**n)
            )
            if getattr(self, "_broker_active", False) is True:
                ident = self.broker.post_progress(
                    run,
                    item,
                    r["digest"],
                    "QUEUED",
                    r.get("progress_id"),
                    retry=bool(n),
                )
                self.state.update(key, progress_id=ident, last_sent_phase="QUEUED")
                return
            client = self.client_for(run)
            if n:
                target = progress_body(run, item, r["digest"], "QUEUED")
                matches = [
                    c
                    for c in client.commit_comments(
                        result_sha(run), category="ack_retry_lookup"
                    )
                    if c.get("user", {}).get("login") == author
                    and c.get("body") == target
                ]
                if len(matches) > 1:
                    raise ValueError("DUPLICATE_ACK")
                if matches:
                    self.state.update(
                        key, progress_id=matches[0]["id"], last_sent_phase="QUEUED"
                    )
                    return
            ident = progress(client, run, item, r["digest"], "QUEUED", retry=bool(n))
            self.state.update(key, progress_id=ident, last_sent_phase="QUEUED")
        except Exception as exc:
            from email.utils import parsedate_to_datetime
            from urllib.error import HTTPError

            from ci_core.egress_policy import Deferred

            if isinstance(exc, Deferred):
                self.state.update(key, ack_attempts=n)
            elif isinstance(exc, ValueError):
                self.state.update(key, ack_blocked=True)
            elif isinstance(exc, HTTPError) and exc.headers.get("Retry-After"):
                header = exc.headers["Retry-After"]
                try:
                    until = (
                        time.time() + int(header)
                        if header.isdigit()
                        else parsedate_to_datetime(header).timestamp()
                    )
                    self.state.update(
                        key,
                        ack_retry_at=max(self.state.read()[key]["ack_retry_at"], until),
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
            logging.warning("ACK not confirmed for %s (%s)", key, type(exc).__name__)

    def receive(self, runs):
        for run in sorted(runs, key=lambda r: (r.get("created_at", ""), str(r["id"]))):
            try:
                self._receive_one(run)
            except Exception as exc:
                logging.warning(
                    "request unavailable: %s/%s (%s)",
                    run.get("_ci_repo", REPO),
                    run.get("id"),
                    type(exc).__name__,
                )

    def probe_rule(self, run):
        if (
            getattr(self, "is_test", False)
            or not self.deployment
            or run.get("_ci_provider", "github") != "github"
        ):
            return None
        rules = [
            r
            for r in self.deployment.get("probe_callers", [])
            if r.get("repo") == run.get("_ci_repo")
            and r.get("workflow") == run.get("path")
        ]
        if len(rules) > 1:
            raise ValueError("AMBIGUOUS_INFRA_CALLER")
        return rules[0] if rules else None

    def receive_probe(self, run, rule):
        from ci_core.contract import digest
        from ci_core.worker_probe import fixed_item, github_call

        proof = github_call(self.client_for(run), run, rule)
        run = {**run, "_ci_infra": True, "_ci_dispatch_id": f"os-{proof['job_id']}"}
        item = fixed_item(self.worker)
        key = task_key(run, item)
        record = dict(
            phase="QUEUED",
            repo=proof["repo"],
            run=run,
            item=item,
            digest=digest(proof),
            defaults={},
            queued_at=time.time(),
            use_instance_id=True,
        )
        for admitted in self.state.admit([(key, record)]):
            self.transition(admitted, "QUEUED")

    def _admit_plan(self, run, plan, *, generic_source=None):
        owned = [c for c in plan["cases"] if c["worker"] == self.worker]
        if not owned or all(task_key(run, c) in self.state.read() for c in owned):
            return
        if generic_source is not None:
            from ci_core.github_source import read_source

            source = read_source(self.client_for(run), run, plan, source=generic_source)
            if source is None:
                return
            run = {
                **run,
                "_ci_source": source,
                "_ci_execution_sha": source["source_sha"],
            }
        counts = Counter(item["test_id"] for item in plan["cases"])
        entries = []
        for item in owned:
            key = task_key(run, item)
            if self.deployment is not None:
                # Refuse permanently undeployable envelopes before allocating hardware.
                minimal = report(
                    run, item, plan["plan_digest"], "RESOURCE_TIMEOUT", 125, [], False
                )
                self.finalize_report(
                    minimal,
                    {
                        "host": self.deployment["address"],
                        "path": str(self.case_folder(key) / "pipeline.log"),
                    },
                )
                body_for(run, item, minimal, local_worker=self.worker)
            entries.append(
                (
                    key,
                    dict(
                        phase="QUEUED",
                        repo=run.get("_ci_repo", REPO),
                        run=run,
                        item=item,
                        digest=plan["plan_digest"],
                        defaults=plan["defaults"],
                        queued_at=time.time(),
                        use_instance_id=counts[item["test_id"]] > 1,
                    ),
                )
            )
        if (
            run.get("_ci_provider") == "gitcode"
            and run.get("_ci_pr_number")
            and run.get("_ci_execution_sha") != run["head_sha"]
        ):
            # The PR may have moved during source inspection or since a cache hit.
            self.client_for(run).validate_current(run)
        for key in self.state.admit(entries):
            self.transition(key, "QUEUED")

    def _receive_one(self, run):
        rule = self.probe_rule(run)
        if rule is not None and self.eligible(run):
            try:
                self.receive_probe(run, rule)
            except Exception as exc:
                logging.warning(
                    "fixed probe unavailable: %s (%s)", run["id"], type(exc).__name__
                )
            if rule.get("mode") == "cpu-only":
                return
        identity = (
            run.get("_ci_provider", "github"),
            run.get("_ci_repo", REPO),
            run["id"],
            run.get("run_attempt", 1),
            run["head_sha"],
            run.get("_ci_execution_sha"),
            run.get("_ci_dispatch_id"),
        )
        cached = self.plans.get(identity)
        primary_pending = True
        if cached is not None:
            owned = [c for c in cached["cases"] if c["worker"] == self.worker]
            if not owned or all(task_key(run, c) in self.state.read() for c in owned):
                primary_pending = False

        if primary_pending:
            if getattr(self, "_broker_active", False) is True:
                if not self.eligible(run):
                    return
                fetched = self.broker.fetch_plan(run)
                if fetched is None:
                    return
                run, plan = fetched
                parse_plan(
                    plan,
                    run,
                    run["path"].rsplit("/", 1)[-1],
                    repo=run.get("_ci_repo", REPO),
                )
                if _is_self_smoke(run):
                    _verify_self_smoke_run(self.client_for(run), run, plan=plan)
                self.plans[identity] = plan
                self._admit_plan(run, plan)
            else:
                if not self.eligible(run):
                    return
                generic_source = None
                if (
                    run.get("_ci_provider") != "gitcode"
                    and not _is_self_smoke(run)
                    and not _is_pr_smoke(run)
                ):
                    from ci_core.github_source import (
                        bind_request_source,
                        read_source_artifact,
                    )

                    client = self.client_for(run)
                    generic_source = read_source_artifact(client, run)
                    if generic_source is None:
                        return
                    run = bind_request_source(client, run, generic_source)
                if cached is None:
                    plan = self.client_for(run).ci_request(run)
                    if plan is None:
                        return
                    parse_plan(
                        plan,
                        run,
                        run["path"].rsplit("/", 1)[-1],
                        repo=run.get("_ci_repo", REPO),
                    )
                    if _is_self_smoke(run):
                        _verify_self_smoke_run(self.client_for(run), run, plan=plan)
                    self.plans[identity] = plan
                else:
                    plan = cached
                self._admit_plan(run, plan, generic_source=generic_source)

        if _is_pr_smoke(run) and getattr(self, "is_test", False) is not True:
            if not self.eligible(run):
                return
            cleanup_identity = (identity, "cleanup")
            cleanup_plan = self.plans.get(cleanup_identity)
            if cleanup_plan is None:
                cleanup_plan = self.client_for(run).ci_cleanup_request(run)
                if cleanup_plan is None:
                    return
                parse_plan(
                    cleanup_plan,
                    run,
                    run["path"].rsplit("/", 1)[-1],
                    repo=run.get("_ci_repo", REPO),
                )
                if any(
                    c.get("kind") != "test_worker_cleanup"
                    for c in cleanup_plan["cases"]
                ):
                    raise ValueError("cleanup artifact must contain only cleanup cases")
                self.plans[cleanup_identity] = cleanup_plan
            self._admit_plan({**run, "_ci_plan_stage": "cleanup"}, cleanup_plan)

    def claim(self, case, *, key=None):
        all_options = eligible_topologies(case, self.pools, include_paused=True)
        allowed = candidates(case, self.pools)
        active = {resource_keys(option) for option in allowed}
        options = [
            option
            for option in all_options
            if resource_keys(option) in active
            and not self.draining.intersection(resource_keys(option))
        ]
        blocked = set()
        for r in self.state.read().values():
            if self.quarantine_enabled and r.get("quarantined"):
                blocked.update(resource_keys(r.get("allocation", [])))
        candidate_wait_resources = set()
        for allocation in options:
            if any(
                resource_keys(x["allocation"]) == resource_keys(allocation)
                and x["until"] > time.time()
                for x in case.get("_temporarily_unavailable", [])
            ):
                continue
            if blocked.intersection(resource_keys(allocation)):
                continue
            candidate_wait_resources.update(resource_keys(allocation))
            if getattr(self, "_coordinator_active", False) is True:
                try:
                    lease = self.coordinator.acquire(
                        allocation,
                        owner_worker=self.worker,
                        owner_zone=self.zone,
                        pool=case["pool"],
                        is_production=getattr(self, "is_test", False) is not True,
                        task_key=key or "",
                        max_jobs_per_host=self.pools[case["pool"]].get(
                            "max_jobs_per_host", 1
                        ),
                        docker=case.get("docker"),
                        wait_key=key,
                        enforce_quarantine=self.quarantine_enabled,
                    )
                    return lease, {
                        **case,
                        "allocation": allocation,
                        "host_budget_slots": lease.host_budget_slots,
                        "lease_id": lease.lease_id,
                        **lease.ports,
                    }
                except ResourceBusy:
                    continue
            stack = ExitStack()
            try:
                root = shared_resource_lock_root()
                stack.enter_context(reserve(root / "devices", allocation))
                budget = stack.enter_context(
                    reserve_host_budget(
                        root / "host-budget",
                        allocation,
                        max_jobs_per_host=self.pools[case["pool"]].get(
                            "max_jobs_per_host", 1
                        ),
                    )
                )
                ports = stack.enter_context(reserve_ports(root / "ports", allocation))
                return stack, {
                    **case,
                    "allocation": allocation,
                    "host_budget_slots": budget,
                    **ports,
                }
            except ResourceBusy:
                stack.close()
            except BaseException:
                stack.close()
                raise
        if (
            getattr(self, "_coordinator_active", False) is True
            and getattr(self, "is_test", False) is not True
            and key
            and candidate_wait_resources
        ):
            self.coordinator.register_production_waiter(
                key,
                owner_worker=self.worker,
                pool=case["pool"],
                resources=candidate_wait_resources,
            )
        return None

    def case_folder(self, key):
        import hashlib

        folder = (
            Path(self.deployment["state_dir"])
            / "logs"
            / "runs"
            / hashlib.sha256(key.encode()).hexdigest()
        )
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return folder

    def finalize_report(self, result, locator):
        from ci_core.contract import digest

        result["log_locator"] = locator
        result["finished_at"] = int(time.time())
        if "native_source" in result:
            result["source_digest"] = digest(result.pop("native_source"))

    def finish(
        self,
        key,
        status,
        rc,
        lines,
        clean=True,
        *,
        locator=None,
        diagnostic=None,
        allocation=None,
        published_by_broker=False,
    ):
        if getattr(self, "_coordinator_active", False) is True:
            self.coordinator.clear_production_waiter(key)
        r = self.state.read()[key]
        final_allocation = allocation if allocation is not None else r.get("allocation")
        if published_by_broker:
            result = report(r["run"], r["item"], r["digest"], status, rc, lines, clean)
            if locator is not None:
                self.finalize_report(result, locator)
            self.state.update(
                key,
                phase="REPORTED",
                published_at=time.time(),
                allocation=final_allocation,
                quarantined=bool(final_allocation)
                and not clean
                and (self.quarantine_enabled or r["item"].get("worker") != self.worker),
                report=result,
            )
            return
        if status == "PASS" and r["run"].get("_ci_source"):
            from ci_core.github_source import check_current

            try:
                check_current(
                    self.client_for(r["run"]), r["run"]["_ci_source"], check_run=True
                )
            except ValueError:
                status, rc, lines = (
                    "CANCELLED",
                    130,
                    ["PR source or run changed before publication"],
                )
            except Exception:
                status, rc, lines = (
                    "INFRA_ERROR",
                    125,
                    ["PR source authority unavailable before publication"],
                )
        if status == "PASS" and r["run"].get("_ci_provider") == "gitcode":
            try:
                self.client_for(r["run"]).validate_current(r["run"])
            except RunChanged:
                status, rc, lines = (
                    "CANCELLED",
                    130,
                    ["GitCode run ended or changed before result publication"],
                )
            except Exception:
                status, rc, lines = (
                    "INFRA_ERROR",
                    125,
                    ["GitCode run identity could not be verified"],
                )
        if status == "PASS" and r["run"].get("_ci_infra") is True:
            from ci_core.contract import digest
            from ci_core.worker_probe import github_call

            try:
                if (
                    digest(
                        github_call(
                            self.client_for(r["run"]),
                            r["run"],
                            self.probe_rule(r["run"]),
                        )
                    )
                    != r["digest"]
                ):
                    raise ValueError
            except Exception:
                status, rc, lines = "INFRA_ERROR", 125, ["INFRA_PUBLICATION_UNVERIFIED"]
        result = report(r["run"], r["item"], r["digest"], status, rc, lines, clean)
        if self.deployment is not None:
            if locator is None:
                path = self.case_folder(key) / "pipeline.log"
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"{status} rc={rc}\n")
                locator = {"host": self.deployment["address"], "path": str(path)}
            self.finalize_report(result, locator)
            if getattr(self, "zone", self.worker) == "blue" and diagnostic is not None:
                data, truncated = diagnostic
                result["last_20_lines"] = []
                if len(data) <= 2048 and not truncated:
                    result["diagnostics"] = data.decode().splitlines()
                else:
                    result["attachment_status"] = "unavailable"
                    if self.release_logs:
                        try:
                            result["attachment_ref"] = self.release_logs.deliver(
                                key, data, truncated
                            )
                            result["attachment_status"] = "available"
                        except Exception as exc:
                            logging.warning(
                                "Blue log delivery unavailable (%s)", type(exc).__name__
                            )

        extra_fields = (
            {"allocation": final_allocation} if allocation is not None else {}
        )
        self.state.update(
            key,
            phase="PENDING_REPORT",
            quarantined=bool(final_allocation)
            and not clean
            and (self.quarantine_enabled or r["item"].get("worker") != self.worker),
            report=result,
            **extra_fields,
        )

    def work(self, key, case, lease, cancel, preflight):
        r = self.state.read()[key]
        item = r["item"]
        run = r["run"]
        launched = False
        try:
            if getattr(self, "_broker_active", False) is True:
                self.transition(key, "STAGING")
                if cancel.is_set():
                    self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
                    return

                def broker_phase(phase):
                    nonlocal launched
                    if phase == "LAUNCHING":
                        launched = True
                        preflight.set()
                    else:
                        self.transition(key, phase)

                outcome = self.broker.request_run(
                    key=key,
                    run=run,
                    item=item,
                    digest=r["digest"],
                    defaults=r["defaults"],
                    use_instance_id=bool(
                        r.get("use_instance_id") or run.get("_ci_dispatch_id")
                    ),
                    cancel_event=cancel,
                    on_phase=broker_phase,
                )
                if outcome.get("retry_busy"):
                    return {
                        "allocation": outcome.get("allocation") or [],
                        "until": time.time() + float(outcome.get("retry_after", 15)),
                    }
                if outcome.get("run_changed"):
                    if outcome.get("allocation") is not None:
                        self.state.update(key, allocation=outcome["allocation"])
                    raise RunChanged()
                if outcome.get("allocation") is not None:
                    self.state.update(key, allocation=outcome["allocation"])
                self.transition(key, "CLEANING")
                self.finish(
                    key,
                    outcome["status"],
                    outcome["exit_code"],
                    outcome.get("tail") or [],
                    outcome.get("release_verified", False),
                    locator=outcome.get("locator"),
                    allocation=outcome.get("allocation"),
                    published_by_broker=bool(outcome.get("published_by_broker")),
                )
                return
            if _is_self_smoke(run):
                approved = _verify_self_smoke_run(
                    self.client_for(run),
                    run,
                    expected_digest=r["digest"],
                )
                if item not in approved["cases"]:
                    raise ValueError("self-smoke case identity mismatch")
            if run.get("_ci_provider") == "gitcode":
                self.client_for(run).validate_current(run)
            if run.get("_ci_source"):
                from ci_core.github_source import check_current

                check_current(self.client_for(run), run["_ci_source"], check_run=True)
            self.transition(key, "STAGING")
            if cancel.is_set():
                self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
                return
            if item["kind"] == "test_worker_deploy":
                self.transition(key, "RUNNING")
                outcome = self.supervisor.handle_deploy(self.client_for(run), run, item)
                self.finish(
                    key,
                    outcome["status"],
                    outcome["exit_code"],
                    outcome.get("tail") or [],
                    outcome.get("release_verified", True),
                )
                return
            if item["kind"] == "test_worker_cleanup":
                self.transition(key, "RUNNING")
                outcome = self.supervisor.handle_cleanup(
                    self.client_for(run), run, item
                )
                self.finish(
                    key,
                    outcome["status"],
                    outcome["exit_code"],
                    outcome.get("tail") or [],
                    outcome.get("release_verified", True),
                )
                return
            if item["kind"] == "worker_probe":
                # Recheck persisted tasks: a pre-upgrade queue is not an authorization.
                if run.get("_ci_infra") is True:
                    from ci_core.contract import digest
                    from ci_core.worker_probe import fixed_item, github_call

                    rule = self.probe_rule(run)
                    if rule is None or item != fixed_item(self.worker):
                        raise ValueError("INFRA_CALLER_NOT_APPROVED")
                    proof = github_call(self.client_for(run), run, rule)
                    if (
                        digest(proof) != r["digest"]
                        or run.get("_ci_dispatch_id") != f"os-{proof['job_id']}"
                    ):
                        raise ValueError("INFRA_STALE_PROBE")
                elif _is_self_smoke(run):
                    from ci_core.self_smoke import expected_plan

                    approved = expected_plan(run)
                    if (
                        r["digest"] != approved["plan_digest"]
                        or item not in approved["cases"]
                    ):
                        raise ValueError("self-smoke probe identity mismatch")
                else:
                    if (
                        "env_vars" in item
                        or item.get("target_pool") is not None
                        or item.get("docker") is not None
                        or item.get("resources") != {"nodes": 0, "devices_per_node": 0}
                    ):
                        raise ValueError("INVALID_WORKER_PROBE_ITEM")
                self.transition(key, "RUNNING")
                p = probe_worker()
                locator = None
                if self.deployment is not None:
                    path = self.case_folder(key) / "probe.log"
                    path.write_text(p.stdout + p.stderr, encoding="utf-8")
                    locator = {"host": self.deployment["address"], "path": str(path)}
                if cancel.is_set():
                    self.finish(
                        key, "CANCELLED", 130, ["Cancelled during worker probe"]
                    )
                    return
                expected = self.workers[getattr(self, "zone", self.worker)].get(
                    "expected_hostname"
                )
                if (
                    expected
                    and f"WORKER_HOSTNAME={expected}" not in p.stdout.splitlines()
                ):
                    raise ValueError("worker identity mismatch")
                self.finish(
                    key,
                    "PASS" if p.returncode == 0 else "INFRA_ERROR",
                    p.returncode,
                    (p.stdout + p.stderr).splitlines(),
                    locator=locator,
                )
                return
            if "env_vars" in item:
                raw_env = item["env_vars"]
                if run.get("_ci_provider") == "gitcode":
                    self.client_for(run).validate_current(run)
            else:
                with tempfile.TemporaryDirectory(prefix="lite-definition-") as tmp:
                    source_repo = run.get("_ci_repo", REPO)
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
                    definition = verify_definition(
                        archive, item, r["defaults"], repo=source_repo
                    )
                if run.get("_ci_provider") == "gitcode":
                    self.client_for(run).validate_current(run)
                raw_env = definition["env_vars"]
            from ci_core.registry import validate_case_environment

            # The model selects values; only the deployment may authorize
            # ASCEND_SET_ENV_PATH and any explicitly constrained keys.
            rules = (
                self.workers[getattr(self, "zone", self.worker)]
                .get("case_environments", {})
                .get(case["docker"], {})
            )
            raw_env = dict(raw_env)
            keys = (
                ("ASCEND_SET_ENV_PATH", "HF_ASSETS_PATH")
                if item["kind"] == "train"
                else ("ASCEND_SET_ENV_PATH",)
            )
            for name in keys:
                allowed = rules.get(name)
                if name not in raw_env and isinstance(allowed, list) and allowed:
                    raw_env[name] = allowed[0]
            environment = validate_case_environment(raw_env, kind=item["kind"])
            approved_setup = rules.get("ASCEND_SET_ENV_PATH")
            if type(approved_setup) is not list or (
                environment.get("ASCEND_SET_ENV_PATH") not in approved_setup
            ):
                raise ValueError("MODEL_ENV_NOT_APPROVED")
            if any(
                k in rules and (type(rules[k]) is not list or v not in rules[k])
                for k, v in environment.items()
            ):
                raise ValueError("MODEL_ENV_NOT_APPROVED")
            case = {**case, "verified_env": environment}
            if run.get("_ci_source"):
                case = {**case, "native_source": run["_ci_source"]}
            subrun = {
                **run,
                "_ci_test_id": item["test_id"],
                "_ci_cancel_event": cancel,
            }
            if r.get("use_instance_id") or run.get("_ci_dispatch_id"):
                subrun["_ci_case_id"] = item["case_id"]

            def on_phase(phase):
                nonlocal launched
                if phase == "LAUNCHING":
                    launched = True
                    preflight.set()
                else:
                    self.transition(key, phase)

            subrun["_ci_phase_callback"] = on_phase
            subrun["_ci_runtime_callback"] = lambda runtime: self.state.update(
                key, runtime=runtime
            )
            if self.deployment is not None:
                subrun["_ci_pipeline_folder"] = self.case_folder(key)
                subrun["_ci_zone"] = getattr(self, "zone", self.worker)
                subrun["_ci_worker_address"] = self.deployment["address"]
            if lease is not None:
                subrun["_ci_lease"] = lease
            execution = execute(case, subrun, root=self.root)
            self.transition(key, "CLEANING")
            from ci_core.reporting import case_passed

            valid = case_passed(execution.log, item["test_id"])
            status = (
                "CANCELLED"
                if cancel.is_set() or execution.exit_code == 130
                else "TIMED_OUT"
                if execution.exit_code == 124
                else "INFRA_ERROR"
                if not execution.release_verified
                else "PASS"
                if execution.exit_code == 0 and valid
                else "FAIL"
            )
            if (
                not execution.release_verified
                and getattr(self, "_coordinator_active", False) is True
                and lease is not None
                and self.quarantine_enabled
                and hasattr(lease, "lease_id")
            ):
                self.coordinator.quarantine_lease(
                    lease.lease_id,
                    owner_worker=self.worker,
                    task_key=key,
                    reason="cleanup_unverified",
                )
            self.finish(
                key,
                status,
                130 if status == "CANCELLED" else execution.exit_code,
                execution.tail,
                execution.release_verified,
                locator=execution.locator,
                diagnostic=execution.diagnostic,
            )
        except ExternalNpuOccupancy:
            if launched:
                self.finish(key, "INFRA_ERROR", 125, ["Occupancy after launch"], False)
            else:
                # The pump requeues only after the lease is released.
                return {"allocation": case["allocation"], "until": time.time() + 60}
        except subprocess.TimeoutExpired:
            self.finish(
                key, "TIMED_OUT", 124, ["Worker operation timed out"], not launched
            )
        except RunChanged:
            self.finish(
                key, "CANCELLED", 130, ["GitCode run or PR changed"], not launched
            )
        except Exception as exc:
            logging.exception("case failure %s", key)
            self.finish(
                key,
                "INFRA_ERROR",
                125,
                [f"Infrastructure error: {type(exc).__name__}"],
                not launched,
            )
        finally:
            if lease:
                lease.close()

    def _run_after_handoff(self, handoff, *args):
        # SIGTERM can interrupt set_result after completion but before it wakes
        # waiters. A bounded wait rechecks the completed handoff in that case.
        while True:
            try:
                accepted = handoff.result(timeout=1)
                break
            except FutureTimeout:
                continue
        if accepted:
            return self.work(*args)

    def _complete_active(self):
        for key, (future, event, runid) in list(self.active.items()):
            if future.done():
                retry = future.result()
                if retry:
                    if event.is_set():
                        self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
                    else:
                        current = self.state.read()[key]
                        self.state.update(
                            key,
                            phase="QUEUED",
                            allocation=[],
                            quarantined=False,
                            unavailable=[
                                row
                                for row in current.get("unavailable", [])
                                if row["until"] > time.time()
                            ]
                            + [retry],
                        )
                del self.active[key]
                self.preflight.pop(key, None)

    def pump(self):
        self._complete_active()
        records = self.state.read()
        blocked_pools = set()
        # Before launch, a leased Smoke can still return to the head of its pool.
        for key in self.active:
            item = records[key]["item"]
            if (
                item["kind"] == "npu_smoke"
                and item["target_pool"]
                and not self.preflight[key].is_set()
            ):
                blocked_pools.add((item["target_pool"], True))
        queued = ordered_queued(records)
        for key, r in queued:
            item = r["item"]
            pool = item["target_pool"]
            is_smoke = item["kind"] == "npu_smoke"
            queue = (pool, is_smoke)
            ident = run_key(r["run"])
            if ident in self.unavailable_runs:
                if pool and is_smoke:
                    blocked_pools.add(queue)
                continue
            if key in self.active:
                continue
            if queue in blocked_pools:
                continue
            # Smoke waits for capacity while its originating run remains active.
            timeout = queue_timeout(item["kind"])
            if timeout is not None and time.time() - r["queued_at"] > timeout:
                self.finish(
                    key,
                    "RESOURCE_TIMEOUT",
                    124,
                    [f"Resource wait exceeded {timeout} seconds"],
                )
                continue
            light = item["kind"] in {"worker_probe", *LIFECYCLE_KINDS}
            count = sum(
                (records[k]["item"]["kind"] in {"worker_probe", *LIFECYCLE_KINDS})
                == light
                for k in self.active
            )
            if count >= (1 if light else 4):
                if pool:
                    blocked_pools.add(queue)
                continue
            # submit() can enqueue work before raising; do not run it until the
            # scheduler has recorded ownership of the full lease.
            lease, handoff = None, Future()
            try:
                case = None
                if not light:
                    case = runtime_case(item, self.pools, self.worker)
                    case["_temporarily_unavailable"] = [
                        x for x in r.get("unavailable", []) if x["until"] > time.time()
                    ]
                    if getattr(self, "_broker_active", False) is not True:
                        claim = self.claim(case, key=key)
                        if claim is None:
                            blocked_pools.add(queue)
                            continue
                        lease, case = claim
                        case["pools_snapshot"] = {pool: deepcopy(self.pools[pool])}
                    else:
                        case["allocation"] = []
                        case["pools_snapshot"] = {pool: deepcopy(self.pools[pool])}
                self.state.update(
                    key,
                    phase="RESERVED",
                    allocation=case["allocation"] if case else [],
                    runtime=case,
                )
                event, preflight = threading.Event(), threading.Event()
                future = (self.light if light else self.training).submit(
                    self._run_after_handoff, handoff, key, case, lease, event, preflight
                )
                self.active[key] = (future, event, r["run"]["id"])
                self.preflight[key] = preflight
                handoff.set_result(True)
                if is_smoke:
                    blocked_pools.add(queue)
            except BaseException as exc:
                if handoff.done():
                    raise
                self.active.pop(key, None)
                self.preflight.pop(key, None)
                handoff.set_result(False)
                clean = True
                if lease:
                    try:
                        lease.close()
                    except Exception:
                        clean = False
                        logging.exception("lease release failed after submission error")
                if not isinstance(exc, Exception):
                    if clean:
                        self.state.update(
                            key, phase="QUEUED", allocation=[], quarantined=False
                        )
                    raise
                self.finish(
                    key,
                    "INFRA_ERROR",
                    125,
                    [
                        f"Capacity check failed: {exc if isinstance(exc, PoolDefaultError) else type(exc).__name__}"
                    ],
                    clean,
                )

    def flush(self):
        import random
        from email.utils import parsedate_to_datetime
        from urllib.error import HTTPError

        from ci_core.egress_policy import Deferred

        cache = {}
        for key, r in self.state.read().items():
            if (
                r.get("phase") not in {"PENDING_REPORT", "VERIFY_REPORT"}
                or r.get("next_retry_at", 0) > time.time()
            ):
                continue
            try:
                if getattr(self, "_broker_active", False) is True:
                    self.broker.post_terminal_report(
                        r["run"],
                        r["item"],
                        r["digest"],
                        r["report"],
                        verify_only=r.get("phase") == "VERIFY_REPORT",
                        retry=bool(r.get("send_attempts")),
                    )
                else:
                    post(
                        self.client_for(r["run"]),
                        r["run"],
                        r["item"],
                        r["report"],
                        os.environ.get(
                            "LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author
                        )
                        if r["run"].get("_ci_provider") == "gitcode"
                        else self.author,
                        local_worker=self.worker,
                        cache=cache,
                        verify_only=r.get("phase") == "VERIFY_REPORT",
                        retry=bool(r.get("send_attempts")),
                    )
                self.state.update(key, phase="REPORTED", published_at=time.time())
            except Deferred:
                self.state.update(key, next_retry_at=time.time() + 30)
            except Exception as exc:
                n = r.get("send_attempts", 0) + 1
                permanent = isinstance(exc, ValueError)
                delay = random.uniform(0.85, 1) * min(900, 30 * 2 ** min(n - 1, 5))
                if isinstance(exc, HTTPError):
                    header = exc.headers.get("Retry-After", "")
                    permanent = exc.code in (400, 401, 404, 422)
                    if header.isdigit():
                        delay = max(delay, int(header))
                    elif header:
                        try:
                            delay = max(
                                delay,
                                parsedate_to_datetime(header).timestamp() - time.time(),
                            )
                        except (ValueError, TypeError, OverflowError):
                            pass
                self.state.update(
                    key,
                    phase="REPORT_BLOCKED"
                    if permanent or r.get("phase") == "VERIFY_REPORT"
                    else ("VERIFY_REPORT" if n >= 8 else "PENDING_REPORT"),
                    send_attempts=n,
                    next_retry_at=time.time() + delay,
                )
                logging.warning("result remains pending: %s", key)

    def tick(self):
        if self.release_logs and time.monotonic() >= self.next_expiry:
            self.next_expiry = time.monotonic() + 3600
            try:
                self.release_logs.expire()
            except Exception as exc:
                logging.warning("Blue log retention deferred (%s)", type(exc).__name__)
        # Outbox survives completed GitHub Runs; never depend on an active-run listing.
        self.flush()
        statuses = {}
        self.unavailable_runs = set()
        for key, r in self.state.read().items():
            if r.get("phase") not in {
                "QUEUED",
                "RESERVED",
                "STAGING",
                "RUNNING",
                "CLEANING",
            }:
                continue
            run = r["run"]
            ident = run_key(run)
            if ident not in statuses:
                try:
                    if getattr(self, "_broker_active", False) is True:
                        statuses[ident] = self.broker.check_run_status(run)
                    else:
                        client = self.client_for(run)
                        if run.get("_ci_provider") == "gitcode" and (
                            run.get("_ci_pr_number") or run.get("_ci_dispatch_id")
                        ):
                            try:
                                client.validate_current(run)
                            except RunChanged:
                                statuses[ident] = {"status": "completed"}
                            else:
                                statuses[ident] = {"status": "in_progress"}
                        else:
                            statuses[ident] = client.workflow_status(run["id"])
                            if run.get("_ci_source"):
                                from ci_core.github_source import check_current

                                try:
                                    check_current(client, run["_ci_source"])
                                except ValueError:
                                    statuses[ident] = {"status": "completed"}
                except Exception as exc:
                    statuses[ident] = None
                    self.unavailable_runs.add(ident)
                    logging.warning(
                        "run status unavailable: %s (%s)", run["id"], type(exc).__name__
                    )
            current = statuses[ident]
            if current is None:
                continue
            superseded = current.get("run_attempt", r["run"].get("run_attempt", 1)) > r[
                "run"
            ].get("run_attempt", 1)
            if current.get("status") == "completed" or superseded:
                if key in self.active:
                    self.active[key][1].set()
                elif r["phase"] == "QUEUED":
                    self.finish(
                        key,
                        "CANCELLED",
                        130,
                        ["CI Run ended or source changed before launch"],
                    )
            elif (
                Router._zone_name(self) == "yellow"
                and not r.get("progress_id")
                and (
                    r["item"]["kind"] != "worker_probe"
                    or _is_self_smoke(run)
                    or _is_pr_smoke(run)
                    or run.get("_ci_infra") is True
                )
            ):
                self.maybe_ack(key, r)
        self.consume_control()
        # Limit only discovery. Cancellation, queue progress and outbox keep ticking.
        now = time.monotonic()
        if Router._zone_name(self) != "yellow" or now >= self.next_discovery:
            self.next_discovery = now + 60
            discovered = self.client.running_workflows()
            if getattr(self, "_supervisor_active", False) is True:
                self.supervisor.update_cached_runs(discovered)
            self.receive(discovered)
        if getattr(self, "_supervisor_active", False) is True:
            self.supervisor.tick_watchdog(
                self.client,
                check_remote_every=300.0
                if Router._zone_name(self) == "yellow"
                else 60.0,
            )
        self.pump()
        self.flush()

    def close(self):
        if self.config_executor:
            self.config_executor.shutdown(wait=False, cancel_futures=True)
        self._complete_active()
        for future, event, _ in self.active.values():
            if not future.done():
                event.set()
        self.training.shutdown(wait=True)
        self.light.shutdown(wait=True)
        self._complete_active()
        self.flush()
