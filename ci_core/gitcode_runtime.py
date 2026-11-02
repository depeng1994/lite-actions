"""Schema-4 lifecycle adapter for the existing single Blue Router.

No second scheduler, resource namespace, comment result channel or source-code
catalog discovery is introduced. Dependencies are constructed from the reviewed
release policy; a deployment without platform authority/App adapters stays off.
"""

from __future__ import annotations

import logging
import time

from ci_core.executor import ExternalNpuOccupancy
from ci_core.gitcode_admission import AcceptedRequest
from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    TORCHTITAN_REPOSITORY_IDS,
    request_id,
    source_identity,
)
from ci_core.gitcode_delivery import GitCodeDelivery
from ci_core.gitcode_state import Admission, StateError
from ci_core.worker_probe import probe


class RuntimeError4(ValueError):
    pass


def is_gitcode_record(record):
    return record.get("protocol") == 4


class GitCodeRouterAdapter:
    """Use Router's threads, JobState and physical device/port reservations."""

    def __init__(
        self,
        brokers,
        *,
        executor=None,
        clock=time.time,
        publishers=None,
        responders=None,
    ):
        if not brokers:
            raise RuntimeError4("SCHEMA4_TRUSTED_BROKERS_REQUIRED")
        if executor is None:
            from ci_core.gitcode_execution import execute

            executor = execute
        self.brokers = {}
        self.executor, self.clock, self.router = executor, clock, None
        self.coordinator = brokers[0].coordinator
        for broker in brokers:
            policy = broker.reader.policy
            key = (policy.github_repo_id, policy.control_sha, policy.workflow_id)
            if (
                key in self.brokers
                or policy.github_repo_id not in TORCHTITAN_REPOSITORY_IDS
                or broker.coordinator is not self.coordinator
                or broker.store.root != brokers[0].store.root
                or not policy.enabled
            ):
                raise RuntimeError4("SCHEMA4_SINGLE_COORDINATOR_POLICY_REQUIRED")
            self.brokers[key] = broker
        self.responders = dict(responders or {})
        if set(self.responders) - set(self.brokers):
            raise RuntimeError4("AUTHORITY_RELEASE_NOT_CONFIGURED")
        self.publishers = dict(publishers or {})
        if set(self.publishers) - set(self.brokers):
            raise RuntimeError4("PUBLICATION_RELEASE_NOT_CONFIGURED")
        self.delivery = GitCodeDelivery(
            self.coordinator,
            resolve_request=self.resolve_request,
            channel_for=lambda plan: self.broker(plan).reader.channel,
            cancel_run=self.cancel_run,
            cancel_execution=self.cancel_execution,
            cleanup_ack=self.cleanup_ack,
        )

    def now(self):
        return int(self.clock())

    def bind(self, router):
        if self.router is not None or router.worker != "blue":
            raise RuntimeError4("SCHEMA4_REQUIRES_SINGLE_BLUE_ROUTER")
        self.router = router
        self.recover()

    def broker(self, plan):
        if plan["control"]["github_repo_id"] != plan["github"]["repo_id"]:
            raise RuntimeError4("SCHEMA4_PLAN_REPOSITORY_MISMATCH")
        return self.brokers[
            (
                plan["github"]["repo_id"],
                plan["control"]["control_sha"],
                plan["github"]["workflow_id"],
            )
        ]

    def resolve_request(self, rid):
        reference = self.coordinator.request_reference(rid)
        # All brokers share the same reviewed, persistent request store.
        verified = next(iter(self.brokers.values())).store.load_plan(
            rid, reference["plan_digest"]
        )
        self.broker(verified.plan)
        if request_id(verified.plan) != rid:
            raise RuntimeError4("ROUTER_RETAINED_REQUEST_ID_MISMATCH")
        return verified

    def handles(self, run):
        return (
            self._run_broker(run) is not None
            and run.get("event") == "workflow_dispatch"
            and any(
                isinstance(run.get(field), str)
                and run[field].startswith("lite-gitcode:")
                for field in ("display_title", "name")
            )
        )

    def _run_broker(self, run):
        repository = run.get("repository") or {}
        repo_id = repository.get("id")
        if type(repo_id) is not int or repo_id not in TORCHTITAN_REPOSITORY_IDS:
            return None
        expected_repo = MODEL_REPOSITORIES[repo_id][0]
        if (
            repository.get("full_name") != expected_repo
            or run.get("_ci_repo") != expected_repo
        ):
            return None
        return self.brokers.get((repo_id, run.get("head_sha"), run.get("workflow_id")))

    def receive(self, run):
        broker = self._run_broker(run)
        if broker is None:
            raise RuntimeError4("SCHEMA4_CONTROL_RELEASE_NOT_APPROVED")
        if run.get("run_attempt", 1) != 1:
            raise RuntimeError4("SCHEMA4_NATIVE_RERUN_REFUSED")
        rid = f"{broker.reader.policy.github_repo_id}/{run['id']}/1"
        try:
            reference = self.coordinator.request_reference(rid)
        except StateError as exc:
            if str(exc) != "UNKNOWN_RETAINED_REQUEST":
                raise
        else:
            if not reference["receipt"]["duplicate"]:
                snapshot = self.coordinator.execution_snapshot(
                    reference["execution_id"]
                )
                from ci_core.router import task_key

                missing = any(
                    task_key(run, item) not in self.router.state.read()
                    for item in snapshot["plan"]["cases"]
                )
                if missing:
                    if any(
                        self.router.state.read()[key]
                        .get("gitcode", {})
                        .get("execution_id")
                        == reference["execution_id"]
                        for key in self.router.active
                    ):
                        raise RuntimeError4("INCOMPLETE_ACTIVE_EXECUTION_PROJECTION")
                    self.recover_execution(snapshot, run=run)
            self.delivery.flush()
            return
        accepted = broker.admit(run["id"])
        if accepted is None:
            return
        if not accepted.duplicate:
            self.enqueue(accepted.verified.plan, accepted.admission.receipt, run)
        self.delivery.flush()

    def enqueue(self, plan, receipt, run):
        from ci_core.router import task_key

        broker = self.broker(plan)
        if (
            self._run_broker(run) is not broker
            or run.get("id") != plan["github"]["run_id"]
            or run.get("run_attempt", 1) != plan["github"]["run_attempt"]
        ):
            raise RuntimeError4("ROUTER_RUN_PLAN_IDENTITY_MISMATCH")

        for item in plan["cases"]:
            key = task_key(run, item)
            old = self.router.state.read().get(key)
            binding = {
                "request_id": request_id(plan),
                "execution_id": receipt["canonical_execution_id"],
                "plan_digest": plan["plan_digest"],
            }
            if old is not None:
                if not is_gitcode_record(old) or old.get("gitcode") != binding:
                    raise RuntimeError4("IMMUTABLE_ROUTER_REQUEST_BINDING")
                continue
            self.router.state.update(
                key,
                protocol=4,
                gitcode=binding,
                phase="QUEUED",
                repo=MODEL_REPOSITORIES[plan["github"]["repo_id"]][0],
                run=run,
                item=item,
                digest=plan["plan_digest"],
                defaults={"worker": "blue"},
                queued_at=self.now(),
                deadline=plan["deadline"],
            )

    def context(self, record):
        binding = record["gitcode"]
        reference = self.coordinator.request_reference(binding["request_id"])
        if (
            reference["execution_id"] != binding["execution_id"]
            or reference["plan_digest"] != binding["plan_digest"]
            or reference["receipt"]["duplicate"]
        ):
            raise RuntimeError4("ROUTER_CANONICAL_BINDING_MISMATCH")
        verified = self.resolve_request(binding["request_id"])
        return self.broker(verified.plan), verified, reference["receipt"]

    def guard(self, record):
        broker, verified, receipt = self.context(record)
        return broker.startable_source(
            AcceptedRequest(verified, Admission(receipt, {}))
        )

    def transition(self, key, phase, **fields):
        # No schema-3 progress comment, even when the Checks API is unavailable.
        self.router.state.update(key, phase=phase, **fields)

    def work(self, key, case, cancel):
        record = self.router.state.read()[key]
        item = record["item"]
        broker, verified, receipt = self.context(record)
        eid, cid = receipt["canonical_execution_id"], item["case_id"]
        stored = self.guard(record)
        if cancel.is_set():
            self.finish(key, "CANCELLED", 130, ["Cancelled before claiming execution"])
            return
        if not self.coordinator.claim_case(eid, cid, now=self.now()):
            # A persisted claim is never a license to replay training.
            raise RuntimeError4("CASE_ALREADY_CLAIMED_RECONCILIATION_REQUIRED")
        self.transition(key, "STAGING")

        captured = None

        def before_launch():
            if cancel.is_set():
                raise InterruptedError("schema-4 cancellation requested")
            broker.assert_plan_current(verified.plan)
            broker.reader.actual_run(verified.plan["github"]["run_id"])
            broker.refresh(
                verified.plan["pr"]["repo_id"], verified.plan["pr"]["number"]
            )
            self.coordinator.assert_startable(eid, now=self.now())
            if captured is not None:
                self.coordinator.assert_lease_active(
                    eid,
                    cid,
                    captured["lease_id"],
                    captured["fencing_token"],
                    now=self.now(),
                )

        if item["kind"] == "worker_probe":
            before_launch()
            self.transition(key, "RUNNING")
            outcome = probe()
            lines = (outcome.stdout + outcome.stderr).splitlines()
            expected = self.router.workers["blue"].get("expected_hostname")
            okay = outcome.returncode == 0 and (
                not expected or "WORKER_HOSTNAME=" + expected in lines
            )
            self.finish(
                key, "PASS" if okay else "INFRA_ERROR", 0 if okay else 125, lines
            )
            return

        def capture(runtime):
            nonlocal captured
            token = self.coordinator.bind_lease(
                eid, cid, runtime["lease_id"], runtime["cleanup"], now=self.now()
            )
            captured = {**runtime, "fencing_token": token}
            self.router.state.update(key, runtime=captured)
            return captured

        try:
            outcome = self.executor(
                stored=stored,
                item=item,
                case=case,
                execution_id=eid,
                pools=self.router.pools,
                root=self.router.root,
                cancel=cancel,
                capture=capture,
                before_launch=before_launch,
                phase=lambda value: self.transition(key, value),
            )
        except ExternalNpuOccupancy:
            self.coordinator.retry_unlaunched_case(eid, cid, now=self.now())
            raise
        status = (
            "CANCELLED"
            if outcome.exit_code == 130
            else "TIMED_OUT"
            if outcome.exit_code == 124
            else "INFRA_ERROR"
            if not outcome.release_verified
            else "PASS"
            if outcome.exit_code == 0
            else "FAIL"
        )
        self.finish(
            key, status, outcome.exit_code, outcome.tail, outcome.release_verified
        )

    def finish(self, key, status, rc, lines, clean=True):
        record = self.router.state.read()[key]
        broker, verified, receipt = self.context(record)
        eid, cid = receipt["canonical_execution_id"], record["item"]["case_id"]
        if status == "PASS":
            try:
                self.guard(record)
            except Exception:
                status, rc, lines = (
                    "CANCELLED",
                    130,
                    ["Current execution authority could not be confirmed"],
                )
        runtime = record.get("runtime") or {}
        if runtime.get("lease_id"):
            released = self.coordinator.complete_lease(
                eid,
                cid,
                runtime["lease_id"],
                runtime["fencing_token"],
                cleanup_verified=bool(clean),
            )
            clean = clean and released
        if not clean and status == "PASS":
            status, rc = "INFRA_ERROR", 125
        if status != "PASS":
            self.coordinator.claim_terminal_case(eid, cid)
        result = {
            "case_id": cid,
            "receipt": receipt,
            "identity": source_identity(verified.plan),
            "status": status,
            "exit_code": rc,
            "cleanup_verified": bool(clean),
            "summary": [
                "".join(c if ord(c) >= 32 else " " for c in str(line))[:180]
                for line in lines[-20:]
            ],
        }
        # Persist cleanup uncertainty before any operation that might fail.
        self.router.state.update(
            key, quarantined=bool(record.get("allocation")) and not clean
        )
        self.coordinator.record_case_result(eid, cid, result)
        self.router.state.update(key, phase="PENDING_REPORT", report=result)

    def cancel_execution(self, payload):
        eid = payload["execution_id"]
        for key, record in self.router.state.read().items():
            if record.get("gitcode", {}).get("execution_id") != eid:
                continue
            if key in self.router.active:
                self.router.active[key][1].set()
            elif record.get("phase") == "QUEUED":
                self.finish(
                    key,
                    "CANCELLED",
                    130,
                    ["Execution generation revoked before launch"],
                )
        return True  # SQLite already durably records cancel_requested.

    def cancel_run(self, payload):
        repo_id = payload.get("repo_id")
        candidates = [
            broker
            for (rid, sha, workflow), broker in self.brokers.items()
            if rid == repo_id
            and sha == payload.get("control_sha")
            and ("workflow_id" not in payload or workflow == payload["workflow_id"])
        ]
        if (
            type(repo_id) is not int
            or repo_id not in TORCHTITAN_REPOSITORY_IDS
            or not candidates
        ):
            raise RuntimeError4("CANCEL_RUN_REPOSITORY_MISMATCH")
        # Repository-scoped read clients may differ per release. For legacy
        # payloads without workflow ID, require one unambiguous configured client.
        clients = {id(b.reader.github): b.reader.github for b in candidates}
        if len(clients) != 1:
            raise RuntimeError4("CANCEL_RUN_AMBIGUOUS_RELEASE")
        client = next(iter(clients.values()))
        current = client.request("GET", f"/actions/runs/{payload['run_id']}")
        key = (repo_id, payload["control_sha"], current.get("workflow_id"))
        if (
            current.get("id") != payload["run_id"]
            or current.get("repository", {}).get("id") != repo_id
            or current.get("repository", {}).get("full_name")
            != MODEL_REPOSITORIES[repo_id][0]
            or current.get("head_sha") != payload["control_sha"]
            or key not in self.brokers
            or self.brokers[key] not in candidates
            or current.get("event") != "workflow_dispatch"
        ):
            raise RuntimeError4("CANCEL_RUN_IDENTITY_CHANGED")
        if current.get("run_attempt") != payload.get("run_attempt", 1):
            return True  # Do not cancel a newer attempt under an old callback.
        if current.get("status") == "completed":
            return True
        client.request("POST", f"/actions/runs/{payload['run_id']}/cancel")
        return True

    def cleanup_ack(self, payload):
        snapshot = self.coordinator.execution_snapshot(payload["execution_id"])
        matches = [
            r
            for r in snapshot["leases"]
            if r["lease_id"] == payload["lease_id"]
            and r["id"] == payload["fencing_token"]
            and r["case_id"] == payload["case_id"]
        ]
        return len(matches) == 1 and matches[0]["state"] == (
            "RELEASED" if payload["cleanup_verified"] else "QUARANTINED"
        )

    def maintain(self):
        for broker in self.brokers.values():
            broker.reconcile_registered()
        self.delivery.flush()
        self.flush()
        self.respond_authority()

    def current_authority(self, verified, receipt):
        plan = verified.plan
        broker = self.broker(plan)
        try:
            broker.assert_plan_current(plan)
            broker.reader._actual_run(plan["github"]["run_id"], allow_completed=True)
            from ci_core.gitcode_platform import ObservedGitCodeAuthority

            provider = getattr(broker.verify_plan, "__self__", None)
            fresh_shared_observation = (
                type(provider) is ObservedGitCodeAuthority
                and provider.coordinator.path.resolve()
                == broker.coordinator.path.resolve()
                == self.coordinator.path.resolve()
                and provider.policy == broker.reader.policy
            )
            # Only this concrete provider has just freshly observed AND
            # reconciled the exact Plan in our durable Coordinator transaction.
            # Generic/optional callbacks retain the old explicit refresh path.
            if not fresh_shared_observation:
                broker.refresh(plan["pr"]["repo_id"], plan["pr"]["number"])
            reference = self.coordinator.request_reference(request_id(plan))
            if (
                reference["plan_digest"] != plan["plan_digest"]
                or reference["receipt"] != receipt
            ):
                return False
            self.coordinator.assert_startable(reference["execution_id"], now=self.now())
        except Exception:
            return False
        return True

    def respond_authority(self):
        if not self.responders:
            return
        for reference in self.coordinator.iter_authority_requests(
            now=self.now(), include_expired=True
        ):
            try:
                verified = self.resolve_request(reference["request_id"])
                plan = verified.plan
                if plan.get("authority_protocol") != "artifact-check-v1":
                    continue
                key = (
                    plan["github"]["repo_id"],
                    plan["control"]["control_sha"],
                    plan["github"]["workflow_id"],
                )
                responder = self.responders.get(key)
                if responder is None:
                    continue
                # Completed owners close their exact non-result slot neutral.
                # Confirmed closures are durably acked; old history needs no API.
                if responder.cleanup(verified, reference["receipt"]):
                    continue
                # respond() itself independently verifies the active owning
                # run before reading its challenge. Do not duplicate that GET.
                responder.respond(
                    verified, reference["receipt"], current=self.current_authority
                )
            except Exception as exc:
                logging.warning(
                    "schema-4 authority response pending (%s)", type(exc).__name__
                )

    def flush(self):
        self.delivery.flush()
        for snapshot in self.coordinator.pending_execution_snapshots():
            try:
                plan, receipt = snapshot["plan"], snapshot["receipt"]
                results = [
                    c["result"] for c in snapshot["cases"] if c["result"] is not None
                ]
                if len(results) != len(plan["cases"]):
                    continue
                verified = self.resolve_request(request_id(plan))

                def current():
                    broker = self.broker(plan)
                    try:
                        broker.assert_plan_current(plan)
                        broker.reader.actual_run(plan["github"]["run_id"])
                        broker.refresh(plan["pr"]["repo_id"], plan["pr"]["number"])
                        self.coordinator.assert_startable(
                            snapshot["id"], now=self.now()
                        )
                    except Exception:
                        return False
                    return True

                self.delivery.publish_complete(
                    verified, receipt, results, current=current
                )
                publisher = self.publishers.get(
                    (
                        plan["github"]["repo_id"],
                        plan["control"]["control_sha"],
                        plan["github"]["workflow_id"],
                    )
                )
                if publisher is not None:
                    # A failed/unknown GitCode write remains pending without
                    # re-running cases or acknowledging aggregate delivery.
                    publisher.publish(verified, receipt, results, current=current)
                self.coordinator.mark_aggregate_delivered(
                    snapshot["id"], receipt, now=self.now()
                )
                for key, record in self.router.state.read().items():
                    if (
                        record.get("gitcode", {}).get("execution_id") == snapshot["id"]
                        and record.get("phase") == "PENDING_REPORT"
                    ):
                        self.router.state.update(key, phase="REPORTED")
            except Exception as exc:
                logging.warning("schema-4 aggregate pending (%s)", type(exc).__name__)

    def recover(self):
        """Only outstanding work and captured quarantines need reconstruction."""
        pending = {
            value["id"]: value
            for value in self.coordinator.pending_execution_snapshots(recovery=True)
        }
        for record in self.router.state.read().values():
            if is_gitcode_record(record) and record.get("phase") != "REPORTED":
                eid = record["gitcode"]["execution_id"]
                if eid not in pending:
                    pending[eid] = self.coordinator.execution_snapshot(eid)
        for snapshot in pending.values():
            self.recover_execution(snapshot)

    def recover_execution(self, snapshot, *, run=None):
        plan, receipt = snapshot["plan"], snapshot["receipt"]
        self.broker(plan)  # Unknown release cannot reuse or silently drop state.
        run = run or {
            "id": plan["github"]["run_id"],
            "run_attempt": 1,
            "head_sha": plan["control"]["control_sha"],
            "event": "workflow_dispatch",
            "head_branch": "master",
            "path": plan["github"]["workflow_path"],
            "workflow_id": plan["github"]["workflow_id"],
            "_ci_repo": MODEL_REPOSITORIES[plan["github"]["repo_id"]][0],
            "repository": {
                "id": plan["github"]["repo_id"],
                "full_name": MODEL_REPOSITORIES[plan["github"]["repo_id"]][0],
            },
            "created_at": "1970-01-01T00:00:00Z",
        }
        self.enqueue(plan, receipt, run)
        for item in snapshot["cases"]:
            from ci_core.router import task_key

            key = task_key(run, item)
            leases = [
                lease
                for lease in snapshot["leases"]
                if lease["case_id"] == item["case_id"]
            ]
            if leases:
                lease = leases[0]
                cleanup = lease["cleanup"]
                runtime = {
                    "lease_id": lease["lease_id"],
                    "fencing_token": lease["id"],
                    "cleanup": cleanup,
                }
                self.router.state.update(
                    key,
                    runtime=runtime,
                    allocation=[
                        {"host_id": n["host_id"], "devices": n["device_ids"]}
                        for n in cleanup["nodes"]
                    ],
                    quarantined=lease["state"] != "RELEASED",
                )
            if item["result"] is not None:
                self.router.state.update(
                    key,
                    phase="REPORTED"
                    if snapshot["aggregate_delivered"]
                    else "PENDING_REPORT",
                    report=item["result"],
                )
            elif item["state"] == "CLAIMED":
                self.finish(
                    key,
                    "INFRA_ERROR",
                    125,
                    [
                        "Interrupted claimed case; captured lease retained for reconciliation"
                    ],
                    not leases or all(lease["state"] == "RELEASED" for lease in leases),
                )
