"""Independent fail-closed aggregate gate for the manual three-case workflow."""

from __future__ import annotations

import os

from .common import ManualForwardError, emit_failure

ALLOWED_REASONS = frozenset(
    {
        "OK",
        "SELECTED_CASE_FAILED",
        "GITHUB_RUN_NOT_SUCCESS",
        "PR_AUTHORITY_MISMATCH",
        "PR_AUTHORITY_UNAVAILABLE",
        "CONTROL_AUTHORITY_MISMATCH",
        "CONTROL_AUTHORITY_UNAVAILABLE",
        "CAPTURE_REVOKED",
    }
)


def evaluate(environment):
    required = (
        "CAPTURE_RESULT",
        "FORWARD_RESULT",
        "FORWARD_GATE",
        "FORWARD_GATE_REASON",
        "OS_RESULT",
        "EIGHT_P_RESULT",
        "SIXTEEN_P_RESULT",
    )
    values = {key: environment.get(key, "") for key in required}
    if values["CAPTURE_RESULT"] != "success":
        raise ManualForwardError("CAPTURE_FAILED")
    if values["FORWARD_RESULT"] != "success":
        raise ManualForwardError("FORWARD_FAILED_OR_MISSING_OUTPUT")
    gate, reason = values["FORWARD_GATE"], values["FORWARD_GATE_REASON"]
    if not reason or gate not in {"success", "failure"}:
        raise ManualForwardError("FORWARD_GATE_MISSING_OR_INVALID")
    if reason not in ALLOWED_REASONS:
        raise ManualForwardError("FORWARD_GATE_REASON_INVALID")
    if gate == "success" and reason != "OK":
        raise ManualForwardError("FORWARD_GATE_REASON_MISMATCH")
    if gate != "success":
        raise ManualForwardError(reason)
    for key, reason in (
        ("OS_RESULT", "OS_CASE_DISPLAY_FAILED"),
        ("EIGHT_P_RESULT", "8P_CASE_DISPLAY_FAILED"),
        ("SIXTEEN_P_RESULT", "16P_CASE_DISPLAY_FAILED"),
    ):
        if values[key] != "success":
            raise ManualForwardError(reason)
    return {
        "aggregate_status": "PASS",
        "historical_only": True,
        "current_authority": "NOT_PROVIDED",
        "merge_gate": "NOT_PROVIDED",
    }


def main(environment=None):
    import json

    try:
        result = evaluate(os.environ if environment is None else environment)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        return emit_failure(error)
