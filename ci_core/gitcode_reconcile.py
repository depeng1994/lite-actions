"""Trusted periodic/Base compensation; no webhook server or revision synthesis.

GitCode's documented PR list is paginated and not an atomic snapshot. Discovery
is only a wakeup. Existing registered scopes are refreshed independently first,
including closed PRs absent from discovery. A trusted intent provider must
durably reuse the original intent for retries and supply independently verified
event provenance; this module never constructs EventProof from timestamps,
random delivery IDs, page order, or current H/B equality.
"""

from __future__ import annotations

from ci_core.gitcode_contract import canonical, positive
from ci_core.gitcode_state import EventProof


class ReconcileError(ValueError):
    pass


class GitCodeCompensator:
    def __init__(self, *, client, broker, bridge, intent_for, clock):
        policy = broker.reader.policy
        if (
            client.repo_id != policy.gitcode_repo_id
            or bridge.personal.repo_id != client.repo_id
            or bridge.policy != policy
            or not callable(intent_for)
            or not callable(clock)
        ):
            raise ReconcileError("COMPENSATION_CONFIGURATION_MISMATCH")
        self.client, self.broker, self.bridge = client, broker, bridge
        self.intent_for, self.clock = intent_for, clock

    def tick(self) -> dict:
        """Read-only revocation first; retryable, independently isolated dispatch.

        intent_for(snapshot) returns an existing durable trusted intent, or None
        if already covered. It must NOT read intent code from the source tree.
        No network discovery failure is interpreted as an empty repository.
        """
        report = {
            "registered": self.broker.reconcile_registered(),
            "dispatched": [],
            "covered": [],
            "pending": [],
        }
        try:
            numbers = self.client.open_pull_numbers()
        except Exception as exc:
            report["pending"].append(
                {"stage": "discovery", "error_type": type(exc).__name__}
            )
            return report
        policy = self.broker.reader.policy
        for number in numbers:
            try:
                # Must establish authenticated version provenance before asking
                # for a new intent. The API snapshot alone grants nothing.
                self.broker.refresh(self.client.repo_id, number)
                snapshot = self.client.open_pull_snapshot(
                    number,
                    allowed_base_refs=policy.allowed_base_refs,
                    allowed_actors=policy.allowed_actors,
                )
                intent = self.intent_for(snapshot)
                if intent is None:
                    report["covered"].append(number)
                    continue
                if not isinstance(intent, dict) or canonical(
                    intent.get("pr")
                ) != canonical(snapshot["identity"]):
                    raise ReconcileError("COMPENSATION_INTENT_SNAPSHOT_MISMATCH")
                # Verify provenance before any external dispatch, not only at
                # later Blue admission. No integers from intent are trusted.
                proof = self.broker.verify_event(intent)
                if (
                    not isinstance(proof, EventProof)
                    or type(proof.authorized_rerun) is not bool
                ):
                    raise ReconcileError("COMPENSATION_EVENT_PROOF_REQUIRED")
                positive(proof.event_order)
                positive(proof.version_revision)
                bridge = intent.get("bridge", {})
                if proof.event_id != bridge.get(
                    "bridge_event_id"
                ) or proof.source_snapshot_id != bridge.get("source_snapshot_id"):
                    raise ReconcileError("COMPENSATION_EVENT_IDENTITY_MISMATCH")
                result = self.bridge.dispatch(intent, now=self.clock())
                report["dispatched"].append({"number": number, "delivery": result})
            except Exception as exc:
                # Never include API exception text, which may contain secrets.
                report["pending"].append(
                    {"number": number, "error_type": type(exc).__name__}
                )
        return report
