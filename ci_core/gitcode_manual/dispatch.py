"""Appended to common code; only this job receives the GH dispatch credential."""

import base64
import hashlib
import io
import json
import os
import re
import stat
import time
import urllib.parse
import zipfile

from .common import (
    ALLOWED_ACTOR,
    ALLOWED_CASES,
    API,
    CAPTURE_JOB,
    CAPTURE_NAME,
    CAPTURE_STEP,
    GH_ID,
    GH_REPO,
    GH_WORKFLOW,
    GH_WORKFLOW_ID,
    PREFIX,
    ManualForwardError,
    canonical,
    context,
    emit_failure,
    ident,
    release,
    require,
    run_identity,
    snapshot,
)

# Replaced by the deterministic workflow generator, not by workflow inputs.
CAPTURE_SCRIPT_SHA256 = os.environ.get("LITE_GITCODE_CAPTURE_SCRIPT_SHA256", "0" * 64)
if not re.fullmatch(r"[0-9a-f]{64}", CAPTURE_SCRIPT_SHA256):
    raise ManualForwardError("CAPTURE_BOOTSTRAP_DIGEST_REQUIRED")


PLATFORM_STEP_KEYS = {
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


def capture_user_step(steps, job_id):
    require(
        isinstance(steps, list) and len(steps) in (1, 3), "CAPTURE_JOB_NOT_SINGLE_STEP"
    )
    if len(steps) == 1:
        return steps[0]
    require(
        all(isinstance(step, dict) for step in steps), "CAPTURE_PLATFORM_STEP_INVALID"
    )
    require(
        len({ident(step.get("id")) for step in steps}) == 3,
        "CAPTURE_PLATFORM_STEP_IDS_DUPLICATE",
    )
    require(
        all(type(step.get("sequence")) is int for step in steps)
        and [step["sequence"] for step in steps] == [0, 1, 2],
        "CAPTURE_PLATFORM_STEP_ORDER",
    )
    for step, task, name in (
        (steps[0], "official_pre_step", "初始化步骤"),
        (steps[2], "official_post_step", "后处理步骤"),
    ):
        require(
            set(step) == PLATFORM_STEP_KEYS
            and {"identifier", "inputs", "env", "condition"}.issubset(step)
            and step.get("task") == task
            and step.get("name") == name
            and step.get("identifier") is None
            and step.get("job_run_id") == job_id
            and step.get("status") == "COMPLETED"
            and step.get("inputs") is None
            and step.get("env") is None
            and step.get("condition") is None
            and (
                step.get("continue-on-error") is None
                or step.get("continue-on-error") is False
            ),
            "CAPTURE_PLATFORM_STEP_INVALID",
        )
    return steps[1]


def fixed_capture_job(api, run_id):
    listing = api.gc("/actions/runs/" + run_id + "/jobs", version=8)
    jobs = listing.get("jobs")
    require(
        isinstance(jobs, list)
        and type(listing.get("total_count")) is int
        and listing["total_count"] == len(jobs) <= 100,
        "CAPTURE_JOB_LIST_INCOMPLETE",
    )
    matches = [job for job in jobs if job.get("identifier") == CAPTURE_JOB]
    require(len(matches) == 1, "CAPTURE_JOB_AMBIGUOUS")
    job_id = ident(matches[0].get("id"))
    job = api.gc("/actions/runs/" + run_id + "/jobs/" + job_id, version=8)
    require(
        job.get("id") == job_id
        and job.get("identifier") == CAPTURE_JOB
        and job.get("status") == "COMPLETED"
        and job.get("is_shadow") is not True,
        "CAPTURE_JOB_NOT_COMPLETE",
    )
    steps = job.get("steps")
    step = capture_user_step(steps, job_id)
    expected = {
        "identifier": CAPTURE_STEP,
        "name": CAPTURE_NAME,
        "job_run_id": job_id,
        "status": "COMPLETED",
        "task": "official_shell",
    }
    require(
        all(step.get(k) == v for k, v in expected.items())
        and (
            step.get("continue-on-error") is None
            or step.get("continue-on-error") is False
        ),
        "CAPTURE_STEP_IDENTITY_MISMATCH",
    )
    inputs = step.get("inputs")
    require(
        isinstance(inputs, list) and len(inputs) <= 100, "CAPTURE_COMMAND_PROOF_MISSING"
    )
    keys = [row.get("key") for row in inputs]
    require(
        all(isinstance(key, str) for key in keys) and len(keys) == len(set(keys)),
        "CAPTURE_COMMAND_DUPLICATE",
    )
    values = {row["key"]: row.get("value") for row in inputs}
    script = values.get("script")
    require(
        isinstance(script, str) and len(script.encode()) <= 128 * 1024,
        "CAPTURE_COMMAND_PROOF_MISSING",
    )
    require(
        hashlib.sha256(script.encode()).hexdigest() == CAPTURE_SCRIPT_SHA256
        and values.get("shell") == "bash"
        and values.get("working-directory") in ("", "."),
        "CAPTURE_COMMAND_NOT_PINNED",
    )
    return job, ident(step.get("id"))


def capture_log(api, run_id, job_id, step_id):
    offset, size, texts = 0, 0, []
    for _ in range(10):
        page = api.gc(
            "/actions/runs/" + run_id + "/jobs/" + job_id + "/logs",
            version=8,
            body={"step_id": step_id, "offset": offset, "limit": 1000, "sort": "asc"},
        )
        start, end, more, log = (
            page.get(k) for k in ("start_offset", "end_offset", "has_more", "log")
        )
        require(
            type(start) is int
            and type(end) is int
            and type(more) is bool
            and isinstance(log, str)
            and start == offset
            and start <= end
            and not (end == start and (more or log)),
            "CAPTURE_LOG_NOT_CONTIGUOUS",
        )
        size += len(log.encode())
        require(size <= 128 * 1024, "CAPTURE_LOG_TOO_LARGE")
        texts.append(log)
        offset = end
        if not more:
            return "".join(texts)
    raise ManualForwardError("CAPTURE_LOG_INCOMPLETE")


def retrieve_intent(api, environment, *, clock=time.time, historical=False):
    run_id, control = context(environment)
    run = run_identity(api, run_id, control)
    job, step_id = fixed_capture_job(api, run_id)
    log = capture_log(api, run_id, job["id"], step_id)
    # GitCode v8 adds exactly one timestamp wrapper to each output line.
    lines = [
        re.sub(
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}\] ",
            "",
            line,
            count=1,
        )
        for line in log.splitlines()
    ]
    lines = [line for line in lines if line.startswith(PREFIX)]
    require(len(lines) == 1, "CAPTURE_LOG_MISSING_OR_DUPLICATE")
    match = re.fullmatch(
        re.escape(PREFIX) + r"([0-9a-f]{64}) ([A-Za-z0-9+/=]{1,32768})", lines[0]
    )
    require(match is not None, "CAPTURE_LOG_INVALID")
    raw = base64.b64decode(match[2], validate=True)
    require(
        len(raw) <= 24 * 1024 and hashlib.sha256(raw).hexdigest() == match[1],
        "CAPTURE_DIGEST_MISMATCH",
    )
    payload = json.loads(raw)
    require(
        set(payload) == {"schema", "kind", "captured_at", "intent"}
        and payload["schema"] == 1
        and payload["kind"] == "manual-current-observation"
        and canonical(payload) == raw,
        "CAPTURE_NOT_CANONICAL",
    )
    intent = payload["intent"]
    require(
        set(intent)
        == {
            "schema",
            "source_provider",
            "pr",
            "control_repo_id",
            "control_sha",
            "workflow_path",
            "pipeline",
            "selectors",
            "bridge",
            "deadline",
        },
        "CAPTURE_INTENT_SHAPE",
    )
    require(
        intent["schema"] == 1
        and intent["source_provider"] == "gitcode"
        and intent["control_repo_id"] == GH_ID
        and intent["control_sha"] == control
        and intent["workflow_path"] == GH_WORKFLOW
        and intent["pipeline"] == "smoke",
        "CAPTURE_CONTROL_MISMATCH",
    )
    require(
        intent["selectors"]
        in (
            [{"test_id": "worker_os_blue"}],
            [{"test_id": name} for name in ALLOWED_CASES[:2]],
            [{"test_id": name} for name in ALLOWED_CASES],
        ),
        "CAPTURE_CASE_NOT_ALLOWED",
    )
    bridge = intent["bridge"]
    require(
        set(bridge)
        == {
            "bridge_event_id",
            "bridge_action_run",
            "bridge_attempt",
            "actor",
            "bridge_nonce",
            "source_snapshot_id",
            "dispatch_intent_id",
            "rerun_intent_id",
            "event_created_at",
        },
        "CAPTURE_BRIDGE_SHAPE",
    )
    nonce = bridge["bridge_nonce"]
    require(
        isinstance(nonce, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,100}", nonce),
        "CAPTURE_NONCE_INVALID",
    )
    require(
        bridge["bridge_action_run"] == run_id
        and bridge["bridge_attempt"] == 1
        and bridge["actor"] == ALLOWED_ACTOR
        and bridge["rerun_intent_id"] == "initial"
        and bridge["bridge_event_id"] == "manual:" + run_id
        and bridge["source_snapshot_id"] == "manual-capture:" + nonce
        and bridge["dispatch_intent_id"] == "manual-intent:" + nonce,
        "CAPTURE_BRIDGE_MISMATCH",
    )
    at = payload["captured_at"]
    step = capture_user_step(job["steps"], job["id"])
    start, end, run_start = (
        step.get("start_time"),
        step.get("end_time"),
        run.get("start_time"),
    )
    require(
        type(at) is int
        and all(type(t) is int for t in (start, end, run_start))
        and 0 < run_start <= start <= end <= (int(clock()) + 30) * 1000,
        "CAPTURE_PLATFORM_TIME_INVALID",
    )
    require(
        start // 1000 - 30 <= at <= end // 1000 + 30
        and at <= int(clock()) + 30
        and (historical or int(clock()) - at <= 300),
        "CAPTURE_AGE_INVALID",
    )
    require(
        bridge["event_created_at"] == at
        and intent["deadline"] == at + 900
        and (historical or int(clock()) < intent["deadline"]),
        "CAPTURE_DEADLINE_INVALID",
    )
    again, again_id = fixed_capture_job(api, run_id)
    require(
        again == job
        and again_id == step_id
        and capture_log(api, run_id, job["id"], step_id) == log,
        "CAPTURE_EVIDENCE_CHANGED",
    )
    run_identity(api, run_id, control)
    if not historical:
        require(
            snapshot(api, intent["pr"]["number"]) == intent["pr"], "CAPTURE_PR_STALE"
        )
        release(api, control)
        require(
            int(clock()) - at <= 300 and int(clock()) < intent["deadline"],
            "CAPTURE_EXPIRED_DURING_READ",
        )
    return intent


