"""Recover schema-4 result delivery without re-running a model or listing comments.

Callbacks perform local cancellation and captured-lease cleanup; they must
return True only after the requested action is durably accepted. No default
no-op callback can acknowledge cancellation. The original Plan is retained by
the control-domain request store, including duplicate-delivery Plans.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ci_core.gitcode_contract import (
    VerifiedRequest,
    aggregate_results,
    canonical,
    request_id,
    seal_plan,
)


class DeliveryError(ValueError):
    pass


class GitCodeDelivery:
    def __init__(
        self,
        coordinator,
        *,
        resolve_request: Callable[[str], VerifiedRequest],
        channel_for: Callable[[dict], object],
        cancel_run: Callable[[dict], bool],
        cancel_execution: Callable[[dict], bool],
        cleanup_ack: Callable[[dict], bool],
    ):
        self.coordinator = coordinator
        self.resolve_request = resolve_request
        self.channel_for = channel_for
        self.cancel_run = cancel_run
        self.cancel_execution = cancel_execution
        self.cleanup_ack = cleanup_ack

    def _request(self, rid: str, expected_digest: str) -> dict:
        value = self.resolve_request(rid)
        if not isinstance(value, VerifiedRequest):
            raise DeliveryError("PERSISTED_VERIFIED_REQUEST_REQUIRED")
        plan = value.plan
        if (
            request_id(plan) != rid
            or plan["plan_digest"] != expected_digest
            or canonical(seal_plan(plan)) != canonical(plan)
        ):
            raise DeliveryError("PERSISTED_REQUEST_IDENTITY_MISMATCH")
        return plan

    def _deliver(self, record: dict) -> None:
        payload, kind = record["payload"], record["kind"]
        if kind in {"CANCEL_RUN", "CANCEL_EXECUTION", "LEASE_CLEANUP"}:
            callback = {
                "CANCEL_RUN": self.cancel_run,
                "CANCEL_EXECUTION": self.cancel_execution,
                "LEASE_CLEANUP": self.cleanup_ack,
            }[kind]
            if callback(payload) is not True:
                raise DeliveryError("DURABLE_ACTION_ACK_REQUIRED")
            return
        if kind == "ADMISSION_RECEIPT":
            receipt = payload["receipt"]
            plan = self._request(receipt["request_id"], receipt["plan_digest"])
            channel = self.channel_for(plan)
            existing = channel.read_receipt(plan)
            if existing is not None:
                if canonical(existing["receipt"]) != canonical(receipt):
                    raise DeliveryError("PUBLISHED_RECEIPT_CHANGED")
                if receipt["duplicate"] and canonical(
                    existing.get("canonical")
                ) != canonical(payload["canonical"]):
                    raise DeliveryError("PUBLISHED_CANONICAL_POINTER_CHANGED")
                # A crash after PATCH but before outbox acknowledgement may leave
                # an aggregate already published. Do not regress it to ADMITTED.
                return
            channel.publish_receipt(
                plan,
                receipt,
                canonical_execution=payload["canonical"]
                if receipt["duplicate"]
                else None,
            )
            return
        if kind == "CASE_RESULT":
            receipt = payload["receipt"]
            plan = self._request(receipt["request_id"], receipt["plan_digest"])
            channel = self.channel_for(plan)
            existing = channel.read_receipt(plan)
            if existing is None or canonical(existing["receipt"]) != canonical(receipt):
                raise DeliveryError("ADMISSION_RECEIPT_NOT_YET_PUBLISHED")
            channel.publish_case(plan, receipt, payload)
            return
        raise DeliveryError("UNKNOWN_OUTBOX_KIND")

    def flush(self, *, limit: int = 100) -> dict:
        records = self.coordinator.pending_outbox(limit)
        # Cleanup/cancellation must not sit behind a failing remote result API.
        records.sort(
            key=lambda row: (
                row["kind"] not in {"CANCEL_RUN", "CANCEL_EXECUTION", "LEASE_CLEANUP"},
                row["id"],
            )
        )
        delivered, pending = [], []
        for record in records:
            try:
                self._deliver(record)
                self.coordinator.acknowledge(record["id"], record["payload_digest"])
            except Exception as exc:
                pending.append(
                    {
                        "id": record["id"],
                        "kind": record["kind"],
                        "error_type": type(exc).__name__,
                    }
                )
                # Exception text could contain platform credentials or URLs.
                logging.warning(
                    "schema-4 outbox %s pending (%s)", record["id"], type(exc).__name__
                )
            else:
                delivered.append(record["id"])
        return {"delivered": delivered, "pending": pending}

    def publish_complete(
        self,
        verified: VerifiedRequest,
        receipt: dict,
        results: list[dict],
        *,
        current: Callable[[], bool],
    ) -> dict:
        """Publish only after every fixed Case slot is delivered.

        Results are loaded from the transactional case ledger by the caller.
        An already completed aggregate remains historical immutable evidence;
        authority is checked again before returning it as a current PASS.
        """
        if not isinstance(verified, VerifiedRequest):
            raise DeliveryError("VERIFIED_REQUEST_REQUIRED")
        plan = self._request(request_id(verified.plan), verified.plan["plan_digest"])
        if receipt.get("duplicate"):
            raise DeliveryError("DUPLICATE_CANNOT_PUBLISH_CANONICAL_AGGREGATE")
        expected = {c["case_id"] for c in plan["cases"]}
        if len(results) != len(expected) or {r["case_id"] for r in results} != expected:
            raise DeliveryError("COMPLETE_PERSISTED_CASE_SET_REQUIRED")
        channel = self.channel_for(plan)
        envelope = channel.read_receipt(plan)
        if envelope is None or canonical(envelope["receipt"]) != canonical(receipt):
            raise DeliveryError("ADMISSION_RECEIPT_NOT_YET_PUBLISHED")
        delivered = []
        for result in results:
            actual = channel.read_case(plan, receipt, result["case_id"])
            if canonical(actual) != canonical(result):
                raise DeliveryError("CASE_RESULT_NOT_YET_PUBLISHED")
            delivered.append(actual)
        fresh = current()
        if type(fresh) is not bool:
            raise DeliveryError("AUTHORITY_BOOLEAN_REQUIRED")
        outcome = aggregate_results(plan, receipt, delivered, current=fresh)
        if envelope["phase"] == "AGGREGATE_RESULT":
            old = envelope.get("outcome")
            if isinstance(old, dict) and old.get("test_result") == "PENDING":
                pending = aggregate_results(plan, receipt, [], current=True)
                check = channel.read_slot(plan, "aggregate")
                if (
                    canonical(old) != canonical(pending)
                    or check.get("status") != "in_progress"
                    or check.get("conclusion") is not None
                    or canonical(channel._payload(check)) != canonical(envelope)
                ):
                    raise DeliveryError("PENDING_AGGREGATE_IDENTITY_CONFLICT")
                channel.publish_aggregate(plan, receipt, delivered, current=fresh)
                return outcome
            if not isinstance(old, dict) or old.get("test_result") not in {
                "PASS",
                "FAIL",
                "STALE",
            }:
                raise DeliveryError("INVALID_TERMINAL_AGGREGATE")
            historical = aggregate_results(
                plan, receipt, delivered, current=old["test_result"] != "STALE"
            )
            if canonical(old) != canonical(historical):
                raise DeliveryError("TERMINAL_AGGREGATE_RESULT_CONFLICT")
            check = channel.read_slot(plan, "aggregate")
            if (
                check.get("status") != "completed"
                or check.get("conclusion")
                != ("success" if old["test_result"] == "PASS" else "failure")
                or canonical(channel._payload(check)) != canonical(envelope)
            ):
                raise DeliveryError("TERMINAL_AGGREGATE_API_CONFLICT")
            if old["test_result"] == "STALE":
                # A return to the same H/B never resurrects this revoked run.
                return old
            return outcome
        channel.publish_aggregate(plan, receipt, delivered, current=fresh)
        return outcome
