"""Independent Pull workers sharing one schedule/execute implementation."""

import logging
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from ci_core.config import load, runtime_case
from ci_core.contract import REPO, TORCHTITAN_REPO, VLLM_REPO, WORKFLOWS, parse_plan
from ci_core.executor import ExternalNpuOccupancy, execute
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
    return f"{prefix}{run.get('_ci_repo', REPO)}:{run['id']}:{run.get('run_attempt', 1)}:{item['case_id']}"


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
        if any(
            r.get("protocol", 3) != 3 and r.get("phase") != "REPORTED"
            for r in self.state.read().values()
        ):
            raise ValueError("legacy nonterminal state requires prior reconciliation")
        self.training = ThreadPoolExecutor(max_workers=4)
        self.light = ThreadPoolExecutor(max_workers=1)
        self.active = {}
        self.plans = {}
        self.unavailable_runs = set()
        # Unknown children survive a dispatcher crash. Fail closed before admitting overlap.
        for key, r in self.state.read().items():
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

    def client_for(self, run):
        repo = run.get("_ci_repo", REPO)
        if hasattr(self.client, "for_run"):
            return self.client.for_run(run)
        if hasattr(self.client, "for_repo"):
            return self.client.for_repo(repo)
        if getattr(self.client, "repo", repo) != repo:
            raise ValueError("run belongs to another GitHub repository")
        return self.client

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
            return (
                repo in GITCODE_REPOS
                and filename in workflows
                and run.get("event") == "workflow_dispatch"
                and run.get("status") == "in_progress"
                and run.get("head_branch") in branches
                and run.get("actor", {}).get("login") in actors
            )
        if repo == VLLM_REPO:
            actors_value = os.environ.get(
                "LITE_ACTIONS_VLLM_ALLOWED_ACTORS", "depeng1994"
            )
            branches_value = os.environ.get(
                "LITE_ACTIONS_VLLM_ALLOWED_BRANCHES", "main"
            )
        elif repo == TORCHTITAN_REPO:
            actors_value = os.environ.get(
                "LITE_ACTIONS_TORCHTITAN_ALLOWED_ACTORS",
                os.environ.get("LITE_ACTIONS_ALLOWED_ACTORS", ""),
            )
            branches_value = os.environ.get(
                "LITE_ACTIONS_TORCHTITAN_ALLOWED_BRANCHES",
                os.environ.get("LITE_ACTIONS_ALLOWED_BRANCHES", "master"),
            )
        else:
            return False
        allowed = set(filter(None, actors_value.split(",")))
        branches = set(filter(None, branches_value.split(",")))
        event = run.get("event")
        if event in ("pull_request", "pull_request_target"):
            admitted = (
                type(run.get("_ci_pr_number")) is int
                and run.get("actor", {}).get("login") in allowed
                and (
                    (repo == VLLM_REPO and event == "pull_request_target")
                    or (repo == TORCHTITAN_REPO and event == "pull_request")
                )
            )
        else:
            admitted = (
                event == "workflow_dispatch"
                and run.get("head_branch") in branches
                and run.get("actor", {}).get("login") in allowed
            )
        return (
            admitted
            and filename in WORKFLOWS
            and run.get("status") in {"queued", "in_progress"}
        )

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
        if not self.eligible(run):
            return
        identity = (
            run.get("_ci_provider", "github"),
            run.get("_ci_repo", REPO),
            run["id"],
            run.get("run_attempt", 1),
            run["head_sha"],
        )
        cached = self.plans.get(identity)
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
            is_catalog = bool(run.get("_ci_catalog"))
            self.plans[identity] = (plan, is_catalog)
        else:
            plan, is_catalog = cached
            if is_catalog:
                run = {**run, "_ci_catalog": True}
        owned = [c for c in plan["cases"] if c["worker"] == self.worker]
        if is_catalog:
            from ci_core.gitcode import plan_receipt

            if len(owned) != len(plan["cases"]):
                raise ValueError("catalog pilot requires a single owning worker")
            plan_receipt(run, plan)
        if run.get("_ci_repo", REPO) == VLLM_REPO:
            from ci_core.vllm_source import is_blue_plan, read_source

            if is_blue_plan(plan):
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
                        **(
                            {"plan_receipt": plan, "receipt_pending": True}
                            if is_catalog
                            else {}
                        ),
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
                    ["STALE: native source or run changed before publication"],
                )
            except Exception:
                status, rc, lines = (
                    "INFRA_ERROR",
                    125,
                    ["Native source authority unavailable before publication"],
                )
        if status == "PASS" and r["run"].get("_ci_provider") == "gitcode":
            try:
                self.client_for(r["run"]).validate_current(r["run"])
            except ValueError:
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
            quarantined=bool(r.get("allocation")) and not clean,
            report=report(r["run"], r["item"], r["digest"], status, rc, lines, clean),
        )

    def work(self, key, case, lease, cancel):
        r = self.state.read()[key]
        item = r["item"]
        run = r["run"]
        launched = False
        try:
            if run.get("_ci_provider") == "gitcode":
                self.client_for(run).validate_current(run)
            self.transition(key, "STAGING")
            with tempfile.TemporaryDirectory(prefix="lite-definition-") as tmp:
                source_repo = run.get("_ci_repo", REPO)
                archive = (
                    Path(tmp) / "unused-probe-source"
                    if source_repo == VLLM_REPO and item["kind"] == "worker_probe"
                    else model_archive(
                        run.get("_ci_execution_sha", run["head_sha"]),
                        Path(tmp),
                        source_repo=source_repo,
                        provider=run.get("_ci_provider", "github"),
                    )
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
                from ci_core.vllm_source import check_current

                check_current(self.client_for(run), run["_ci_source"], check_run=True)
                case = {**case, "native_source": run["_ci_source"]}
            subrun = {
                **run,
                "_ci_test_id": item["test_id"],
                "_ci_cancel_event": cancel,
            }
            if r.get("use_instance_id"):
                subrun["_ci_case_id"] = item["case_id"]

            def on_phase(phase):
                nonlocal launched
                if phase == "LAUNCHING":
                    launched = True
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

    def _run_after_handoff(self, handoff, *args):
        if handoff.result():
            self.work(*args)

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
            ident = (
                r["run"].get("_ci_provider", "github"),
                r["run"].get("_ci_repo", REPO),
                r["run"]["id"],
            )
            if ident in self.unavailable_runs or r.get("receipt_pending"):
                if pool and is_smoke:
                    blocked_pools.add(queue)
                continue
            if key in self.active:
                continue
            if queue in blocked_pools:
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
                event = threading.Event()
                future = (self.light if light else self.training).submit(
                    self._run_after_handoff, handoff, key, case, lease, event
                )
                self.active[key] = (future, event, r["run"]["id"])
                handoff.set_result(True)
            except BaseException as exc:
                if handoff.done():
                    raise
                handoff.set_result(False)
                clean = True
                if lease:
                    try:
                        lease.close()
                    except Exception:
                        clean = False
                        logging.exception("lease release failed after submission error")
                if not isinstance(exc, Exception):
                    raise
                self.finish(key, "INFRA_ERROR", 125, [str(exc)[:180]], clean)

    def flush(self):
        pending_plans = {}
        for key, record in self.state.read().items():
            if record.get("receipt_pending") and record.get("phase") == "QUEUED":
                run = record["run"]
                identity = (
                    run.get("_ci_provider"),
                    run.get("_ci_repo"),
                    run["id"],
                    run.get("run_attempt", 1),
                )
                pending_plans.setdefault(identity, []).append((key, record))
        for rows in pending_plans.values():
            keys = [key for key, _ in rows]
            record = rows[0][1]
            try:
                client = self.client_for(record["run"])
                client.validate_current(record["run"])
                client.publish_plan(
                    record["run"],
                    record["plan_receipt"],
                    os.environ.get("LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author),
                )
                self.state.update_many(keys, receipt_pending=False)
            except Exception:
                logging.warning("smoke plan receipt remains pending: %s", keys[0])
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
            ident = (
                run.get("_ci_provider", "github"),
                run.get("_ci_repo", REPO),
                run["id"],
            )
            if ident not in statuses:
                try:
                    statuses[ident] = self.client_for(run).workflow_status(run["id"])
                except Exception as exc:
                    statuses[ident] = None
                    self.unavailable_runs.add(ident)
                    logging.warning(
                        "run status unavailable: %s (%s)", run["id"], type(exc).__name__
                    )
            current = statuses[ident]
            if current is not None and run.get("_ci_source"):
                from ci_core.vllm_source import check_current

                try:
                    check_current(self.client_for(run), run["_ci_source"])
                except ValueError:
                    current = {
                        **current,
                        "status": "completed",
                        "conclusion": "cancelled",
                    }
                    if key in self.active:
                        self.active[key][1].set()
                except Exception:
                    self.unavailable_runs.add(ident)
                    current = None
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
