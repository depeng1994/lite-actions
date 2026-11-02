"""Manual-only API log capture. Never a proof of an original PR event.

The actual authorized GitHub dispatch principal authorizes this manual request;
GitCode provides an independently fetched, once-only wake/capture identity.
Neither the log's actor nor run.head_sha is promoted to GitCode actor or workflow
source-revision evidence. Enabling this contract is reviewed deployment policy.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from ci_core.gitcode_contract import (
    canonical,
    digest,
    hex_digest,
    positive,
    shape,
    text,
)
from ci_core.gitcode_source import require_sha

PREFIX = "LITE_GITCODE_MANUAL_CAPTURE_V1 "


class ManualCaptureError(ValueError):
    pass


class ManualCaptureRevoked(ManualCaptureError):
    pass


@dataclass(frozen=True)
class ManualCapturePolicy:
    control: object
    workflow_id: str
    workflow_path: str
    job_identifier: str
    step_identifier: str
    step_name: str
    script_sha256: str
    shell: str
    working_directory: str
    enabled: bool = False
    request_authority: str = "github_dispatch_operator"
    max_capture_age_seconds: int = 300
    clock_skew_seconds: int = 30
    allowed_run_statuses: tuple[str, ...] = ("RUNNING", "COMPLETED")

    def __post_init__(self):
        if self.request_authority != "github_dispatch_operator":
            raise ManualCaptureError("MANUAL_OPERATOR_AUTHORITY_REQUIRED")
        for value in (
            self.workflow_id,
            self.job_identifier,
            self.step_identifier,
            self.step_name,
        ):
            text(value, limit=160)
        if (
            not self.workflow_path.startswith(".gitcode/workflows/")
            or ".." in self.workflow_path
        ):
            raise ManualCaptureError("MANUAL_WORKFLOW_PATH_REQUIRED")
        hex_digest(self.script_sha256)
        if self.shell != "bash" or self.working_directory not in ("", "."):
            raise ManualCaptureError("MANUAL_ISOLATED_SHELL_REQUIRED")
        if (
            type(self.enabled) is not bool
            or type(self.clock_skew_seconds) is not int
            or not 0 <= self.clock_skew_seconds <= 60
        ):
            raise ManualCaptureError("MANUAL_POLICY_INVALID")
        positive(self.max_capture_age_seconds)
        if (
            self.max_capture_age_seconds > 900
            or not self.allowed_run_statuses
            or set(self.allowed_run_statuses) - {"RUNNING", "COMPLETED"}
        ):
            raise ManualCaptureError("MANUAL_POLICY_INVALID")
        require_sha(self.control.control_sha)

    @property
    def key_id(self):
        # Namespaces API evidence policy, NOT a signing key identifier.
        return "manual-api:" + digest(
            {k: v for k, v in self.__dict__.items() if k != "control"}
        )


def encode_capture(intent, captured_at):
    """Emission format only. This function grants no authentication or rights."""
    positive(captured_at)
    payload = canonical(
        {
            "schema": 1,
            "kind": "manual-current-observation",
            "captured_at": captured_at,
            "intent": intent,
        },
        limit=24 * 1024,
    )
    return (
        PREFIX
        + hashlib.sha256(payload).hexdigest()
        + " "
        + base64.b64encode(payload).decode()
    )


@dataclass(frozen=True)
class VerifiedManualCapture:
    capture_kind: str
    key_id: str
    issuer_repo_id: str
    source_key: str
    gitcode_run_id: str
    workflow_id: str
    workflow_path: str
    capture_job_id: str
    capture_step_id: str
    attempt: int
    actor: str
    captured_at: int
    request_started_at: int
    event_id: str
    envelope_digest: str
    intent: dict
    github_delivery: dict | None = None

    @property
    def original_event_at(self):
        # Shared ledger's historical column: manual request time only, not PR origin.
        return self.request_started_at

    @property
    def original_event_id(self):
        return self.event_id


# Exact API field names observed in b472's platform-owned pre/post steps.
PLATFORM_STEP_KEYS = frozenset(
    {
        "business_type",
        "condition",
        "continue-on-error",
        "daily_build_number",
        "end_time",
        "endpoint_ids",
        "env",
        "icon_url",
        "id",
        "identifier",
        "inputs",
        "job_run_id",
        "last_dispatch_id",
        "message",
        "multi_step_editable",
        "name",
        "official_task_version",
        "runtime_attribution",
        "sequence",
        "start_time",
        "status",
        "task",
        "timeout-minutes",
    }
)


def capture_user_step(job, policy):
    """Accept one user step, optionally inside the exact observed platform shell."""
    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) not in (1, 3):
        raise ManualCaptureError("MANUAL_CAPTURE_JOB_NOT_FIXED_COMPLETE")
    if len(steps) == 1:
        return steps[0]
    if len({step.get("id") for step in steps}) != 3:
        raise ManualCaptureError("MANUAL_PLATFORM_STEP_IDENTITY_MISMATCH")
    for index, task, name in (
        (0, "official_pre_step", "初始化步骤"),
        (2, "official_post_step", "后处理步骤"),
    ):
        step = steps[index]
        if (
            step.get("task") != task
            or step.get("name") != name
            or "identifier" not in step
            or step["identifier"] is not None
            or type(step.get("sequence")) is not int
            or step["sequence"] != index
            or step.get("status") != "COMPLETED"
            or step.get("job_run_id") != job["id"]
            or step.get("continue-on-error") not in (None, False)
            or set(step.get("metadata_keys", [])) != PLATFORM_STEP_KEYS
            or any(
                step.get(field + "_state") != "null"
                for field in ("inputs", "env", "condition")
            )
            or any(
                key in step for key in ("script_sha256", "shell", "working_directory")
            )
        ):
            raise ManualCaptureError("MANUAL_PLATFORM_STEP_ENVELOPE_MISMATCH")
    if type(steps[1].get("sequence")) is not int or steps[1]["sequence"] != 1:
        raise ManualCaptureError("MANUAL_USER_STEP_SEQUENCE_MISMATCH")
    return steps[1]


def verify_manual_capture(
    intent, policy, client, *, now, first_enrollment=True, allow_terminal=False
):
    if (
        policy is None
        or not policy.enabled
        or not policy.control.enabled
        or client.repo_id != policy.control.gitcode_repo_id
    ):
        raise ManualCaptureError("MANUAL_CAPTURE_NOT_ENABLED")
    if (
        intent.get("pipeline") != "smoke"
        or intent.get("bridge", {}).get("bridge_attempt") != 1
        or intent.get("bridge", {}).get("rerun_intent_id") != "initial"
        or not isinstance(intent.get("selectors"), list)
        or not intent["selectors"]
        or intent["selectors"] not in (
            [{"test_id": "worker_os_blue"}],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dispatch_smoke_16p"},
            ],
        )
    ):
        raise ManualCaptureError("MANUAL_BOUNDED_SMOKE_REQUEST_REQUIRED")
    run_id = text(intent.get("bridge", {}).get("bridge_action_run"), limit=100)
    run = client.action_run(run_id)
    expected = {
        "workflow_run_id": run_id,
        "workflow_id": policy.workflow_id,
        "file_path": policy.workflow_path,
        "head_sha": policy.control.control_sha,
        "head_branch": "master",
        "event": "Manual",
        "attempt": 1,
        "exist_in_default_branch": True,
    }
    if any(type(run.get(k)) is not type(v) or run[k] != v for k, v in expected.items()):
        raise ManualCaptureError("MANUAL_RUN_IDENTITY_MISMATCH")
    terminal = run.get("status") in {"FAILED", "CANCELED", "CANCELLED", "TIMED_OUT"}
    if terminal and not allow_terminal:
        raise ManualCaptureRevoked("MANUAL_RUN_TERMINALLY_REVOKED")
    if not terminal and run.get("status") not in policy.allowed_run_statuses:
        raise ManualCaptureError("MANUAL_RUN_STATUS_UNPROVEN")
    jobs = [
        j
        for j in client.action_jobs(run_id)
        if j["identifier"] == policy.job_identifier
    ]
    if len(jobs) != 1:
        raise ManualCaptureError("MANUAL_CAPTURE_JOB_AMBIGUOUS")
    job = client.action_job(run_id, jobs[0]["id"])
    steps = job.get("steps")
    if (
        job.get("identifier") != policy.job_identifier
        or job.get("status") != "COMPLETED"
        or job.get("is_shadow") is True
        or not isinstance(steps, list)
    ):
        raise ManualCaptureError("MANUAL_CAPTURE_JOB_NOT_FIXED_COMPLETE")
    step = capture_user_step(job, policy)
    required = {
        "identifier": policy.step_identifier,
        "name": policy.step_name,
        "job_run_id": job["id"],
        "status": "COMPLETED",
        "task": "official_shell",
        "script_sha256": policy.script_sha256,
        "shell": policy.shell,
        "working_directory": policy.working_directory,
    }
    if any(step.get(k) != v for k, v in required.items()) or step.get(
        "continue-on-error"
    ) not in (None, False):
        raise ManualCaptureError("MANUAL_CAPTURE_STEP_COMMAND_MISMATCH")
    start, end = step.get("start_time"), step.get("end_time")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0
        < run["start_time"]
        <= start
        <= end
        <= (now + policy.clock_skew_seconds) * 1000
    ):
        raise ManualCaptureError("MANUAL_CAPTURE_PLATFORM_TIME_INVALID")
    if first_enrollment and now - start // 1000 > policy.max_capture_age_seconds:
        raise ManualCaptureError("MANUAL_CAPTURE_TOO_OLD")
    logs = client.action_step_logs(run_id, job["id"], step["id"], max_bytes=128 * 1024)
    if logs.get("complete") is not True:
        raise ManualCaptureError("MANUAL_CAPTURE_LOG_INCOMPLETE")
    # Live v8 logs prefix output lines with exactly this timestamp framing.
    # Strip one anchored platform wrapper only; never search arbitrary suffixes.
    lines = [
        re.sub(
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}\] ",
            "",
            line,
            count=1,
        )
        for line in logs["log"].splitlines()
    ]
    matches = [line for line in lines if line.startswith(PREFIX)]
    if len(matches) != 1:
        raise ManualCaptureError("MANUAL_CAPTURE_LOG_MISSING_OR_DUPLICATE")
    match = re.fullmatch(
        re.escape(PREFIX) + r"([0-9a-f]{64}) ([A-Za-z0-9+/=]{1,32768})", matches[0]
    )
    if match is None:
        raise ManualCaptureError("MANUAL_CAPTURE_LOG_ENCODING_INVALID")
    try:
        raw = base64.b64decode(match[2], validate=True)
        from ci_core.gitcode_contract import load_json

        payload = load_json(raw, limit=24 * 1024)
    except (ValueError, TypeError):
        raise ManualCaptureError("MANUAL_CAPTURE_LOG_ENCODING_INVALID") from None
    shape(payload, {"schema", "kind", "captured_at", "intent"})
    if (
        raw != canonical(payload)
        or hashlib.sha256(raw).hexdigest() != match[1]
        or type(payload["schema"]) is not int
        or payload["schema"] != 1
        or payload["kind"] != "manual-current-observation"
        or canonical(payload["intent"]) != canonical(intent)
    ):
        raise ManualCaptureError("MANUAL_CAPTURE_INTENT_MISMATCH")
    at = payload["captured_at"]
    if (
        type(at) is not int
        or not start // 1000 - policy.clock_skew_seconds
        <= at
        <= end // 1000 + policy.clock_skew_seconds
        or intent["bridge"]["event_created_at"] != at
    ):
        raise ManualCaptureError("MANUAL_CAPTURE_TIMESTAMP_MISMATCH")
    # Re-read immutable completed evidence. No 'latest artifact' or mutable log fallback.
    again = client.action_step_logs(run_id, job["id"], step["id"], max_bytes=128 * 1024)
    if canonical(again) != canonical(logs) or canonical(
        client.action_job(run_id, job["id"])
    ) != canonical(job):
        raise ManualCaptureRevoked("MANUAL_CAPTURE_EVIDENCE_CHANGED")
    after = client.action_run(run_id)
    if any(after.get(k) != v for k, v in expected.items()):
        raise ManualCaptureRevoked("MANUAL_RUN_IDENTITY_CHANGED")
    if (
        after.get("status") in {"FAILED", "CANCELED", "CANCELLED", "TIMED_OUT"}
        and not allow_terminal
    ):
        raise ManualCaptureRevoked("MANUAL_RUN_TERMINALLY_REVOKED")
    if after.get("status") not in policy.allowed_run_statuses and not (
        allow_terminal
        and after.get("status") in {"FAILED", "CANCELED", "CANCELLED", "TIMED_OUT"}
    ):
        raise ManualCaptureError("MANUAL_RUN_STATUS_UNPROVEN")
    source_key = digest(
        {
            "repo": client.repo_id,
            "workflow": policy.workflow_id,
            "run": run_id,
            "job": job["id"],
            "attempt": 1,
            "pipeline": intent["pipeline"],
        }
    )
    return VerifiedManualCapture(
        "manual_api",
        policy.key_id,
        client.repo_id,
        source_key,
        run_id,
        policy.workflow_id,
        policy.workflow_path,
        job["id"],
        step["id"],
        1,
        intent["bridge"]["actor"],
        at,
        start // 1000,
        "manual:" + run_id,
        digest({"payload": payload, "job": job}),
        intent,
    )


def verify_retained_manual_capture(attested, policy, client):
    """Fresh revocation check of durable evidence, not new log authorization.

    Only the ledger owner supplies ``attested``. Unknown requests must use the
    full double-read enrollment path. Unread log mutations are not detected.
    """
    if (
        policy is None
        or not policy.enabled
        or not policy.control.enabled
        or client.repo_id != policy.control.gitcode_repo_id
        or attested.issuer_repo_id != client.repo_id
        or attested.key_id != policy.key_id
        or attested.capture_kind != "manual_api"
    ):
        raise ManualCaptureError("MANUAL_RETAINED_POLICY_MISMATCH")
    fresh = client.action_capture_snapshot(
        attested.gitcode_run_id, attested.capture_job_id
    )
    run, job = fresh["run"], fresh["job"]
    expected = {
        "workflow_run_id": attested.gitcode_run_id,
        "workflow_id": policy.workflow_id,
        "file_path": policy.workflow_path,
        "head_sha": policy.control.control_sha,
        "head_branch": "master",
        "event": "Manual",
        "attempt": 1,
        "exist_in_default_branch": True,
    }
    if any(type(run.get(k)) is not type(v) or run[k] != v for k, v in expected.items()):
        raise ManualCaptureRevoked("MANUAL_RUN_IDENTITY_CHANGED")
    if run.get("status") not in policy.allowed_run_statuses:
        raise ManualCaptureRevoked("MANUAL_RUN_NOT_CURRENT")
    payload = {
        "schema": 1,
        "kind": "manual-current-observation",
        "captured_at": attested.captured_at,
        "intent": attested.intent,
    }
    if (
        job.get("id") != attested.capture_job_id
        or digest({"payload": payload, "job": job}) != attested.envelope_digest
        or job.get("status") != "COMPLETED"
    ):
        raise ManualCaptureRevoked("MANUAL_IMMUTABLE_CAPTURE_CHANGED")
    return attested
