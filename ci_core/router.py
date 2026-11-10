"""Independent Pull workers sharing one schedule/execute implementation."""

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
from pathlib import Path
from urllib.parse import quote

from ci_core.config import load, runtime_case
from ci_core.contract import (
    REPO,
    SELF_REPO,
    SELF_WORKFLOW,
    TORCHTITAN_REPO,
    VLLM_REPO,
    WORKFLOWS,
    configured_repos,
    parse_plan,
)
from ci_core.executor import ExternalNpuOccupancy, execute
from ci_core.gitcode import RunChanged, source_workflows
from ci_core.job_state import JobState
from ci_core.plan import verify_definition
from ci_core.reporting import post, progress, report
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


class Router:
    def __init__(self, root, worker, client, author):
        if not author:
            raise ValueError("LITE_ACTIONS_RESULT_AUTHOR is required")
        self.root, self.worker, self.client, self.author = (
            Path(root),
            worker,
            client,
            author,
        )
        self.workers, self.pools = load(self.root, worker)
        self.self_sha = None
        if SELF_REPO in configured_repos():
            if os.environ.get("LITE_ACTIONS_SELF_SMOKE_ENABLED") != "1":
                raise ValueError("self-smoke requires explicit opt-in")
            from ci_core.self_identity import checkout_sha

            self.self_sha = checkout_sha(self.root)
        self.state = JobState(self.root)
        self.quarantine_enabled = worker not in {
            name.strip()
            for name in os.environ.get(
                "LITE_ACTIONS_DISABLE_QUARANTINE_WORKERS", ""
            ).split(",")
            if name.strip()
        }
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
                self.state.update(
                    key,
                    phase="PENDING_REPORT",
                    quarantined=bool(r.get("allocation"))
                    and (self.quarantine_enabled or r["item"].get("worker") != worker),
                    report=result,
                )

    def client_for(self, run):
        repo = run.get("_ci_repo", REPO)
        if hasattr(self.client, "for_run"):
            return self.client.for_run(run)
        if hasattr(self.client, "for_repo"):
            return self.client.for_repo(repo)
        if getattr(self.client, "repo", repo) != repo:
            raise ValueError("run belongs to another GitHub repository")
        return self.client

    def trusted_manual(self, run):
        """Admit an authorized repository writer at its branch's current SHA."""
        actor = (run.get("triggering_actor") or run.get("actor") or {}).get("login")
        branch = run.get("head_branch")
        sha = run.get("_ci_control_sha", run.get("head_sha"))
        if (
            not isinstance(actor, str)
            or not isinstance(branch, str)
            or not branch
            or len(branch) > 255
            or any(ord(c) < 32 for c in branch)
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
        ):
            return False
        try:
            client = self.client_for(run)
            if actor == "github-actions[bot]":
                if (
                    run.get("_ci_repo") != VLLM_REPO
                    or type(run.get("_ci_pr_number")) is not int
                    or client.request("GET", "").get("default_branch") != branch
                ):
                    return False
                # Source admission verifies the recorded controller against
                # trusted default-branch history, including a concurrent push.
                return True
            else:
                if not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", actor
                ):
                    return False
                result = client.request(
                    "GET", f"/collaborators/{quote(actor, safe='')}/permission"
                )
                if result.get("permission") not in {"write", "maintain", "admin"}:
                    return False
            current = client.request("GET", "/branches/" + quote(branch, safe=""))
            return current.get("commit", {}).get("sha") == sha
        except Exception:
            logging.warning("repository permission or branch verification unavailable")
            return False

    def eligible(self, run):
        repo = run.get("_ci_repo", REPO)
        filename = (run.get("path") or "").rsplit("/", 1)[-1]
        if run.get("_ci_provider") == "gitcode":
            from ci_core.contract import GITCODE_REPOS

            actors = set(
                filter(
                    None,
                    os.environ.get("LITE_ACTIONS_GITCODE_ALLOWED_ACTORS", "").split(
                        ","
                    ),
                )
            )
            branches = set(
                filter(
                    None,
                    os.environ.get(
                        "LITE_ACTIONS_GITCODE_ALLOWED_BRANCHES", "master"
                    ).split(","),
                )
            )
            workflows = set(
                filter(
                    None,
                    os.environ.get(
                        "LITE_ACTIONS_GITCODE_WORKFLOWS", "lite-actions.yml"
                    ).split(","),
                )
            )
            if filename in source_workflows() and not run.get("_ci_dispatch_id"):
                return False
            if run.get("event") == "pull_request":
                return (
                    repo in GITCODE_REPOS
                    and filename in workflows & source_workflows()
                    and run.get("status") == "in_progress"
                    and type(run.get("_ci_pr_number")) is int
                    and isinstance(run.get("_ci_source_repo"), str)
                )
            return (
                repo in GITCODE_REPOS
                and filename in workflows
                and run.get("event") == "workflow_dispatch"
                and run.get("status") == "in_progress"
                and run.get("head_branch") in branches
                and run.get("actor", {}).get("login") in actors
            )
        if repo == SELF_REPO:
            from ci_core.self_identity import checkout_sha

            return (
                self.self_sha is not None
                and run.get("event") == "workflow_dispatch"
                and filename == SELF_WORKFLOW
                and run.get("status") in {"queued", "in_progress"}
                and run.get("head_sha") == self.self_sha == checkout_sha(self.root)
                and self.trusted_manual(run)
            )
        if filename not in WORKFLOWS or run.get("status") not in {
            "queued",
            "in_progress",
        }:
            return False
        if repo == VLLM_REPO:
            allowed = set()
            branches = set()
        elif repo == TORCHTITAN_REPO:
            actors_value = os.environ.get(
                "LITE_ACTIONS_TORCHTITAN_ALLOWED_ACTORS",
                os.environ.get("LITE_ACTIONS_ALLOWED_ACTORS", ""),
            )
            branches_value = os.environ.get(
                "LITE_ACTIONS_TORCHTITAN_ALLOWED_BRANCHES",
                os.environ.get("LITE_ACTIONS_ALLOWED_BRANCHES", "master"),
            )
            allowed = set(filter(None, actors_value.split(",")))
            branches = set(filter(None, branches_value.split(",")))
        else:
            return False
        event = run.get("event")
        if event in ("pull_request", "pull_request_target"):
            admitted = type(run.get("_ci_pr_number")) is int and (
                (repo == VLLM_REPO and event == "pull_request_target")
                or (
                    repo == TORCHTITAN_REPO
                    and event == "pull_request"
                    and run.get("actor", {}).get("login") in allowed
                )
            )
        else:
            admitted = event == "workflow_dispatch" and (
                self.trusted_manual(run)
                if repo == VLLM_REPO
                else run.get("head_branch") in branches
                and run.get("actor", {}).get("login") in allowed
            )
        return admitted

    def transition(self, key, phase, **fields):
        self.state.update(key, phase=phase, **fields)
        r = self.state.read()[key]
        try:
            ident = progress(
                self.client_for(r["run"]),
                r["run"],
                r["item"],
                r["digest"],
                phase,
                r.get("progress_id"),
            )
            self.state.update(key, progress_id=ident)
        except Exception:
            logging.warning("progress pending for %s", key)

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

    def _receive_one(self, run):
        identity = (
            run.get("_ci_provider", "github"),
            run.get("_ci_repo", REPO),
            run["id"],
            run.get("run_attempt", 1),
            run["head_sha"],
            run.get("_ci_dispatch_id"),
        )
        cached = self.plans.get(identity)
        if cached is not None:
            owned = [c for c in cached["cases"] if c["worker"] == self.worker]
            if not owned or all(task_key(run, c) in self.state.read() for c in owned):
                return
        if not self.eligible(run):
            return
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
            if run.get("_ci_repo") == SELF_REPO:
                from ci_core.self_smoke import expected_plan

                if plan != expected_plan(run):
                    raise ValueError("self-smoke plan differs from its pinned profile")
            self.plans[identity] = plan
        else:
            plan = cached
        owned = [c for c in plan["cases"] if c["worker"] == self.worker]
        if not owned or all(task_key(run, c) in self.state.read() for c in owned):
            return
        if run.get("_ci_repo") == VLLM_REPO:
            from ci_core.vllm_source import read_source, requires_source

            if requires_source(plan):
                source = read_source(self.client_for(run), run, plan)
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
        for key in self.state.admit(entries):
            self.transition(key, "QUEUED")

    def claim(self, case):
        options = candidates(case, self.pools)
        if not options:
            raise ValueError("NO_VERIFIED_TOPOLOGY")
        inventory = {n["host_id"]: n for n in self.pools[case["pool"]]["nodes"]}
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
        blocked = set()
        for r in self.state.read().values():
            if self.quarantine_enabled and r.get("quarantined"):
                blocked.update(resource_keys(r.get("allocation", [])))
        for allocation in options:
            if any(
                resource_keys(x["allocation"]) == resource_keys(allocation)
                and x["until"] > time.time()
                for x in case.get("_temporarily_unavailable", [])
            ):
                continue
            if blocked.intersection(resource_keys(allocation)):
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
        return None

    def finish(self, key, status, rc, lines, clean=True):
        r = self.state.read()[key]
        if status == "PASS" and r["run"].get("_ci_source"):
            from ci_core.vllm_source import check_current

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
        self.state.update(
            key,
            phase="PENDING_REPORT",
            quarantined=bool(r.get("allocation"))
            and not clean
            and (self.quarantine_enabled or r["item"].get("worker") != self.worker),
            report=report(r["run"], r["item"], r["digest"], status, rc, lines, clean),
        )

    def work(self, key, case, lease, cancel, preflight):
        r = self.state.read()[key]
        item = r["item"]
        run = r["run"]
        launched = False
        try:
            if run.get("_ci_repo") == SELF_REPO:
                from ci_core.self_identity import checkout_sha

                if (
                    self.self_sha != run["head_sha"]
                    or checkout_sha(self.root) != self.self_sha
                ):
                    raise ValueError("self-smoke Worker SHA changed")
            if run.get("_ci_provider") == "gitcode":
                self.client_for(run).validate_current(run)
            if run.get("_ci_source"):
                from ci_core.vllm_source import check_current

                check_current(self.client_for(run), run["_ci_source"], check_run=True)
            self.transition(key, "STAGING")
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
                verify_definition(archive, item, r["defaults"], repo=source_repo)
            if run.get("_ci_provider") == "gitcode":
                self.client_for(run).validate_current(run)
            if cancel.is_set():
                self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
                return
            if item["kind"] == "worker_probe":
                self.transition(key, "RUNNING")
                p = probe_worker()
                if cancel.is_set():
                    self.finish(
                        key, "CANCELLED", 130, ["Cancelled during worker probe"]
                    )
                    return
                expected = self.workers[self.worker].get("expected_hostname")
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
                )
                return
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
            self.finish(
                key,
                status,
                130 if status == "CANCELLED" else execution.exit_code,
                execution.tail,
                execution.release_verified,
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
                [f"Infrastructure error: {type(exc).__name__}: {str(exc)[:160]}"],
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
        queued = sorted(
            ((k, r) for k, r in records.items() if r.get("phase") == "QUEUED"),
            key=lambda kv: (
                kv[1]["run"].get("created_at", ""),
                str(kv[1]["run"]["id"]),
                kv[1]["item"]["order"],
            ),
        )
        # Keep the legacy ordering and its positions relative to Smoke while
        # ordering only the Smoke positions by durable admission sequence.
        smoke = iter(
            sorted(
                (pair for pair in queued if pair[1]["item"]["kind"] == "npu_smoke"),
                key=lambda pair: pair[1].get("admission_seq", 0),
            )
        )
        queued = [
            next(smoke) if r["item"]["kind"] == "npu_smoke" else (key, r)
            for key, r in queued
        ]
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
            if not is_smoke and time.time() - r["queued_at"] > 3600:
                self.finish(
                    key,
                    "RESOURCE_TIMEOUT",
                    124,
                    ["Resource wait exceeded 3600 seconds"],
                )
                continue
            light = item["kind"] == "worker_probe"
            count = sum(
                (records[k]["item"]["kind"] == "worker_probe") == light
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
                    claim = self.claim(case)
                    if claim is None:
                        blocked_pools.add(queue)
                        continue
                    lease, case = claim
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
                self.finish(key, "INFRA_ERROR", 125, [str(exc)[:180]], clean)

    def flush(self):
        for key, r in self.state.read().items():
            if r.get("phase") != "PENDING_REPORT":
                continue
            try:
                post(
                    self.client_for(r["run"]),
                    r["run"],
                    r["item"],
                    r["report"],
                    os.environ.get("LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author)
                    if r["run"].get("_ci_provider") == "gitcode"
                    else self.author,
                )
                self.state.update(key, phase="REPORTED")
            except Exception:
                logging.warning("result remains pending: %s", key)

    def tick(self):
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
                    client = self.client_for(run)
                    if run.get("_ci_provider") == "gitcode" and (
                        run.get("event") == "pull_request" or run.get("_ci_dispatch_id")
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
                            from ci_core.vllm_source import check_current

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
        self.receive(self.client.running_workflows())
        self.pump()
        self.flush()

    def close(self):
        self._complete_active()
        for future, event, _ in self.active.values():
            if not future.done():
                event.set()
        self.training.shutdown(wait=True)
        self.light.shutdown(wait=True)
        self._complete_active()
        self.flush()
