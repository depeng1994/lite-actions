"""Independent Pull workers sharing one schedule/execute implementation."""

import logging
import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from pathlib import Path

from ci_core.config import load, runtime_case
from ci_core.contract import (
    REPO,
    SELF_REPO,
    SELF_WORKFLOW,
    configured_repos,
    parse_plan,
)
from ci_core.executor import ExternalNpuOccupancy, execute
from ci_core.gitcode import RunChanged
from ci_core.job_state import JobState
from ci_core.plan import verify_definition
from ci_core.reporting import body_for, post, progress, progress_body, report
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


def self_request(run):
    return (
        run.get("_ci_provider", "github") == "github"
        and run.get("_ci_repo") == SELF_REPO
        and run.get("path") == ".github/workflows/" + SELF_WORKFLOW
    )


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
    def __init__(self, root, worker, client, author, *, deployment_config=None):
        self.deployment = deployment_config
        self.next_discovery = 0.0
        self.release_logs = None
        self.next_expiry = 0.0
        if (
            worker == "blue"
            and deployment_config
            and deployment_config.get("public_release")
        ):
            from ci_core.github import ReleaseLogs

            self.release_logs = ReleaseLogs(
                worker,
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
        self.workers, self.pools = load(self.root, worker)
        if deployment_config is not None:
            self.workers[worker]["case_environments"] = deployment_config[
                "case_environments"
            ]
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
        if not str(run.get("name", "")).startswith("lite-ci-") or run.get(
            "status"
        ) not in {"queued", "in_progress"}:
            return False
        if (
            run.get("_ci_repo", REPO) == SELF_REPO
            and (run.get("path") or "").rsplit("/", 1)[-1] == SELF_WORKFLOW
        ):
            from ci_core.self_identity import checkout_sha

            return self.self_sha is not None and run.get(
                "head_sha"
            ) == self.self_sha == checkout_sha(self.root)
        return True

    def transition(self, key, phase, **fields):
        self.state.update(key, phase=phase, **fields)
        r = self.state.read()[key]
        if self.worker == "yellow":
            if phase == "QUEUED":
                self.maybe_ack(key, r)
            return
        if r.get("last_sent_phase") == phase:
            return
        try:
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
        client = self.client_for(r["run"])
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
            if n:
                target = progress_body(run, item, r["digest"], "QUEUED")
                matches = [
                    c
                    for c in client.commit_comments(run["head_sha"])
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
            ident = progress(client, run, item, r["digest"], "QUEUED")
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
        if not self.deployment or run.get("_ci_provider", "github") != "github":
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
            run.get("_ci_dispatch_id"),
        )
        cached = self.plans.get(identity)
        if cached is not None:
            owned = [c for c in cached["cases"] if c["worker"] == self.worker]
            if not owned or all(task_key(run, c) in self.state.read() for c in owned):
                return
        if not self.eligible(run):
            return
        generic_source = None
        if run.get("_ci_provider") != "gitcode" and not (
            run.get("_ci_repo") == SELF_REPO
            and run.get("path", "").rsplit("/", 1)[-1] == SELF_WORKFLOW
        ):
            from ci_core.github_source import bind_request_source, read_source_artifact

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
            if self_request(run):
                from ci_core.self_smoke import expected_plan

                if plan != expected_plan(run):
                    raise ValueError("self-smoke plan differs from its pinned profile")
            elif any(i["kind"] == "worker_probe" for i in plan["cases"]):
                raise ValueError("MODEL_HOST_PROBE_FORBIDDEN")
            self.plans[identity] = plan
        else:
            plan = cached
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
        self, key, status, rc, lines, clean=True, *, locator=None, diagnostic=None
    ):
        r = self.state.read()[key]
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
            if self.worker == "blue" and diagnostic is not None:
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

        self.state.update(
            key,
            phase="PENDING_REPORT",
            quarantined=bool(r.get("allocation"))
            and not clean
            and (self.quarantine_enabled or r["item"].get("worker") != self.worker),
            report=result,
        )

    def work(self, key, case, lease, cancel, preflight):
        r = self.state.read()[key]
        item = r["item"]
        run = r["run"]
        launched = False
        try:
            if self_request(run):
                from ci_core.self_identity import checkout_sha

                if (
                    self.self_sha != run["head_sha"]
                    or checkout_sha(self.root) != self.self_sha
                ):
                    raise ValueError("self-smoke Worker SHA changed")
            if run.get("_ci_provider") == "gitcode":
                self.client_for(run).validate_current(run)
            if run.get("_ci_source"):
                from ci_core.github_source import check_current

                check_current(self.client_for(run), run["_ci_source"], check_run=True)
            self.transition(key, "STAGING")
            if cancel.is_set():
                self.finish(key, "CANCELLED", 130, ["Cancelled before launch"])
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
                elif self_request(run):
                    from ci_core.self_smoke import expected_plan

                    approved = expected_plan(run)
                    if (
                        r["digest"] != approved["plan_digest"]
                        or item not in approved["cases"]
                    ):
                        raise ValueError("self-smoke probe identity mismatch")
                else:
                    raise ValueError("MODEL_HOST_PROBE_FORBIDDEN")
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
                    locator=locator,
                )
                return
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
            from ci_core.registry import validate_case_environment

            environment = validate_case_environment(
                definition["env_vars"], kind=item["kind"]
            )
            # The model selects values; only the deployment may authorize them.
            rules = (
                self.workers[self.worker]
                .get("case_environments", {})
                .get(item["docker"], {})
            )
            if any(
                type(rules.get(k)) is not list or v not in rules[k]
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
                subrun["_ci_zone"] = self.worker
                subrun["_ci_worker_address"] = self.deployment["address"]
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
                self.finish(
                    key,
                    "INFRA_ERROR",
                    125,
                    [f"Capacity check failed: {type(exc).__name__}"],
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
                post(
                    self.client_for(r["run"]),
                    r["run"],
                    r["item"],
                    r["report"],
                    os.environ.get("LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author)
                    if r["run"].get("_ci_provider") == "gitcode"
                    else self.author,
                    local_worker=self.worker,
                    cache=cache,
                    verify_only=r.get("phase") == "VERIFY_REPORT",
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
                self.worker == "yellow"
                and not r.get("progress_id")
                and (
                    r["item"]["kind"] != "worker_probe"
                    or self_request(run)
                    or run.get("_ci_infra") is True
                )
            ):
                self.maybe_ack(key, r)
        now = time.monotonic()
        if self.worker != "yellow" or now >= self.next_discovery:
            self.next_discovery = now + 60
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
