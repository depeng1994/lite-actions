"""Schema-4 admission broker inside a trusted Blue control process.

This is not a second scheduler and never allocates devices. Authority/revision
and EventProof callbacks are authenticated deployment adapters, not Plan fields.
An unconfigured proof provider must fail closed; SHA equality is not an event
history or an ABA proof. The legacy schema-3 executor must not consume the
returned execution source until a compatible SourceHandle backend is installed.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ci_core.gitcode_contract import VerifiedRequest, request_id
from ci_core.gitcode_state import Admission, Authority, EventProof
from ci_core.gitcode_store import StoredExecution


class AdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class AcceptedRequest:
    verified: VerifiedRequest
    admission: Admission

    @property
    def execution_id(self):
        return self.admission.receipt["canonical_execution_id"]

    @property
    def duplicate(self):
        return self.admission.receipt["duplicate"]


class GitCodeAdmissionBroker:
    def __init__(
        self,
        *,
        reader,
        coordinator,
        store,
        workspace: Path,
        read_authority: Callable[[str, int], Authority],
        verify_event: Callable[[dict], EventProof],
        clock: Callable[[], int],
    ):
        if reader.policy.github_repo_id != 1210038933:
            raise AdmissionError("TORCHTITAN_GITCODE_SCOPE_ONLY")
        self.reader, self.coordinator, self.store = reader, coordinator, store
        self.read_authority, self.verify_event, self.clock = read_authority, verify_event, clock
        self.workspace = Path(workspace)
        if self.workspace.is_symlink():
            raise AdmissionError("ADMISSION_WORKSPACE_CANNOT_BE_SYMLINK")
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _now(self):
        now = self.clock()
        if type(now) is not int or now < 0:
            raise AdmissionError("INVALID_TRUSTED_CLOCK")
        return now

    def refresh(self, repo_id: str, number: int) -> int:
        if repo_id != self.reader.policy.gitcode_repo_id:
            raise AdmissionError("UNAUTHORIZED_GITCODE_SCOPE")

        def read():
            value = self.read_authority(repo_id, number)
            if not isinstance(value, Authority):
                raise AdmissionError("AUTHENTICATED_AUTHORITY_REQUIRED")
            return value

        return self.coordinator.reconcile(repo_id, number, read, now=self._now())

    def admit(self, run_id: int) -> AcceptedRequest | None:
        if not self.reader.policy.enabled:
            raise AdmissionError("GITCODE_PROVIDER_DISABLED")
        early = self.reader.early_intent(run_id, now=self._now())
        if early is None:
            return None
        intent, run = early
        pr = intent["pr"]
        self.refresh(pr["repo_id"], pr["number"])
        proof = self.verify_event(intent)
        if not isinstance(proof, EventProof):
            raise AdmissionError("AUTHENTICATED_EVENT_PROOF_REQUIRED")
        delivery = {
            "request_id": f"{self.reader.policy.github_repo_id}/{run_id}/1",
            "repo_id": self.reader.policy.github_repo_id,
            "run_id": run_id,
            "run_attempt": 1,
            "control_sha": self.reader.policy.control_sha,
        }
        self.coordinator.register_intent(intent, delivery, proof)
        # Early registration grants cancellation ownership, NOT resource access.
        with tempfile.TemporaryDirectory(prefix="request-", dir=self.workspace) as temporary:
            loaded = self.reader.load(run_id, Path(temporary) / "materialized", now=self._now())
            if loaded is None:
                return None
            if request_id(loaded.verified.plan) != delivery["request_id"]:
                raise AdmissionError("FINAL_REQUEST_DELIVERY_CHANGED")
            retained = self.store.save(loaded)
            # Download/staging may outlive authority freshness. Reconcile again
            # before the atomic claim; source/manifest identity alone is not a grant.
            self.refresh(pr["repo_id"], pr["number"])
            admission = self.coordinator.claim(retained, now=self._now())
        return AcceptedRequest(retained, admission)

    def resolve_request(self, rid: str) -> VerifiedRequest:
        """Outbox recovery reads immutable metadata even if Artifacts disappeared."""
        reference = self.coordinator.request_reference(rid)
        return self.store.load_plan(rid, reference["plan_digest"])

    def startable_source(self, accepted: AcceptedRequest) -> StoredExecution:
        """Revalidate before resource allocation; still not a device lease.

        Callers must repeat the generation/fence check after acquiring a shared
        physical lease and after all-node staging, immediately before launch.
        """
        if not isinstance(accepted, AcceptedRequest) or accepted.duplicate:
            raise AdmissionError("ONLY_CANONICAL_EXECUTION_MAY_START")
        plan = accepted.verified.plan
        reference = self.coordinator.request_reference(request_id(plan))
        if reference["receipt"] != accepted.admission.receipt:
            raise AdmissionError("PERSISTED_RECEIPT_MISMATCH")
        self.reader.actual_run(plan["github"]["run_id"])
        self.refresh(plan["pr"]["repo_id"], plan["pr"]["number"])
        self.coordinator.assert_startable(accepted.execution_id, now=self._now())
        return self.store.load_execution(request_id(plan), plan["plan_digest"])

    def reconcile_registered(self) -> dict:
        """Track dispatched/Prepare-stage scopes without requiring active Run lists."""
        refreshed, unavailable = [], []
        for repo_id, number in self.coordinator.registered_scopes():
            try:
                self.refresh(repo_id, number)
            except Exception as exc:
                unavailable.append({"repo_id": repo_id, "number": number, "error_type": type(exc).__name__})
            else:
                refreshed.append({"repo_id": repo_id, "number": number})
        return {"refreshed": refreshed, "unavailable": unavailable}
