"""Read schema-4 results by fixed IDs; never infer PASS from a Workflow alone.

The caller supplies a verified immutable request, an independently configured
Checks App channel, and a current-version authority callback. Network errors
propagate: cached success is not evidence of current Head/Base authorization.
"""

from __future__ import annotations

from collections.abc import Callable

from ci_core.gitcode_contract import (
    VerifiedRequest,
    aggregate_results,
    canonical,
    request_id,
    seal_plan,
    source_identity,
    validate_receipt,
)


class WaiterError(ValueError):
    pass


class GitCodeWaiter:
    def __init__(
        self,
        verified: VerifiedRequest,
        channel,
        *,
        current: Callable[[dict], bool],
        resolve_canonical: Callable[[dict], VerifiedRequest] | None = None,
    ):
        if not isinstance(verified, VerifiedRequest):
            raise WaiterError("VERIFIED_REQUEST_REQUIRED")
        self.plan = verified.plan
        if canonical(seal_plan(self.plan)) != canonical(self.plan):
            raise WaiterError("WAITER_PLAN_DIGEST_MISMATCH")
        channel._plan(self.plan)
        expected = {"aggregate"} | {c["case_id"] for c in self.plan["cases"]}
        if set(self.plan["result_slots"]) != expected:
            raise WaiterError("WAITER_COMPLETE_CASE_SLOTS_REQUIRED")
        self.channel = channel
        self.current = current
        self.resolve_canonical = resolve_canonical
        self.receipt: dict | None = None
        self.canonical_pointer: dict | None = None
        self.follower: GitCodeWaiter | None = None

    def _pin_receipt(self, envelope: dict) -> dict:
        receipt = envelope["receipt"]
        validate_receipt(self.plan, receipt)
        if self.receipt is not None and canonical(self.receipt) != canonical(receipt):
            raise WaiterError("WAITER_RECEIPT_CHANGED")
        if self.receipt is None:
            self.receipt = dict(receipt)
        return receipt

    def _follow(self, envelope: dict, *, now: int) -> dict:
        pointer = envelope.get("canonical")
        if self.resolve_canonical is None or not isinstance(pointer, dict):
            raise WaiterError("CANONICAL_RESOLVER_REQUIRED")
        if set(pointer) != {"request_id", "plan_digest", "github", "result_slots"}:
            raise WaiterError("INVALID_CANONICAL_POINTER")
        if pointer["request_id"] == request_id(self.plan):
            raise WaiterError("CANONICAL_POINTER_CYCLE")
        if self.canonical_pointer is not None and canonical(pointer) != canonical(
            self.canonical_pointer
        ):
            raise WaiterError("CANONICAL_POINTER_CHANGED")
        if self.follower is None:
            verified = self.resolve_canonical(pointer)
            if not isinstance(verified, VerifiedRequest):
                raise WaiterError("VERIFIED_CANONICAL_REQUEST_REQUIRED")
            plan = verified.plan
            expected = {
                "request_id": request_id(plan),
                "plan_digest": plan["plan_digest"],
                "github": plan["github"],
                "result_slots": plan["result_slots"],
            }
            if canonical(pointer) != canonical(expected):
                raise WaiterError("CANONICAL_POINTER_PLAN_MISMATCH")
            if (
                plan["request_spec_digest"] != self.plan["request_spec_digest"]
                or canonical(source_identity(plan))
                != canonical(source_identity(self.plan))
            ):
                raise WaiterError("CANONICAL_SPECIFICATION_MISMATCH")
            self.canonical_pointer = dict(pointer)
            # Deliberately do not pass a resolver: duplicate chains/cycles fail.
            self.follower = GitCodeWaiter(verified, self.channel, current=self.current)
        result = self.follower.poll(now=now)
        follower_receipt = result.get("receipt")
        if follower_receipt is not None and any(
            follower_receipt[k] != self.receipt[k]
            for k in (
                "canonical_execution_id",
                "pr_generation",
                "execution_generation",
                "publication_revision",
            )
        ):
            raise WaiterError("CANONICAL_EXECUTION_RECEIPT_MISMATCH")
        return {**result, "followed_from_request_id": request_id(self.plan)}

    def poll(self, *, now: int) -> dict:
        if type(now) is not int or now < 0:
            raise WaiterError("INVALID_WAITER_CLOCK")
        if now >= self.plan["deadline"]:
            raise WaiterError("WAITER_DEADLINE_EXCEEDED")
        envelope = self.channel.read_receipt(self.plan)
        if envelope is None:
            return {"test_result": "PENDING", "reason": "AWAITING_ADMISSION"}
        receipt = self._pin_receipt(envelope)
        if receipt["duplicate"]:
            return self._follow(envelope, now=now)
        results = []
        for case in self.plan["cases"]:
            result = self.channel.read_case(self.plan, receipt, case["case_id"])
            if result is not None:
                results.append(result)
        current = self.current(self.plan)
        if type(current) is not bool:
            raise WaiterError("AUTHORITY_BOOLEAN_REQUIRED")
        outcome = aggregate_results(self.plan, receipt, results, current=current)
        if envelope["phase"] != "AGGREGATE_RESULT":
            if outcome["test_result"] == "STALE":
                return outcome
            return {
                **outcome,
                "test_result": "PENDING",
                "reason": "AWAITING_AGGREGATE",
            }
        reported = envelope.get("outcome")
        if not isinstance(reported, dict):
            raise WaiterError("AGGREGATE_OUTCOME_REQUIRED")
        check = self.channel.read_slot(self.plan, "aggregate")
        confirmed = self.channel._payload(check)
        if canonical(confirmed) != canonical(envelope):
            raise WaiterError("AGGREGATE_CHANGED_DURING_READ")
        complete = len(results) == len(self.plan["cases"])
        if reported.get("test_result") == "PASS" and not complete:
            raise WaiterError("INCOMPLETE_CASE_SET_CANNOT_PASS")
        if current:
            if canonical(reported) != canonical(outcome):
                raise WaiterError("AGGREGATE_CASE_RESULTS_CONFLICT")
            if outcome["test_result"] != "PENDING":
                expected = "success" if outcome["test_result"] == "PASS" else "failure"
                if check.get("status") != "completed" or check.get("conclusion") != expected:
                    raise WaiterError("AGGREGATE_CONCLUSION_CONFLICT")
        else:
            # A historical green Check is immutable. Report STALE rather than
            # rewriting the old Check or calling it current merge permission.
            if (
                canonical(reported.get("identity"))
                != canonical(source_identity(self.plan))
                or canonical(reported.get("receipt")) != canonical(receipt)
                or reported.get("merge_gate") != "NOT_PROVIDED"
                or reported.get("merge_gate_mode") != "versioned_only"
            ):
                raise WaiterError("STALE_AGGREGATE_IDENTITY_CONFLICT")
        return outcome