def validate_github_run(run, intent, *, allow_initial_title_pending=False):
    require(type(run.get("id")) is int and run["id"] > 0, "GITHUB_RUN_ID_INVALID")
    expected = {
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_sha": intent["control_sha"],
        "head_branch": "master",
        "workflow_id": GH_WORKFLOW_ID,
    }
    for field, expected_value in expected.items():
        # Field names come only from this fixed mapping; never print actual values.
        require(
            field in run,
            "GITHUB_RUN_MISSING_" + field.upper(),
        )
        require(
            type(run[field]) is type(expected_value),
            "GITHUB_RUN_TYPE_MISMATCH_" + field.upper(),
        )
        require(
            run[field] == expected_value,
            "GITHUB_RUN_VALUE_MISMATCH_" + field.upper(),
        )
    require(
        (run.get("repository") or {}).get("id") == GH_ID
        and (run.get("repository") or {}).get("full_name") == GH_REPO
        and (run.get("actor") or {}).get("login") == ALLOWED_ACTOR
        and run.get("path") in (GH_WORKFLOW, GH_WORKFLOW + "@master"),
        "GITHUB_RUN_OWNER_MISMATCH",
    )
    require("display_title" in run, "GITHUB_RUN_MISSING_DISPLAY_TITLE")
    title = run["display_title"]
    require(type(title) is str, "GITHUB_RUN_TYPE_MISMATCH_DISPLAY_TITLE")
    expected_title = "lite-gitcode:" + intent["bridge"]["bridge_nonce"]
    if title != expected_title:
        if (
            allow_initial_title_pending
            and len(title) <= 200
            and not any(ord(char) < 32 or ord(char) == 127 for char in title)
            and not title.startswith("lite-gitcode:")
            and run.get("status") in {"queued", "in_progress"}
        ):
            return None
        raise ManualForwardError("GITHUB_RUN_VALUE_MISMATCH_DISPLAY_TITLE")
    return run["id"]


def bind_initial_run(api, intent, run_id, *, clock, sleep, progress):
    # Only a validated response from THIS dispatch grants this narrow pending path.
    expires = min(
        int(clock()) + 30,
        intent["deadline"],
        intent["bridge"]["event_created_at"] + 300,
    )
    for attempt in range(1, 7):
        require(int(clock()) < expires, "GITHUB_INITIAL_TITLE_TIMEOUT")
        run_identity(api, intent["bridge"]["bridge_action_run"], intent["control_sha"])
        require(int(clock()) < expires, "GITHUB_INITIAL_TITLE_TIMEOUT")
        run = api.gh(f"/actions/runs/{run_id}")
        require(int(clock()) < expires, "GITHUB_INITIAL_TITLE_TIMEOUT")
        require(
            run.get("id") == run_id and type(run.get("id")) is int,
            "GITHUB_RUN_RESPONSE_ID_MISMATCH",
        )
        if (
            validate_github_run(run, intent, allow_initial_title_pending=True)
            is not None
        ):
            return run
        progress({"event": "INITIAL_TITLE_PENDING", "attempt": attempt})
        if attempt < 6:
            sleep(min(5, max(0, expires - int(clock()))))
    raise ManualForwardError("GITHUB_INITIAL_TITLE_TIMEOUT")


def find_run(api, intent):
    query = urllib.parse.urlencode(
        {
            "event": "workflow_dispatch",
            "branch": "master",
            "head_sha": intent["control_sha"],
            "actor": ALLOWED_ACTOR,
            "per_page": 100,
            "page": 1,
        }
    )
    result = api.gh(f"/actions/workflows/{GH_WORKFLOW_ID}/runs?" + query)
    rows = result.get("workflow_runs")
    require(
        isinstance(rows, list)
        and type(result.get("total_count")) is int
        and result["total_count"] == len(rows) <= 100,
        "GITHUB_RECOVERY_SCOPE_INCOMPLETE",
    )
    matches = [
        row
        for row in rows
        if row.get("display_title")
        == "lite-gitcode:" + intent["bridge"]["bridge_nonce"]
    ]
    require(len(matches) <= 1, "GITHUB_NONCE_AMBIGUOUS")
    return validate_github_run(matches[0], intent) if matches else None


CASE_ORDER = {
    "worker_os_blue": "case_000_worker_os_blue",
    "dispatch_smoke_8p": "case_001_dispatch_smoke_8p",
    "dispatch_smoke_16p": "case_002_dispatch_smoke_16p",
}


def strict_json(raw, *, limit):
    require(isinstance(raw, bytes) and len(raw) <= limit, "PLAN_JSON_SIZE_LIMIT")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "PLAN_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, RecursionError, UnicodeError):
        raise ManualForwardError("PLAN_JSON_INVALID") from None
    require(
        isinstance(value, dict) and canonical(value) == raw, "PLAN_JSON_NOT_CANONICAL"
    )
    return value


