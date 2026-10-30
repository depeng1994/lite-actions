"""Independent Pull workers sharing one schedule/execute implementation."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from ci_core.contract import parse_plan, WORKFLOWS, REPO
from ci_core.v2_config import load, runtime_case
from ci_core.v2_plan import verify_definition
from ci_core.v2_reporting import report, post, progress
from ci_core.job_state import JobState
from ci_core.resources import (
    reserve,
    reserve_ports,
    reserve_host_budget,
    shared_resource_lock_root,
    resource_keys,
    ResourceBusy,
)
from ci_core.scheduler import candidates
from ci_core.ssh_runner import model_archive
from ci_core.dispatch import execute, ExternalNpuOccupancy
from ci_core.worker_runner import PROBE


def task_key(run, item):
    return f"{REPO}:{run['id']}:{run.get('run_attempt', 1)}:{item['case_id']}"


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
        self.state = JobState(self.root)
        self.training = ThreadPoolExecutor(max_workers=4)
        self.light = ThreadPoolExecutor(max_workers=1)
        self.active = {}
        self.plans = {}
        # Unknown children survive a dispatcher crash. Fail closed before admitting overlap.
        for key, r in self.state.read().items():
            if r.get("phase") == "RECEIVED":
                self.state.update(key, phase="QUEUED")
            if r.get("phase") in {"RESERVED", "STAGING", "RUNNING", "CLEANING"}:
                result = report(
                    r["run"],
                    r["item"],
                    r["digest"],
                    "INFRA_ERROR",
                    125,
                    ["Worker restarted; remote reconciliation required"],
                    False,
                )
                self.state.update(
                    key,
                    phase="PENDING_REPORT",
                    quarantined=bool(r.get("allocation")),
                    report=result,
                )

    def eligible(self, run):
        filename = (run.get("path") or "").rsplit("/", 1)[-1]
        allowed = set(
            filter(None, os.environ.get("LITE_ACTIONS_ALLOWED_ACTORS", "").split(","))
        )
        branches = set(
            filter(
                None,
                os.environ.get("LITE_ACTIONS_ALLOWED_BRANCHES", "master").split(","),
            )
        )
        event = run.get("event")
        if event == "pull_request":
            admitted = (
                type(run.get("_ci_pr_number")) is int
                and run.get("actor", {}).get("login") in allowed
            )
        else:
            admitted = (
                event == "workflow_dispatch"
                and run.get("head_branch") in branches
                and run.get("actor", {}).get("login") in allowed
            )
        return admitted and filename in WORKFLOWS and run.get("status") == "in_progress"

    def transition(self, key, phase, **fields):
        self.state.update(key, phase=phase, **fields)
        r = self.state.read()[key]
        try:
            ident = progress(
                self.client,
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
        for run in sorted(runs, key=lambda r: (r.get("created_at", ""), r["id"])):
            if not self.eligible(run):
                continue
            identity = (run["id"], run.get("run_attempt", 1), run["head_sha"])
            plan = self.plans.get(identity)
            if plan is None:
                plan = self.client.ci_request(run)
                if plan is None:
                    continue
                parse_plan(plan, run, run["path"].rsplit("/", 1)[-1])
                self.plans[identity] = plan
            for item in plan["cases"]:
                if item["worker"] != self.worker:
                    continue
                key = task_key(run, item)
                if key in self.state.read():
                    continue
                self.state.update(
                    key,
                    phase="QUEUED",
                    run=run,
                    item=item,
                    digest=plan["plan_digest"],
                    defaults=plan["defaults"],
                    queued_at=time.time(),
                )
                self.transition(key, "QUEUED")

    def claim(self, case):
        options = candidates(case, self.pools)
        if not options:
            raise ValueError("NO_VERIFIED_TOPOLOGY")
        if case.get("docker"):
            inventory = {n["host_id"]: n for n in self.pools[case["pool"]]["nodes"]}
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
            if r.get("quarantined"):
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
        self.state.update(
            key,
            phase="PENDING_REPORT",
            quarantined=bool(r.get("allocation")) and not clean,
            report=report(r["run"], r["item"], r["digest"], status, rc, lines, clean),
        )

    def work(self, key, case, lease, cancel):
        r = self.state.read()[key]
        item = r["item"]
        run = r["run"]
        launched = False
        try:
            self.transition(key, "STAGING")
            with tempfile.TemporaryDirectory(prefix="lite-v3-definition-") as tmp:
                archive = model_archive(run["head_sha"], Path(tmp))
                verify_definition(archive, item, r["defaults"])
            if cancel.is_set():
                self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
                return
            if item["kind"] == "worker_probe":
                self.transition(key, "RUNNING")
                p = subprocess.run(
                    ["bash", "-lc", PROBE], capture_output=True, text=True, timeout=90
                )
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
            subrun = {
                **run,
                "_ci_test_id": item["test_id"],
                "_ci_cancel_event": cancel,
                "_ci_request": dict(
                    schema=1,
                    run_id=run["id"],
                    attempt=run.get("run_attempt", 1),
                    sha=run["head_sha"],
                    cases=[{"test_id": item["test_id"]}],
                ),
            }

            def on_phase(phase):
                nonlocal launched
                if phase == "LAUNCHING":
                    launched = True
                else:
                    self.transition(key, phase)

            subrun["_ci_phase_callback"] = on_phase
            execution = execute(case, subrun, root=self.root, pre_reserved=True)
            self.transition(key, "CLEANING")
            from ci_core.reporting import case_summary

            checks = case_summary(
                execution.log, {"_ci_expanded_cases": [{"test_id": item["test_id"]}]}
            )
            valid = len(checks) == 1 and checks[0]["status"] == "PASS"
            status = (
                "CANCELLED"
                if execution.exit_code == 130
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
                execution.exit_code,
                execution.tail,
                execution.release_verified,
            )
        except ExternalNpuOccupancy:
            # Read-only check: no rank has launched, no device is quarantined.
            self.state.update(
                key,
                phase="QUEUED",
                allocation=[],
                quarantined=False,
                unavailable=r.get("unavailable", [])
                + [{"allocation": case["allocation"], "until": time.time() + 60}],
            )
        except subprocess.TimeoutExpired:
            self.finish(
                key, "TIMED_OUT", 124, ["Worker operation timed out"], not launched
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

    def pump(self):
        for key, (future, event, runid) in list(self.active.items()):
            if future.done():
                future.result()
                del self.active[key]
        records = self.state.read()
        blocked_pools = set()
        queued = sorted(
            ((k, r) for k, r in records.items() if r.get("phase") == "QUEUED"),
            key=lambda kv: (
                kv[1]["run"].get("created_at", ""),
                kv[1]["run"]["id"],
                kv[1]["item"]["order"],
            ),
        )
        for key, r in queued:
            if key in self.active:
                continue
            item = r["item"]
            pool = item["target_pool"]
            if pool in blocked_pools:
                continue
            if time.time() - r["queued_at"] > 3600:
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
                    blocked_pools.add(pool)
                continue
            try:
                case = None
                lease = None
                if not light:
                    case = runtime_case(item, self.pools, self.worker)
                    case["_temporarily_unavailable"] = [
                        x for x in r.get("unavailable", []) if x["until"] > time.time()
                    ]
                    claim = self.claim(case)
                    if claim is None:
                        blocked_pools.add(pool)
                        continue
                    lease, case = claim
                self.state.update(
                    key,
                    phase="RESERVED",
                    allocation=case["allocation"] if case else [],
                    runtime=case,
                )
                event = threading.Event()
                future = (self.light if light else self.training).submit(
                    self.work, key, case, lease, event
                )
                self.active[key] = (future, event, r["run"]["id"])
            except Exception as exc:
                self.finish(key, "INFRA_ERROR", 125, [str(exc)[:180]])

    def flush(self):
        for key, r in self.state.read().items():
            if r.get("phase") != "PENDING_REPORT":
                continue
            try:
                post(self.client, r["run"], r["item"], r["report"], self.author)
                self.state.update(key, phase="REPORTED")
            except Exception:
                logging.warning("result remains pending: %s", key)

    def tick(self):
        # Outbox survives completed GitHub Runs; never depend on an active-run listing.
        self.flush()
        statuses = {}
        for key, r in self.state.read().items():
            if r.get("phase") not in {
                "QUEUED",
                "RESERVED",
                "STAGING",
                "RUNNING",
                "CLEANING",
            }:
                continue
            ident = r["run"]["id"]
            if ident not in statuses:
                statuses[ident] = self.client.workflow_status(ident)
            current = statuses[ident]
            superseded = current.get("run_attempt", r["run"].get("run_attempt", 1)) > r[
                "run"
            ].get("run_attempt", 1)
            if current.get("status") == "completed" or superseded:
                if key in self.active:
                    self.active[key][1].set()
                elif r["phase"] == "QUEUED":
                    self.finish(
                        key, "CANCELLED", 130, ["GitHub Run completed before launch"]
                    )
        self.receive(self.client.running_workflows())
        self.pump()
        self.flush()

    def close(self):
        for _, event, _ in self.active.values():
            event.set()
        self.training.shutdown(wait=True)
        self.light.shutdown(wait=True)
        self.flush()