def read_plan_artifact(api, intent, run_id, *, expected_source_plan=None):
    listing = api.gh(
        f"/actions/runs/{run_id}/artifacts?name=lite-ci-request-1&per_page=10&page=1",
        authenticated_read=True,
    )
    rows = listing.get("artifacts") if isinstance(listing, dict) else None
    require(
        isinstance(rows, list)
        and type(listing.get("total_count")) is int
        and listing["total_count"] == len(rows) == 1,
        "GITHUB_PLAN_ARTIFACT_MISSING_OR_AMBIGUOUS",
    )
    artifact_id = rows[0].get("id")
    require(
        type(artifact_id) is int
        and artifact_id > 0
        and rows[0].get("name") == "lite-ci-request-1",
        "GITHUB_PLAN_ARTIFACT_IDENTITY_INVALID",
    )
    metadata = api.gh(f"/actions/artifacts/{artifact_id}", authenticated_read=True)
    owner = metadata.get("workflow_run") or {}
    digest_value = metadata.get("digest")
    require(
        metadata.get("id") == artifact_id
        and metadata.get("name") == "lite-ci-request-1"
        and metadata.get("expired") is False
        and type(metadata.get("size_in_bytes")) is int
        and 0 < metadata["size_in_bytes"] <= 256 * 1024
        and type(owner.get("id")) is int
        and owner["id"] == run_id
        and type(owner.get("repository_id")) is int
        and owner["repository_id"] == GH_ID
        and owner.get("head_sha") == intent["control_sha"]
        and owner.get("head_branch") == "master"
        and isinstance(digest_value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value),
        "GITHUB_PLAN_ARTIFACT_OWNER_OR_DIGEST_MISMATCH",
    )
    archive = api.download_authenticated_artifact(artifact_id)
    require(
        len(archive) == metadata["size_in_bytes"]
        and hashlib.sha256(archive).hexdigest() == digest_value[7:],
        "GITHUB_PLAN_ARTIFACT_ZIP_DIGEST_MISMATCH",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            members = zf.infolist()
            require(
                len(members) == 1 and members[0].filename == "ci-request.json",
                "GITHUB_PLAN_ARTIFACT_MEMBERS_INVALID",
            )
            info = members[0]
            mode = info.external_attr >> 16
            require(
                not info.is_dir()
                and not (info.flag_bits & 1)
                and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                and 0 < info.file_size <= 256 * 1024,
                "GITHUB_PLAN_ARTIFACT_MEMBER_UNSAFE",
            )
            raw = zf.read(info)
    except (zipfile.BadZipFile, EOFError, RuntimeError):
        raise ManualForwardError("GITHUB_PLAN_ARTIFACT_ZIP_INVALID") from None
    plan = strict_json(raw, limit=256 * 1024)
    unsigned = dict(plan)
    digest_plan = unsigned.pop("plan_digest", None)
    require(
        isinstance(digest_plan, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest_plan)
        and hashlib.sha256(canonical(unsigned)).hexdigest() == digest_plan,
        "IMMUTABLE_PLAN_DIGEST_MISMATCH",
    )
    gh, control = plan.get("github") or {}, plan.get("control") or {}
    jobs_response = api.gh(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = jobs_response.get("jobs") if isinstance(jobs_response, dict) else None
    require(
        isinstance(jobs, list)
        and type(jobs_response.get("total_count")) is int
        and jobs_response["total_count"] == len(jobs) <= 100,
        "GITHUB_PREPARE_JOB_LIST_INCOMPLETE",
    )
    prepare_jobs = [job for job in jobs if job.get("name") == "prepare-gitcode"]
    require(
        len(prepare_jobs) == 1
        and prepare_jobs[0].get("status") == "completed"
        and prepare_jobs[0].get("conclusion") == "success"
        and prepare_jobs[0].get("run_id") == run_id
        and prepare_jobs[0].get("run_attempt") == 1,
        "GITHUB_PREPARE_JOB_NOT_SUCCESSFUL",
    )
    common_identity = (
        plan.get("schema") == 4
        and plan.get("source_provider") == "gitcode"
        and plan.get("test_mode") == "merge_result"
        and gh.get("repo") == GH_REPO
        and gh.get("repo_id") == GH_ID
        and gh.get("run_id") == run_id
        and gh.get("run_attempt") == 1
        and gh.get("prepare_job_id") == prepare_jobs[0].get("id")
        and gh.get("workflow_id") == GH_WORKFLOW_ID
        and gh.get("workflow_path") == GH_WORKFLOW
        and control.get("control_sha") == intent["control_sha"]
        and plan.get("pr") == intent["pr"]
        and plan.get("pipeline") == intent["pipeline"]
    )
    if expected_source_plan is None:
        request_binding = (
            plan.get("bridge") == intent["bridge"]
            and plan.get("deadline") == intent["deadline"]
        )
    else:
        request_binding = (
            plan.get("request_spec_digest")
            == expected_source_plan.get("request_spec_digest")
            and plan.get("pr") == expected_source_plan.get("pr")
            and plan.get("merge") == expected_source_plan.get("merge")
            and plan.get("control") == expected_source_plan.get("control")
            and plan.get("cases") == expected_source_plan.get("cases")
            and plan.get("pipeline") == expected_source_plan.get("pipeline")
            and plan.get("policy_version") == expected_source_plan.get("policy_version")
            and plan.get("environment_version")
            == expected_source_plan.get("environment_version")
            and isinstance(plan.get("bridge"), dict)
            and set(plan["bridge"])
            == {
                "bridge_event_id",
                "bridge_action_run",
                "bridge_attempt",
                "actor",
                "bridge_nonce",
                "source_snapshot_id",
                "dispatch_intent_id",
                "rerun_intent_id",
                "event_created_at",
            }
            and plan["bridge"].get("bridge_event_id")
            == "manual:" + str(plan["bridge"].get("bridge_action_run"))
            and isinstance(plan["bridge"].get("bridge_action_run"), str)
            and re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", plan["bridge"]["bridge_action_run"]
            )
            and plan["bridge"].get("bridge_attempt") == 1
            and plan["bridge"].get("actor") == ALLOWED_ACTOR
            and isinstance(plan["bridge"].get("bridge_nonce"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,100}", plan["bridge"]["bridge_nonce"])
            and plan["bridge"].get("source_snapshot_id")
            == "manual-capture:" + plan["bridge"]["bridge_nonce"]
            and plan["bridge"].get("dispatch_intent_id")
            == "manual-intent:" + plan["bridge"]["bridge_nonce"]
            and plan["bridge"].get("rerun_intent_id") == "initial"
            and type(plan["bridge"].get("event_created_at")) is int
            and type(plan.get("deadline")) is int
            and plan["deadline"] > plan["bridge"]["event_created_at"]
        )
    require(
        common_identity and request_binding, "IMMUTABLE_PLAN_RUN_OR_CAPTURE_MISMATCH"
    )
    spec_payload = {
        "pr": plan["pr"],
        "merge": plan["merge"],
        "control": plan["control"],
        "workflow_id": gh["workflow_id"],
        "workflow_path": gh["workflow_path"],
        "pipeline": plan["pipeline"],
        "policy_version": plan["policy_version"],
        "environment_version": plan["environment_version"],
        "cases": plan["cases"],
    }
    if "authority_protocol" in plan:
        spec_payload["authority_protocol"] = plan["authority_protocol"]
    require(
        isinstance(plan.get("request_spec_digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", plan["request_spec_digest"])
        and hashlib.sha256(canonical(spec_payload)).hexdigest()
        == plan["request_spec_digest"],
        "IMMUTABLE_PLAN_SPEC_DIGEST_MISMATCH",
    )
    expected_cases = [CASE_ORDER[row["test_id"]] for row in intent["selectors"]]
    cases = plan.get("cases")
    require(
        isinstance(cases, list)
        and [row.get("case_id") for row in cases] == expected_cases
        and len({row.get("case_id") for row in cases}) == len(cases),
        "IMMUTABLE_PLAN_CASE_SET_MISMATCH",
    )
    expected_slots = {"aggregate", *expected_cases}
    if plan.get("authority_protocol") == "artifact-check-v1":
        expected_slots.add("authority")
    require(
        isinstance(plan.get("result_slots"), dict)
        and set(plan["result_slots"]) == expected_slots,
        "IMMUTABLE_PLAN_RESULT_SLOTS_INVALID",
    )
    slot_ids = [
        slot.get("id")
        for slot in plan["result_slots"].values()
        if isinstance(slot, dict)
    ]
    require(
        len(slot_ids) == len(expected_slots)
        and all(type(value) is int and value > 0 for value in slot_ids)
        and len(set(slot_ids)) == len(slot_ids),
        "IMMUTABLE_PLAN_RESULT_SLOT_IDS_INVALID",
    )
    return plan


def expected_slot(plan, case_id):
    gh = plan["github"]
    external = (
        "lite-actions:v4:"
        + hashlib.sha256(
            canonical(
                {
                    "repo_id": GH_ID,
                    "run_id": gh["run_id"],
                    "run_attempt": gh["run_attempt"],
                    "case_id": case_id,
                    "request_spec_digest": plan["request_spec_digest"],
                }
            )
        ).hexdigest()
    )
    return {
        "app_id": 5575236,
        "repo_id": GH_ID,
        "control_sha": plan["control"]["control_sha"],
        "run_id": gh["run_id"],
        "run_attempt": gh["run_attempt"],
        "case_id": case_id,
        "external_id": external,
    }


def read_fixed_check(api, plan, case_id):
    slot = plan["result_slots"].get(case_id)
    require(
        isinstance(slot, dict) and type(slot.get("id")) is int and slot["id"] > 0,
        "IMMUTABLE_PLAN_CHECK_ID_MISSING",
    )
    expected = expected_slot(plan, case_id)
    require(
        set(slot) == {"id", *expected}
        and all(slot.get(k) == v for k, v in expected.items()),
        "IMMUTABLE_PLAN_CHECK_SLOT_MISMATCH",
    )
    check = api.gh(f"/check-runs/{slot['id']}", authenticated_read=True)
    run_id = plan["github"]["run_id"]
    require(
        type(check.get("id")) is int
        and check["id"] == slot["id"]
        and check.get("name")
        == f"lite-v4-{run_id}-1-{case_id}-{plan['request_spec_digest'][:12]}"
        and check.get("head_sha") == plan["control"]["control_sha"]
        and (check.get("app") or {}).get("id") == 5575236
        and check.get("external_id") == expected["external_id"]
        and check.get("details_url")
        == f"https://github.com/{GH_REPO}/actions/runs/{run_id}"
        and check.get("url")
        == f"https://api.github.com/repos/{GH_REPO}/check-runs/{slot['id']}",
        "FIXED_CHECK_FULL_IDENTITY_MISMATCH",
    )
    return check


def read_admission_record(api, plan, intent):
    check = read_fixed_check(api, plan, "aggregate")
    summary = (check.get("output") or {}).get("summary")
    envelope = (
        strict_json(summary.encode(), limit=32 * 1024)
        if isinstance(summary, str)
        else None
    )
    require(
        isinstance(envelope, dict) and envelope.get("schema") == 4,
        "ADMISSION_CHECK_ENVELOPE_INVALID",
    )
    phase = envelope.get("phase")
    if phase == "SLOT_CREATED":
        expected_created = {
            "schema": 4,
            "phase": "SLOT_CREATED",
            "request_id": f"{GH_ID}/{plan['github']['run_id']}/1",
            "case_id": "aggregate",
            "request_spec_digest": plan["request_spec_digest"],
        }
        require(
            canonical(envelope) == canonical(expected_created)
            and check.get("status") in {"queued", "in_progress"}
            and check.get("conclusion") is None,
            "ADMISSION_SLOT_CREATED_ENVELOPE_INVALID",
        )
        return None, None
    require(
        phase in {"ADMITTED", "AGGREGATE_RESULT"}
        and envelope.get("plan_digest") == plan["plan_digest"],
        "ADMISSION_CHECK_ENVELOPE_INVALID",
    )
    receipt = envelope.get("receipt")
    expected = {
        "request_id": f"{GH_ID}/{plan['github']['run_id']}/1",
        "plan_digest": plan["plan_digest"],
        "request_spec_digest": plan["request_spec_digest"],
        "source_snapshot_id": intent["bridge"]["source_snapshot_id"],
        "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
    }
    require(
        isinstance(receipt, dict)
        and set(receipt)
        == {
            "request_id",
            "plan_digest",
            "request_spec_digest",
            "source_snapshot_id",
            "dispatch_intent_id",
            "canonical_execution_id",
            "pr_generation",
            "execution_generation",
            "publication_revision",
            "duplicate",
        }
        and all(receipt.get(k) == v for k, v in expected.items())
        and isinstance(receipt.get("canonical_execution_id"), str)
        and bool(receipt["canonical_execution_id"])
        and all(
            type(receipt.get(k)) is int and receipt[k] > 0
            for k in ("pr_generation", "execution_generation", "publication_revision")
        )
        and type(receipt.get("duplicate")) is bool,
        "ADMISSION_RECEIPT_IDENTITY_INVALID",
    )
    pointer = envelope.get("canonical")
    if receipt["duplicate"]:
        require(
            isinstance(pointer, dict)
            and set(pointer) == {"request_id", "plan_digest", "github", "result_slots"}
            and pointer["request_id"] != receipt["request_id"]
            and isinstance(pointer["plan_digest"], str)
            and re.fullmatch(r"[0-9a-f]{64}", pointer["plan_digest"])
            and isinstance(pointer["github"], dict)
            and pointer["github"].get("repo") == GH_REPO
            and pointer["github"].get("repo_id") == GH_ID
            and type(pointer["github"].get("run_id")) is int
            and pointer["github"]["run_id"] > 0
            and pointer["github"].get("run_attempt") == 1
            and isinstance(pointer["result_slots"], dict),
            "ADMISSION_CANONICAL_POINTER_INVALID",
        )
    else:
        require(pointer is None, "ADMISSION_UNEXPECTED_CANONICAL_POINTER")
    require(
        (check.get("status") == "in_progress" and check.get("conclusion") is None)
        or (
            phase == "AGGREGATE_RESULT"
            and check.get("status") == "completed"
            and check.get("conclusion")
            == (
                "success"
                if (envelope.get("outcome") or {}).get("test_result") == "PASS"
                else "failure"
            )
        ),
        "ADMISSION_CHECK_CONCLUSION_INVALID",
    )
    return receipt, pointer


def read_admission_receipt(api, plan, intent):
    receipt, _ = read_admission_record(api, plan, intent)
    return receipt


def resolve_canonical_execution(
    api, intent, duplicate_plan, duplicate_receipt, pointer
):
    run_id = pointer["github"]["run_id"]
    canonical_plan = read_plan_artifact(
        api, intent, run_id, expected_source_plan=duplicate_plan
    )
    expected_pointer = {
        "request_id": f"{GH_ID}/{run_id}/1",
        "plan_digest": canonical_plan["plan_digest"],
        "github": canonical_plan["github"],
        "result_slots": canonical_plan["result_slots"],
    }
    require(
        canonical(expected_pointer) == canonical(pointer)
        and canonical_plan["request_spec_digest"]
        == duplicate_plan["request_spec_digest"]
        and canonical_plan["pr"] == duplicate_plan["pr"]
        and canonical_plan["merge"] == duplicate_plan["merge"]
        and canonical_plan["control"] == duplicate_plan["control"],
        "ADMISSION_CANONICAL_PLAN_POINTER_MISMATCH",
    )
    canonical_intent = dict(intent)
    canonical_intent.update(
        pr=canonical_plan["pr"],
        bridge=canonical_plan["bridge"],
        pipeline=canonical_plan["pipeline"],
        deadline=canonical_plan["deadline"],
    )
    canonical_run = api.gh(f"/actions/runs/{run_id}")
    require(
        canonical_run.get("id") == run_id
        and type(canonical_run.get("id")) is int
        and canonical_run.get("status") == "completed"
        and canonical_run.get("conclusion")
        in {"success", "failure", "cancelled", "timed_out", "action_required"},
        "ADMISSION_CANONICAL_RUN_NOT_TERMINAL",
    )
    validate_github_run(canonical_run, canonical_intent)
    jobs_response = api.gh(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = jobs_response.get("jobs") if isinstance(jobs_response, dict) else None
    require(
        isinstance(jobs, list)
        and type(jobs_response.get("total_count")) is int
        and jobs_response["total_count"] == len(jobs) <= 100,
        "ADMISSION_CANONICAL_JOB_LIST_INCOMPLETE",
    )
    for name in ("prepare-gitcode", "wait-gitcode", "Lite-Actions-GitCode-Smoke"):
        matches = [job for job in jobs if job.get("name") == name]
        require(
            len(matches) == 1
            and matches[0].get("run_id") == run_id
            and matches[0].get("run_attempt") == 1
            and matches[0].get("status") == "completed"
            and matches[0].get("conclusion")
            in {"success", "failure", "cancelled", "timed_out", "action_required"},
            "ADMISSION_CANONICAL_JOB_IDENTITY_INVALID",
        )
    canonical_receipt, canonical_pointer = read_admission_record(
        api, canonical_plan, canonical_intent
    )
    require(
        canonical_receipt is not None
        and canonical_receipt["duplicate"] is False
        and canonical_pointer is None
        and canonical_receipt["canonical_execution_id"]
        == duplicate_receipt["canonical_execution_id"]
        and canonical_receipt["pr_generation"] == duplicate_receipt["pr_generation"]
        and canonical_receipt["execution_generation"]
        == duplicate_receipt["execution_generation"]
        and canonical_receipt["publication_revision"]
        == duplicate_receipt["publication_revision"]
        and canonical_receipt["request_spec_digest"]
        == duplicate_receipt["request_spec_digest"],
        "ADMISSION_CANONICAL_RECEIPT_GENERATION_MISMATCH",
    )
    confirmed_receipt, confirmed_pointer = read_admission_record(
        api, duplicate_plan, intent
    )
    require(
        canonical(confirmed_receipt) == canonical(duplicate_receipt)
        and canonical(confirmed_pointer) == canonical(pointer),
        "ADMISSION_CANONICAL_POINTER_CHANGED",
    )
    return canonical_plan, canonical_receipt, canonical_intent, canonical_run


def current_control_matches(api, control):
    try:
        release(api, control)
        return True
    except ManualForwardError as error:
        if str(error) in {
            "GITCODE_CONTROL_CHANGED",
            "GITHUB_CONTROL_CHANGED",
            "GITHUB_WORKFLOW_NOT_APPROVED",
            "GITHUB_NUMERIC_REPOSITORY_CHANGED",
        }:
            return False
        raise


def current_pr_matches(api, intent):
    number = intent["pr"]["number"]
    row = api.gc(f"/pulls/{number}")
    if row.get("state") != "open" or row.get("merged_at"):
        return False
    try:
        return snapshot(api, number) == intent["pr"]
    except ManualForwardError as error:
        if str(error) in {
            "PR_NOT_OPEN",
            "PR_TARGET_NOT_MASTER",
            "PERSONAL_SOURCE_TARGET_ONLY",
            "PR_CHANGED_DURING_CAPTURE",
            "PR_AUTHOR_NOT_ALLOWED",
        }:
            return False
        raise


def case_check(
    api,
    intent,
    plan,
    receipt,
    run_id,
    test_id,
    *,
    run_conclusion="failure",
    pr_matches=False,
):
    require(test_id in CASE_ORDER, "CASE_NOT_IN_FIXED_RESULT_SET")
    case_id = CASE_ORDER[test_id]
    if case_id not in plan["result_slots"]:
        return {
            "case_id": case_id,
            "test_id": test_id,
            "case_status": "NOT_RUN",
            "reason": "Case was not selected by this immutable Plan",
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
            "case_check_url": None,
            "case_log_url": None,
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
            "current_pr_matches": pr_matches,
            "selected": False,
            "check_conclusion": None,
            "cleanup_verified": None,
            "exit_code": None,
        }
    check = read_fixed_check(api, plan, case_id)
    html = check.get("html_url")
    require(
        isinstance(html, str)
        and re.fullmatch(
            r"https://github\.com/" + re.escape(GH_REPO) + r"/runs/[0-9]+(?:\?[^\s]*)?",
            html,
        ),
        "CASE_CHECK_HTML_URL_INVALID",
    )
    if check.get("status") != "completed":
        require(
            check.get("status") in {"queued", "in_progress"}
            and run_conclusion in {"failure", "cancelled", "timed_out"},
            "CASE_CHECK_NOT_TERMINAL",
        )
        return {
            "case_id": case_id,
            "test_id": test_id,
            "case_status": "NOT_RUN",
            "reason": "GitHub workflow concluded "
            + str(run_conclusion)
            + " before this Case published a terminal result",
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
            "case_check_url": html,
            "case_log_url": html,
            "check_id": check["id"],
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
            "current_pr_matches": pr_matches,
            "selected": True,
            "check_conclusion": check.get("conclusion"),
            "cleanup_verified": None,
            "exit_code": None,
        }
    summary = (check.get("output") or {}).get("summary")
    envelope = (
        strict_json(summary.encode(), limit=32 * 1024)
        if isinstance(summary, str)
        else None
    )
    require(
        envelope is not None
        and envelope.get("schema") == 4
        and envelope.get("phase") == "CASE_RESULT"
        and envelope.get("plan_digest") == plan["plan_digest"]
        and envelope.get("receipt") == receipt,
        "CASE_CHECK_RECEIPT_OR_PLAN_MISMATCH",
    )
    result = envelope.get("result")
    expected_identity = {
        "pr": plan["pr"],
        "merge": plan["merge"],
        "control": plan["control"],
    }
    require(
        isinstance(result, dict)
        and set(result)
        == {
            "case_id",
            "receipt",
            "identity",
            "status",
            "exit_code",
            "cleanup_verified",
            "summary",
        }
        and result.get("case_id") == case_id
        and result.get("receipt") == receipt
        and canonical(result.get("identity")) == canonical(expected_identity)
        and result.get("identity") == expected_identity,
        "CASE_RESULT_EXECUTION_IDENTITY_MISMATCH",
    )
    status = result.get("status")
    require(
        status
        in {
            "PASS",
            "FAIL",
            "INFRA_ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "NOT_RUN",
            "RESOURCE_TIMEOUT",
        },
        "CASE_STATUS_INVALID",
    )
    require(
        type(result.get("exit_code")) is int
        and -255 <= result["exit_code"] <= 255
        and type(result.get("cleanup_verified")) is bool
        and isinstance(result.get("summary"), list)
        and len(result["summary"]) <= 20
        and all(
            isinstance(item, str)
            and len(item) <= 180
            and not any(ord(c) < 32 for c in item)
            for item in result["summary"]
        ),
        "CASE_RESULT_SHAPE_INVALID",
    )
    require(check.get("status") == "completed", "CASE_CHECK_NOT_TERMINAL")
    if status == "PASS":
        require(
            result["cleanup_verified"] is True
            and result["exit_code"] == 0
            and check.get("conclusion") == "success",
            "CASE_PASS_EVIDENCE_MISMATCH",
        )
    else:
        expected_conclusion = {
            "CANCELLED": "cancelled",
            "TIMED_OUT": "timed_out",
            "NOT_RUN": "action_required",
        }.get(status, "failure")
        require(
            check.get("conclusion") == expected_conclusion,
            "CASE_CHECK_CONCLUSION_MISMATCH",
        )
    return {
        "case_id": case_id,
        "test_id": test_id,
        "case_status": status,
        "reason": "; ".join(result["summary"]) or status,
        "github_run_id": run_id,
        "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
        "case_check_url": html,
        "case_log_url": html,
        "check_id": check["id"],
        "plan_digest": plan["plan_digest"],
        "pr_generation": receipt["pr_generation"],
        "execution_generation": receipt["execution_generation"],
        "publication_revision": receipt["publication_revision"],
        "historical_only": True,
        "current_authority": "NOT_PROVIDED",
        "current_pr_matches": pr_matches,
        "selected": True,
        "check_conclusion": check.get("conclusion"),
        "cleanup_verified": result["cleanup_verified"],
        "exit_code": result["exit_code"],
    }


FORWARD_GATE_REASONS = {
    "OK",
    "SELECTED_CASE_FAILED",
    "GITHUB_RUN_NOT_SUCCESS",
    "PR_AUTHORITY_MISMATCH",
    "PR_AUTHORITY_UNAVAILABLE",
    "CONTROL_AUTHORITY_MISMATCH",
    "CONTROL_AUTHORITY_UNAVAILABLE",
    "CAPTURE_REVOKED",
}


CASE_OUTPUTS = {
    "worker_os_blue": "case_os_result",
    "dispatch_smoke_8p": "case_8p_result",
    "dispatch_smoke_16p": "case_16p_result",
}


def encode_case_evidence(
    intent,
    plan,
    receipt,
    result,
    *,
    control_matches,
    workflow_conclusion,
    current_plan=None,
    current_receipt=None,
    canonical_pointer=None,
    canonical_receipt=None,
):
    test_id = result["test_id"]
    case_id = CASE_ORDER[test_id]
    current_plan = current_plan or plan
    current_receipt = receipt if current_receipt is None else current_receipt
    result_run_id = result.get("github_run_id", plan["github"]["run_id"])
    body = {
        "schema": 1,
        "phase": "CASE_DISPLAY_EVIDENCE",
        "test_id": test_id,
        "case_id": case_id,
        "selected": result["selected"],
        "case_status": result["case_status"],
        "reason": result["reason"],
        "check_id": result.get("check_id"),
        "check_conclusion": result.get("check_conclusion"),
        "cleanup_verified": result.get("cleanup_verified"),
        "exit_code": result.get("exit_code"),
        "github_run_id": result_run_id,
        "github_run_attempt": plan["github"]["run_attempt"],
        "workflow_conclusion": workflow_conclusion,
        "github_run_url": "https://github.com/"
        + GH_REPO
        + "/actions/runs/"
        + str(result_run_id),
        "case_check_url": result.get("case_check_url"),
        "case_log_url": result.get("case_log_url"),
        "identity": {
            "repo_id": GH_ID,
            "capture_run_id": intent["bridge"]["bridge_action_run"],
            "capture_attempt": intent["bridge"]["bridge_attempt"],
            "bridge_nonce": intent["bridge"]["bridge_nonce"],
            "request_id": (
                current_receipt["request_id"]
                if current_receipt is not None
                else f"{GH_ID}/{current_plan['github']['run_id']}/1"
            ),
            "control_sha": intent["control_sha"],
            "plan_digest": current_plan["plan_digest"],
            "request_spec_digest": current_plan["request_spec_digest"],
            "receipt": current_receipt,
            "receipt_digest": (
                hashlib.sha256(canonical(current_receipt)).hexdigest()
                if current_receipt is not None
                else None
            ),
            "canonical_pointer": canonical_pointer,
            "canonical_receipt": canonical_receipt,
            "canonical_receipt_digest": (
                hashlib.sha256(canonical(canonical_receipt)).hexdigest()
                if canonical_receipt is not None
                else None
            ),
        },
        "observation": {
            "current_pr_matches": result.get("current_pr_matches") is True,
            "current_control_matches": control_matches is True,
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
            "not_current_merge_authorization": True,
        },
    }
    encoded = canonical(body)
    envelope = {
        "body": body,
        "evidence_digest": hashlib.sha256(encoded).hexdigest(),
    }
    raw = canonical(envelope)
    require(len(raw) <= 24 * 1024, "CASE_DISPLAY_EVIDENCE_TOO_LARGE")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def write_case_job_outputs(outputs, *, output_path=None):
    path = output_path or os.environ.get("ATOMGIT_OUTPUT")
    require(
        isinstance(path, str) and path and len(path) <= 4096, "ATOMGIT_OUTPUT_REQUIRED"
    )
    require(
        set(outputs)
        == set(CASE_OUTPUTS.values()) | {"forward_gate", "forward_gate_reason"}
        and outputs["forward_gate"] in {"success", "failure"}
        and outputs["forward_gate_reason"] in FORWARD_GATE_REASONS,
        "CASE_OUTPUT_SET_INVALID",
    )
    rows = []
    for key in (*CASE_OUTPUTS.values(), "forward_gate", "forward_gate_reason"):
        value = outputs[key]
        require(
            isinstance(value, str) and len(value) <= 32 * 1024,
            "CASE_OUTPUT_VALUE_INVALID",
        )
        require(re.fullmatch(r"[A-Za-z0-9_-]+", value), "CASE_OUTPUT_ENCODING_INVALID")
        rows.append(key + "=" + value + "\n")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode), "ATOMGIT_OUTPUT_NOT_REGULAR_FILE")
            with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as handle:
                handle.writelines(rows)
                handle.flush()
        finally:
            os.close(fd)
    except ManualForwardError:
        raise
    except Exception:
        raise ManualForwardError("ATOMGIT_OUTPUT_WRITE_FAILED") from None


def decode_case_evidence(environment):
    test_id = environment.get("RESULT_CASE", "")
    require(test_id in CASE_ORDER, "CASE_NOT_IN_FIXED_RESULT_SET")
    expected_case_id = CASE_ORDER[test_id]
    value = environment.get("CASE_EVIDENCE", "")
    require(
        isinstance(value, str) and 0 < len(value) <= 32 * 1024,
        "CASE_EVIDENCE_MISSING_OR_OVERSIZED",
    )
    require(re.fullmatch(r"[A-Za-z0-9_-]+", value), "CASE_EVIDENCE_ENCODING_INVALID")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        envelope = strict_json(raw, limit=24 * 1024)
    except Exception:
        raise ManualForwardError("CASE_EVIDENCE_INVALID") from None
    require(
        set(envelope) == {"body", "evidence_digest"}, "CASE_EVIDENCE_ENVELOPE_INVALID"
    )
    body = envelope["body"]
    require(
        isinstance(body, dict)
        and hashlib.sha256(canonical(body)).hexdigest() == envelope["evidence_digest"],
        "CASE_EVIDENCE_DIGEST_MISMATCH",
    )
    require(
        set(body)
        == {
            "schema",
            "phase",
            "test_id",
            "case_id",
            "selected",
            "case_status",
            "reason",
            "check_id",
            "check_conclusion",
            "cleanup_verified",
            "exit_code",
            "github_run_id",
            "github_run_attempt",
            "workflow_conclusion",
            "github_run_url",
            "case_check_url",
            "case_log_url",
            "identity",
            "observation",
        }
        and body["schema"] == 1
        and body["phase"] == "CASE_DISPLAY_EVIDENCE"
        and body["test_id"] == test_id
        and body["case_id"] == expected_case_id
        and type(body["selected"]) is bool
        and body["case_status"]
        in {
            "PASS",
            "FAIL",
            "INFRA_ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "NOT_RUN",
            "RESOURCE_TIMEOUT",
        }
        and isinstance(body["reason"], str)
        and 0 < len(body["reason"]) <= 4096
        and type(body["github_run_id"]) is int
        and body["github_run_id"] > 0
        and type(body["github_run_attempt"]) is int
        and body["github_run_attempt"] == 1
        and body["workflow_conclusion"]
        in {"success", "failure", "cancelled", "timed_out", "action_required"}
        and body["github_run_url"]
        == "https://github.com/"
        + GH_REPO
        + "/actions/runs/"
        + str(body["github_run_id"]),
        "CASE_EVIDENCE_BODY_INVALID",
    )
    identity = body["identity"]
    require(
        isinstance(identity, dict)
        and set(identity)
        == {
            "repo_id",
            "capture_run_id",
            "capture_attempt",
            "bridge_nonce",
            "request_id",
            "control_sha",
            "plan_digest",
            "request_spec_digest",
            "receipt",
            "receipt_digest",
            "canonical_pointer",
            "canonical_receipt",
            "canonical_receipt_digest",
        }
        and identity["repo_id"] == GH_ID
        and identity["capture_run_id"] == environment.get("CAPTURE_RUN_ID")
        and type(identity["capture_attempt"]) is int
        and identity["capture_attempt"] == 1
        and identity["capture_attempt"] == int(environment.get("CAPTURE_ATTEMPT", "0"))
        and isinstance(identity["bridge_nonce"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{16,100}", identity["bridge_nonce"])
        and isinstance(identity["request_id"], str)
        and re.fullmatch(r"[0-9]+/[0-9]+/1", identity["request_id"])
        and identity["control_sha"] == environment.get("CONTROL_SHA")
        and re.fullmatch(r"[0-9a-f]{40}", identity["control_sha"])
        and re.fullmatch(r"[0-9a-f]{64}", identity["plan_digest"])
        and re.fullmatch(r"[0-9a-f]{64}", identity["request_spec_digest"]),
        "CASE_EVIDENCE_IDENTITY_INVALID",
    )
    receipt = identity["receipt"]
    if receipt is None:
        require(
            identity["receipt_digest"] is None
            and body["case_status"] == "NOT_RUN"
            and body["cleanup_verified"] is None
            and body["exit_code"] is None
            and body["check_conclusion"] is None,
            "CASE_EVIDENCE_NO_ADMISSION_INVALID",
        )
    else:
        require(
            isinstance(receipt, dict)
            and set(receipt)
            == {
                "request_id",
                "plan_digest",
                "request_spec_digest",
                "source_snapshot_id",
                "dispatch_intent_id",
                "canonical_execution_id",
                "pr_generation",
                "execution_generation",
                "publication_revision",
                "duplicate",
            }
            and receipt["request_id"] == identity["request_id"]
            and receipt["plan_digest"] == identity["plan_digest"]
            and receipt["request_spec_digest"] == identity["request_spec_digest"]
            and receipt["source_snapshot_id"]
            == "manual-capture:" + identity["bridge_nonce"]
            and receipt["dispatch_intent_id"]
            == "manual-intent:" + identity["bridge_nonce"]
            and isinstance(receipt["canonical_execution_id"], str)
            and bool(receipt["canonical_execution_id"])
            and all(
                type(receipt[k]) is int and receipt[k] > 0
                for k in (
                    "pr_generation",
                    "execution_generation",
                    "publication_revision",
                )
            )
            and type(receipt["duplicate"]) is bool
            and hashlib.sha256(canonical(receipt)).hexdigest()
            == identity["receipt_digest"],
            "CASE_EVIDENCE_RECEIPT_INVALID",
        )
    if receipt is not None and receipt["duplicate"]:
        pointer = identity["canonical_pointer"]
        canonical_receipt = identity["canonical_receipt"]
        require(
            isinstance(pointer, dict)
            and set(pointer) == {"request_id", "plan_digest", "github", "result_slots"}
            and pointer["request_id"] != identity["request_id"]
            and isinstance(pointer["github"], dict)
            and pointer["github"].get("repo") == GH_REPO
            and pointer["github"].get("repo_id") == GH_ID
            and pointer["github"].get("workflow_id") == GH_WORKFLOW_ID
            and pointer["github"].get("workflow_path") == GH_WORKFLOW
            and type(pointer["github"].get("run_id")) is int
            and pointer["github"].get("run_id") > 0
            and pointer["github"].get("run_attempt") == 1
            and pointer["request_id"]
            == str(GH_ID) + "/" + str(pointer["github"]["run_id"]) + "/1"
            and isinstance(pointer["result_slots"], dict)
            and isinstance(canonical_receipt, dict)
            and pointer["plan_digest"] == canonical_receipt.get("plan_digest")
            and pointer["request_id"] == canonical_receipt.get("request_id")
            and set(canonical_receipt) == set(receipt)
            and canonical_receipt["duplicate"] is False
            and canonical_receipt["request_spec_digest"]
            == receipt["request_spec_digest"]
            and canonical_receipt["canonical_execution_id"]
            == receipt["canonical_execution_id"]
            and canonical_receipt["pr_generation"] == receipt["pr_generation"]
            and canonical_receipt["execution_generation"]
            == receipt["execution_generation"]
            and canonical_receipt["publication_revision"]
            == receipt["publication_revision"]
            and canonical_receipt["source_snapshot_id"].startswith("manual-capture:")
            and canonical_receipt["dispatch_intent_id"]
            == "manual-intent:"
            + canonical_receipt["source_snapshot_id"].split(":", 1)[1]
            and canonical_receipt["source_snapshot_id"] != receipt["source_snapshot_id"]
            and hashlib.sha256(canonical(canonical_receipt)).hexdigest()
            == identity["canonical_receipt_digest"],
            "CASE_EVIDENCE_CANONICAL_RECEIPT_INVALID",
        )
        slots = pointer["result_slots"]
        require(
            "aggregate" in slots
            and set(slots).issubset({"aggregate", *CASE_ORDER.values(), "authority"})
            and (not body["selected"] or expected_case_id in slots),
            "CASE_EVIDENCE_CANONICAL_SLOTS_INCOMPLETE",
        )
        slot_ids = []
        for slot_case_id, slot in slots.items():
            expected_external_id = (
                "lite-actions:v4:"
                + hashlib.sha256(
                    canonical(
                        {
                            "repo_id": GH_ID,
                            "run_id": pointer["github"]["run_id"],
                            "run_attempt": 1,
                            "case_id": slot_case_id,
                            "request_spec_digest": canonical_receipt[
                                "request_spec_digest"
                            ],
                        }
                    )
                ).hexdigest()
            )
            require(
                isinstance(slot, dict)
                and set(slot)
                == {
                    "id",
                    "app_id",
                    "repo_id",
                    "control_sha",
                    "run_id",
                    "run_attempt",
                    "case_id",
                    "external_id",
                }
                and type(slot["id"]) is int
                and slot["id"] > 0
                and slot["app_id"] == 5575236
                and slot["repo_id"] == GH_ID
                and slot["run_id"] == pointer["github"]["run_id"]
                and slot["run_attempt"] == 1
                and slot["case_id"] == slot_case_id
                and slot["control_sha"] == identity["control_sha"]
                and slot["external_id"] == expected_external_id,
                "CASE_EVIDENCE_CANONICAL_SLOT_INVALID",
            )
            slot_ids.append(slot["id"])
        require(
            len(slot_ids) == len(set(slot_ids)),
            "CASE_EVIDENCE_CANONICAL_SLOT_IDS_DUPLICATE",
        )
        if (
            body["selected"]
            and body["case_status"] == "INFRA_ERROR"
            and body["check_id"] is None
        ):
            require(
                body["case_check_url"] is None
                and body["case_log_url"] == body["github_run_url"],
                "CASE_EVIDENCE_CANONICAL_INFRA_LINK_INVALID",
            )
        elif body["selected"]:
            slot = slots.get(expected_case_id)
            require(
                isinstance(slot, dict)
                and type(slot.get("id")) is int
                and body["check_id"] == slot["id"]
                and slot.get("case_id") == expected_case_id
                and slot.get("repo_id") == GH_ID
                and slot.get("run_id") == pointer["github"]["run_id"]
                and slot.get("run_attempt") == 1
                and slot.get("control_sha") == identity["control_sha"]
                and slot.get("app_id") == 5575236,
                "CASE_EVIDENCE_CANONICAL_CASE_SLOT_MISMATCH",
            )
        require(
            body["github_run_id"] == pointer["github"]["run_id"]
            and identity["request_spec_digest"]
            == canonical_receipt["request_spec_digest"],
            "CASE_EVIDENCE_CANONICAL_RUN_MISMATCH",
        )
    else:
        require(
            identity["canonical_pointer"] is None
            and identity["canonical_receipt"] is None
            and identity["canonical_receipt_digest"] is None
            and (receipt is None or receipt["duplicate"] is False)
            and identity["request_id"]
            == str(GH_ID) + "/" + str(body["github_run_id"]) + "/1",
            "CASE_EVIDENCE_UNEXPECTED_CANONICAL_BINDING",
        )
    observation = body["observation"]
    require(
        isinstance(observation, dict)
        and set(observation)
        == {
            "current_pr_matches",
            "current_control_matches",
            "historical_only",
            "current_authority",
            "not_current_merge_authorization",
        }
        and type(observation["current_pr_matches"]) is bool
        and type(observation["current_control_matches"]) is bool
        and observation["historical_only"] is True
        and observation["current_authority"] == "NOT_PROVIDED"
        and observation["not_current_merge_authorization"] is True,
        "CASE_EVIDENCE_OBSERVATION_INVALID",
    )
    if body["selected"] is False:
        require(
            body["case_status"] == "NOT_RUN"
            and body["check_id"] is None
            and body["case_check_url"] is None
            and body["case_log_url"] is None,
            "CASE_UNSELECTED_RESULT_INVALID",
        )
    elif body["case_status"] == "INFRA_ERROR" and body["check_id"] is None:
        require(
            body["case_check_url"] is None
            and body["case_log_url"] == body["github_run_url"],
            "CASE_INFRASTRUCTURE_RUN_LINK_INVALID",
        )
    else:
        link = urllib.parse.urlparse(body["case_check_url"] or "")
        require(
            type(body["check_id"]) is int
            and body["check_id"] > 0
            and link.scheme == "https"
            and link.hostname == "github.com"
            and link.path.startswith("/" + GH_REPO + "/runs/")
            and link.path.rsplit("/", 1)[-1].isdigit()
            and int(link.path.rsplit("/", 1)[-1]) == body["check_id"]
            and body["case_log_url"] == body["case_check_url"],
            "CASE_SELECTED_RESULT_LINK_INVALID",
        )
        if body["case_status"] == "PASS":
            require(
                body["cleanup_verified"] is True
                and body["exit_code"] == 0
                and body["check_conclusion"] == "success",
                "CASE_EVIDENCE_PASS_MISMATCH",
            )
        elif body["case_status"] == "CANCELLED":
            require(
                body["check_conclusion"] == "cancelled", "CASE_EVIDENCE_CANCEL_MISMATCH"
            )
        else:
            require(body["case_status"] != "PASS", "CASE_EVIDENCE_FALSE_PASS")
    return body


def display_case_main(environment):
    case = environment.get("RESULT_CASE", "unknown")
    if environment.get("CAPTURE_RESULT") != "success":
        payload = {
            "test_id": case,
            "case_status": "NOT_RUN",
            "reason": "GitCode Capture did not complete; no GitHub execution was authorized",
            "github_run_id": None,
            "github_run_url": None,
            "case_log_url": None,
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
        }
        print(json.dumps(payload, sort_keys=True))
        return 1
    try:
        body = decode_case_evidence(environment)
        reason = body["reason"]
        payload = {
            "test_id": body["test_id"],
            "case_id": body["case_id"],
            "case_status": body["case_status"],
            "reason": reason,
            "github_run_id": body["github_run_id"],
            "github_run_url": body["github_run_url"],
            "case_check_url": body["case_check_url"],
            "case_log_url": body["case_log_url"],
            "check_id": body["check_id"],
            "plan_digest": body["identity"]["plan_digest"],
            "pr_generation": (
                body["identity"]["receipt"]["pr_generation"]
                if body["identity"]["receipt"] is not None
                else None
            ),
            "execution_generation": (
                body["identity"]["receipt"]["execution_generation"]
                if body["identity"]["receipt"] is not None
                else None
            ),
            "publication_revision": (
                body["identity"]["receipt"]["publication_revision"]
                if body["identity"]["receipt"] is not None
                else None
            ),
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
            "current_pr_matches": body["observation"]["current_pr_matches"],
            "current_control_matches": body["observation"]["current_control_matches"],
        }
        print(json.dumps(payload, sort_keys=True))
        return result_exit_code(
            {"case_status": body["case_status"], "selected": body["selected"]}
        )
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, ManualForwardError)
            else type(error).__name__
        )
        payload = {
            "test_id": case,
            "case_status": "INFRA_ERROR",
            "reason": "CASE_EVIDENCE_REJECTED:" + code,
            "github_run_id": None,
            "github_run_url": None,
            "case_log_url": None,
            "historical_only": True,
            "current_authority": "NOT_PROVIDED",
        }
        print(json.dumps(payload, sort_keys=True))
        return 1


def result_exit_code(result):
    return (
        0
        if result.get("case_status") == "PASS" or result.get("selected") is False
        else 1
    )


def result_main():
    return display_case_main(os.environ)


def forward(api, intent, *, clock=time.time, sleep=time.sleep, progress=lambda _: None):
    require(int(clock()) < intent["deadline"], "MANUAL_DEADLINE_EXCEEDED")
    release(api, intent["control_sha"])
    require(int(clock()) < intent["deadline"], "MANUAL_DEADLINE_EXCEEDED")
    run_id = find_run(api, intent)
    initial_response_run = False
    if run_id is None:
        capture_before_dispatch = run_identity(
            api, intent["bridge"]["bridge_action_run"], intent["control_sha"]
        )
        require(
            capture_before_dispatch.get("status") == "RUNNING"
            or (
                capture_before_dispatch.get("status") == "COMPLETED"
                and capture_before_dispatch.get("conclusion") == "success"
            ),
            "CAPTURE_RUN_CANCELLED_OR_FAILED",
        )
        require(
            int(clock()) < intent["deadline"]
            and int(clock()) - intent["bridge"]["event_created_at"] <= 300,
            "CAPTURE_EXPIRED_BEFORE_DISPATCH",
        )
        body = {
            "ref": "master",
            "return_run_details": True,
            "inputs": {
                "source_provider": "gitcode",
                "gitcode_request": canonical(intent).decode(),
            },
        }
        try:
            response = api.gh(
                f"/actions/workflows/{GH_WORKFLOW_ID}/dispatches",
                body=body,
                dispatch=True,
            )
        except (OSError, TimeoutError, ManualForwardError):
            response = None
        if isinstance(response, dict) and type(response.get("workflow_run_id")) is int:
            run_id = response["workflow_run_id"]
            require(
                response.get("run_url")
                == f"https://api.github.com/repos/{GH_REPO}/actions/runs/{run_id}"
                and response.get("html_url")
                == f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
                "DISPATCH_RESPONSE_IDENTITY_MISMATCH",
            )
            initial_response_run = True
        if run_id is None:
            for _ in range(6):
                require(int(clock()) < intent["deadline"], "MANUAL_DEADLINE_EXCEEDED")
                run_id = find_run(api, intent)
                if run_id is not None:
                    break
                sleep(5)
            require(run_id is not None, "DISPATCH_OUTCOME_UNKNOWN_NO_RETRY")

    first_bound = (
        bind_initial_run(
            api, intent, run_id, clock=clock, sleep=sleep, progress=progress
        )
        if initial_response_run
        else None
    )
    reported_run = False
    capture_revoked = False
    while int(clock()) < intent["deadline"]:
        try:
            capture_observation = run_identity(
                api, intent["bridge"]["bridge_action_run"], intent["control_sha"]
            )
            if not (
                capture_observation.get("status") == "RUNNING"
                or (
                    capture_observation.get("status") == "COMPLETED"
                    and capture_observation.get("conclusion") == "success"
                )
            ):
                capture_revoked = True
        except Exception:
            capture_revoked = True
        run = (
            first_bound
            if first_bound is not None
            else api.gh(f"/actions/runs/{run_id}")
        )
        first_bound = None
        require(
            run.get("id") == run_id and type(run.get("id")) is int,
            "GITHUB_RUN_RESPONSE_ID_MISMATCH",
        )

        validate_github_run(run, intent)
        if not reported_run:
            progress(
                {
                    "event": "GITHUB_RUN_BOUND",
                    "test_id": intent["selectors"][0]["test_id"],
                    "github_run_id": run_id,
                    "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
                    "not_current_merge_authorization": True,
                }
            )
            reported_run = True
        require(int(clock()) < intent["deadline"], "MANUAL_DEADLINE_EXCEEDED")
        if run.get("status") != "completed":
            sleep(min(30, max(0, intent["deadline"] - int(clock()))))
            continue

        jobs_response = api.gh(
            f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
        )
        rows = jobs_response.get("jobs") if isinstance(jobs_response, dict) else None
        require(
            isinstance(rows, list)
            and type(jobs_response.get("total_count")) is int
            and jobs_response["total_count"] == len(rows) <= 100,
            "GITHUB_JOBS_INCOMPLETE",
        )
        observed_jobs = {}
        for name in ("prepare-gitcode", "wait-gitcode", "Lite-Actions-GitCode-Smoke"):
            matches = [job for job in rows if job.get("name") == name]
            require(
                len(matches) == 1
                and matches[0].get("status") == "completed"
                and matches[0].get("run_id") == run_id
                and matches[0].get("run_attempt") == 1
                and matches[0].get("conclusion")
                in {"success", "failure", "cancelled", "timed_out", "action_required"},
                "GITHUB_FIXED_RESULT_JOB_IDENTITY_INVALID",
            )
            observed_jobs[name] = matches[0]
        require(
            observed_jobs["prepare-gitcode"]["conclusion"] == "success",
            "GITHUB_PREPARE_JOB_NOT_SUCCESSFUL",
        )

        plan = read_plan_artifact(api, intent, run_id)
        receipt, canonical_pointer = read_admission_record(api, plan, intent)
        result_plan = plan
        result_receipt = receipt
        result_intent = intent
        result_run = run
        canonical_receipt = None
        if receipt is not None and receipt["duplicate"]:
            result_plan, canonical_receipt, result_intent, result_run = (
                resolve_canonical_execution(
                    api, intent, plan, receipt, canonical_pointer
                )
            )
            result_receipt = canonical_receipt
        authority_reasons = ["CAPTURE_REVOKED"] if capture_revoked else []
        try:
            pr_matches = current_pr_matches(api, intent)
            if not pr_matches:
                authority_reasons.append("PR_AUTHORITY_MISMATCH")
        except Exception:
            pr_matches = False
            authority_reasons.append("PR_AUTHORITY_UNAVAILABLE")
        try:
            control_matches = current_control_matches(api, intent["control_sha"])
            if not control_matches:
                authority_reasons.append("CONTROL_AUTHORITY_MISMATCH")
        except Exception:
            control_matches = False
            authority_reasons.append("CONTROL_AUTHORITY_UNAVAILABLE")
        selected_ids = {row["test_id"] for row in intent["selectors"]}
        case_results = []
        for test_id in ALLOWED_CASES:
            try:
                if receipt is None and test_id in selected_ids:
                    case_id = CASE_ORDER[test_id]
                    check = read_fixed_check(api, result_plan, case_id)
                    html = check.get("html_url")
                    require(
                        check.get("status") in {"queued", "in_progress"}
                        and check.get("conclusion") is None
                        and isinstance(html, str)
                        and urllib.parse.urlparse(html).hostname == "github.com",
                        "CASE_NOT_ADMITTED_CHECK_INVALID",
                    )
                    result = {
                        "case_id": case_id,
                        "test_id": test_id,
                        "case_status": "NOT_RUN",
                        "reason": "No Admission Receipt was published; no Case execution was authorized",
                        "github_run_id": run_id,
                        "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
                        "case_check_url": html,
                        "case_log_url": html,
                        "check_id": check["id"],
                        "selected": True,
                        "check_conclusion": None,
                        "cleanup_verified": None,
                        "exit_code": None,
                        "current_pr_matches": pr_matches,
                    }
                else:
                    result = case_check(
                        api,
                        result_intent,
                        result_plan,
                        result_receipt,
                        result_plan["github"]["run_id"],
                        test_id,
                        run_conclusion=result_run.get("conclusion"),
                        pr_matches=pr_matches,
                    )
            except Exception as error:
                code = (
                    str(error)
                    if isinstance(error, ManualForwardError)
                    else type(error).__name__
                )
                result = {
                    "case_id": CASE_ORDER[test_id],
                    "test_id": test_id,
                    "case_status": "INFRA_ERROR",
                    "reason": "CASE_EVIDENCE_READ_FAILED:" + code,
                    "github_run_id": run_id,
                    "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
                    "case_check_url": None,
                    "case_log_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
                    "check_id": None,
                    "selected": test_id in selected_ids,
                    "check_conclusion": None,
                    "cleanup_verified": None,
                    "exit_code": None,
                    "current_pr_matches": pr_matches,
                }
            case_results.append(result)

        try:
            final_capture = run_identity(
                api, intent["bridge"]["bridge_action_run"], intent["control_sha"]
            )
            capture_authority_ok = final_capture.get("status") == "RUNNING" or (
                final_capture.get("status") == "COMPLETED"
                and final_capture.get("conclusion") == "success"
            )
        except Exception:
            capture_authority_ok = False
        if not capture_authority_ok and "CAPTURE_REVOKED" not in authority_reasons:
            authority_reasons.append("CAPTURE_REVOKED")
        require(int(clock()) < intent["deadline"], "MANUAL_DEADLINE_EXCEEDED")

        selected_pass = all(
            result["case_status"] == "PASS"
            for result in case_results
            if result["test_id"] in selected_ids
        )
        outputs = {}
        for result in case_results:
            outputs[CASE_OUTPUTS[result["test_id"]]] = encode_case_evidence(
                intent,
                result_plan,
                result_receipt,
                result,
                control_matches=control_matches,
                workflow_conclusion=result_run.get("conclusion"),
                current_plan=plan,
                current_receipt=receipt,
                canonical_pointer=canonical_pointer
                if receipt is not None and receipt["duplicate"]
                else None,
                canonical_receipt=canonical_receipt,
            )
        gate_reasons = list(authority_reasons)
        if (
            run.get("conclusion") != "success"
            or result_run.get("conclusion") != "success"
        ):
            gate_reasons.append("GITHUB_RUN_NOT_SUCCESS")
        if not selected_pass:
            gate_reasons.append("SELECTED_CASE_FAILED")
        gate_reason = gate_reasons[0] if gate_reasons else "OK"
        outputs["forward_gate"] = "success" if not gate_reasons else "failure"
        outputs["forward_gate_reason"] = gate_reason
        return {
            "scope": "PERSONAL_MANUAL_FORWARD_NOT_OFFICIAL_PR_GATE",
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/{GH_REPO}/actions/runs/{run_id}",
            "control_sha": intent["control_sha"],
            "case_results": [
                {
                    key: result.get(key)
                    for key in (
                        "test_id",
                        "case_id",
                        "case_status",
                        "selected",
                        "reason",
                        "check_id",
                        "check_conclusion",
                        "cleanup_verified",
                        "exit_code",
                        "case_check_url",
                        "case_log_url",
                        "current_pr_matches",
                        "github_run_id",
                        "github_run_url",
                    )
                }
                for result in case_results
            ],
            "selected_cases_pass": selected_pass,
            "forward_gate": outputs["forward_gate"],
            "forward_gate_reason": gate_reason,
            "workflow_conclusion": run.get("conclusion"),
            "historical_execution_result": "PASS" if selected_pass else "FAIL",
            "current_authority": "NOT_PROVIDED",
            "not_current_merge_authorization": True,
            "merge_gate": "NOT_PROVIDED",
            "case_job_outputs": outputs,
        }
    raise ManualForwardError("MANUAL_DEADLINE_EXCEEDED")


def main():
    if os.environ.get("RESULT_CASE"):
        return result_main()
    try:
        api = API(os.environ)
        intent = retrieve_intent(api, os.environ)
        result = forward(
            api,
            intent,
            progress=lambda row: print(json.dumps(row, sort_keys=True), flush=True),
        )
        outputs = result.pop("case_job_outputs")
        write_case_job_outputs(outputs)
        result.pop("selected_cases_pass")
        print(json.dumps(result, sort_keys=True))
        # This job publishes verified evidence. Case outcomes and the global
        # workflow conclusion are enforced by downstream read-only jobs.
        return 0
    except Exception as error:
        return emit_failure(error)


if __name__ == "__main__":
    raise SystemExit(main())
