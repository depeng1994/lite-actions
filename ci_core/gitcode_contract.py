"""Independent schema-4 admission contract; schema-3 meanings stay unchanged.

All observations passed to verify_request must be obtained by trusted adapters
from the indicated repository/run/attempt. Request fields never count as API
observations or as permission to enable this provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ci_core.gitcode_source import MERGE_POLICY, require_ref, require_sha

VLLM_BLUE_SMOKE_DOCKER = "vllm_hccl_smoke"
TORCHTITAN_REPOSITORY_IDS = frozenset({1210038933, 1245430804})
STAGING_GITHUB_REPO_ID = 1245430804
MODEL_REPOSITORIES = MappingProxyType(
    {
        1210038933: ("cann-framework/torchtitan-npu", "master"),
        1438625957: ("cann-framework/vllm-ascend", "main"),
        1245430804: ("depeng1994/torchtitan-npu", "master"),
    }
)
PR_KEYS = {
    "repo_id",
    "number",
    "head_repo_id",
    "head_ref",
    "head_sha",
    "base_repo_id",
    "base_ref",
    "base_sha",
}
CONTROL_KEYS = {
    "upstream_repo_id",
    "upstream_default_ref",
    "upstream_control_sha",
    "github_repo_id",
    "default_ref",
    "control_sha",
    "runner_sha256",
}
MERGE_KEYS = {"policy", "builder_version", "tested_merge_sha", "tested_tree_sha"}
GITHUB_KEYS = {
    "repo",
    "repo_id",
    "workflow_id",
    "workflow_path",
    "run_id",
    "run_attempt",
    "prepare_job_id",
}
BRIDGE_KEYS = {
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
PLAN_KEYS = {
    "schema",
    "source_provider",
    "test_mode",
    "pr",
    "merge",
    "control",
    "github",
    "bridge",
    "pipeline",
    "policy_version",
    "environment_version",
    "deadline",
    "artifacts",
    "result_slots",
    "cases",
    "request_spec_digest",
    "plan_digest",
}
RECEIPT_KEYS = {
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


class ContractError(ValueError):
    pass


def canonical(value: Any, *, limit: int = 256 * 1024) -> bytes:
    try:
        data = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("NON_CANONICAL_JSON") from exc
    if len(data) > limit:
        raise ContractError("JSON_SIZE_LIMIT")
    return data


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(data: bytes, *, limit: int = 256 * 1024) -> dict:
    if len(data) > limit:
        raise ContractError("JSON_SIZE_LIMIT")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(
            data,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ContractError("NON_FINITE_JSON")
            ),
        )
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ContractError("INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise ContractError("JSON_OBJECT_REQUIRED")
    canonical(value, limit=limit)
    return value


def shape(value, keys, code="INVALID_STRUCTURE"):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError(code)


def positive(value, code="INVALID_INTEGER"):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ContractError(code)
    return value


def text(value, *, limit=200, code="INVALID_TEXT"):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
    ):
        raise ContractError(code)
    return value


def hex_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("INVALID_SHA256")
    return value


def validate_pr(
    pr: dict,
    *,
    allowed_repo_id: str | None = None,
    allowed_refs: tuple[str, ...] = (),
    allowed_head_repo_ids: tuple[str, ...] | None = (),
) -> dict:
    shape(pr, PR_KEYS, "INVALID_PR_IDENTITY")
    for name in ("repo_id", "head_repo_id", "base_repo_id"):
        if not isinstance(pr[name], str) or not re.fullmatch(
            r"[1-9][0-9]{0,19}", pr[name]
        ):
            raise ContractError("INVALID_REPOSITORY_ID")
    positive(pr["number"])
    for name in ("head_sha", "base_sha"):
        require_sha(pr[name])
    for name in ("head_ref", "base_ref"):
        require_ref(pr[name])
    if pr["base_repo_id"] != pr["repo_id"]:
        raise ContractError("TARGET_REPOSITORY_IDENTITY_MISMATCH")
    # None is structural validation for already trusted ledger/source objects,
    # never admission authorization. Empty remains same-repository only.
    if allowed_head_repo_ids is not None and pr["head_repo_id"] not in (
        pr["repo_id"],
        *allowed_head_repo_ids,
    ):
        raise ContractError("EXTERNAL_FORK_NOT_AUTHORIZED")
    if allowed_repo_id is not None and pr["repo_id"] != allowed_repo_id:
        raise ContractError("GITCODE_REPOSITORY_NOT_AUTHORIZED")
    if allowed_refs and pr["base_ref"] not in allowed_refs:
        raise ContractError("BASE_REF_NOT_AUTHORIZED")
    return pr


@dataclass(frozen=True)
class ControlPolicy:
    """Reviewed release and deployment policy, never constructed from a Plan."""

    github_repo_id: int
    gitcode_repo_id: str
    upstream_repo_id: str
    upstream_default_ref: str
    control_sha: str
    workflow_id: int
    workflow_path: str
    app_id: int
    allowed_actors: tuple[str, ...]
    allowed_base_refs: tuple[str, ...]
    policy_version: str
    environment_version: str
    git_builder_version: str
    runner_sha256: str
    enabled: bool = False
    prepare_job_name: str = "prepare-gitcode"
    max_duration_seconds: int = 28_800
    allowed_head_repositories: tuple[tuple[str, str], ...] = ()
    deployment_profile: str = "production"

    def __post_init__(self):
        if self.github_repo_id not in MODEL_REPOSITORIES:
            raise ContractError("CONTROL_REPOSITORY_NOT_AUTHORIZED")
        if self.github_repo_id == STAGING_GITHUB_REPO_ID:
            if (
                self.deployment_profile != "personal-staging"
                or self.gitcode_repo_id != "9075802"
                or self.upstream_repo_id != "9075802"
                or self.upstream_default_ref != "master"
                or self.allowed_base_refs != ("master",)
                or any(
                    binding != ("depeng1994/torchtitan-npu", "9075802")
                    for binding in self.allowed_head_repositories
                )
                or self.app_id == 5575236
            ):
                raise ContractError("PERSONAL_STAGING_BINDING_REQUIRED")
        elif self.deployment_profile != "production":
            raise ContractError("PRODUCTION_PROFILE_REQUIRED")
        for value in (self.workflow_id, self.app_id, self.max_duration_seconds):
            positive(value)
        require_sha(self.control_sha)
        hex_digest(self.runner_sha256)
        require_ref(self.upstream_default_ref)
        if not self.allowed_actors or not self.allowed_base_refs:
            raise ContractError("EMPTY_AUTHORIZATION_POLICY")
        for ref in self.allowed_base_refs:
            require_ref(ref)
        if self.workflow_path not in (
            ".github/workflows/lite_actions_dispatcher_smoke.yml",
            ".github/workflows/nightly_fulltests.yml",
        ):
            raise ContractError("WORKFLOW_NOT_AUTHORIZED")
        if type(self.enabled) is not bool:
            raise ContractError("INVALID_PROVIDER_SWITCH")
        paths, ids = set(), set()
        for binding in self.allowed_head_repositories:
            if not isinstance(binding, tuple) or len(binding) != 2:
                raise ContractError("INVALID_HEAD_REPOSITORY_BINDING")
            path, ident = binding
            if (
                not isinstance(path, str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", path
                )
                or not isinstance(ident, str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", ident)
                or path in paths
                or ident in ids
            ):
                raise ContractError("INVALID_HEAD_REPOSITORY_BINDING")
            paths.add(path)
            ids.add(ident)


def vllm_blue_catalog() -> dict[str, dict]:
    """Independent worker-side bindings, including OS's explicit no-Docker rule."""
    cases = {}
    for name, nodes, devices, timeout in (
        ("vllm_worker_os_blue", 0, 0, 90),
        ("dispatch_smoke_a3_8p", 1, 8, 120),
        ("dispatch_smoke_a3_16p", 2, 8, 240),
    ):
        cases[name] = {
            "test_id": name,
            "label": "Blue OS probe"
            if not nodes
            else f"A3 HCCL distributed smoke {nodes * devices}P",
            "kind": "worker_probe" if not nodes else "npu_smoke",
            "worker": "blue",
            "target_pool": "a3_shared_blue" if nodes else None,
            "resources": {"nodes": nodes, "devices_per_node": devices},
            "docker": VLLM_BLUE_SMOKE_DOCKER if nodes else None,
            "execution_timeout_seconds": timeout,
        }
    return cases


def select_cases(selectors: list[dict], catalog: dict[str, dict]) -> list[dict]:
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 64:
        raise ContractError("INVALID_CASE_SELECTION")
    result, seen = [], set()
    for order, selector in enumerate(selectors):
        shape(selector, {"test_id"}, "CASE_OVERRIDES_FORBIDDEN")
        name = text(selector["test_id"], limit=80)
        if name in seen or name not in catalog:
            raise ContractError("DUPLICATE_OR_UNKNOWN_CASE")
        seen.add(name)
        case = json.loads(canonical(catalog[name]))
        if case.get("worker") != "blue":
            raise ContractError("GITCODE_REQUIRES_BLUE")
        if case.get("kind") == "worker_probe" and (
            case.get("docker") is not None
            or case.get("target_pool") is not None
            or case.get("resources") != {"nodes": 0, "devices_per_node": 0}
        ):
            raise ContractError("PROBE_CANNOT_REQUEST_RESOURCES")
        case.update(order=order, case_id=f"case_{order:03d}_{name}")
        result.append(case)
    return result


def validate_pipeline(pipeline: str, workflow_path: str, cases: list[dict]) -> None:
    expected = {
        ".github/workflows/lite_actions_dispatcher_smoke.yml": (
            "smoke",
            {"worker_probe", "npu_smoke"},
        ),
        ".github/workflows/nightly_fulltests.yml": ("nightly", {"train"}),
    }.get(workflow_path)
    if expected is None or pipeline != expected[0]:
        raise ContractError("PIPELINE_WORKFLOW_MISMATCH")
    if not cases or any(case.get("kind") not in expected[1] for case in cases):
        raise ContractError("PIPELINE_CASE_KIND_MISMATCH")


AUTHORITY_PROTOCOL = "artifact-check-v1"


def validate_plan_shape(plan):
    keys = PLAN_KEYS | (
        {"authority_protocol"} if "authority_protocol" in plan else set()
    )
    shape(plan, keys, "INVALID_SCHEMA4_PLAN")
    if (
        "authority_protocol" in plan
        and plan["authority_protocol"] != AUTHORITY_PROTOCOL
    ):
        raise ContractError("UNKNOWN_AUTHORITY_PROTOCOL")


def planned_slot_ids(plan):
    result = {"aggregate"} | {case["case_id"] for case in plan["cases"]}
    if "authority_protocol" in plan:
        if plan["authority_protocol"] != AUTHORITY_PROTOCOL:
            raise ContractError("UNKNOWN_AUTHORITY_PROTOCOL")
        result.add("authority")
    return result


def spec_digest(plan: dict) -> str:
    """Semantic identity excludes delivery nonce, run, attempt, artifacts and slots."""
    return digest(
        {
            "pr": plan["pr"],
            "merge": plan["merge"],
            "control": plan["control"],
            "workflow_id": plan["github"]["workflow_id"],
            "workflow_path": plan["github"]["workflow_path"],
            "pipeline": plan["pipeline"],
            "policy_version": plan["policy_version"],
            "environment_version": plan["environment_version"],
            "cases": plan["cases"],
            **(
                {"authority_protocol": plan["authority_protocol"]}
                if "authority_protocol" in plan
                else {}
            ),
        }
    )


def seal_plan(plan: dict) -> dict:
    result = json.loads(canonical(plan))
    result["request_spec_digest"] = spec_digest(result)
    result.pop("plan_digest", None)
    result["plan_digest"] = digest(result)
    return result


def external_id(plan: dict, case_id: str) -> str:
    return "lite-actions:v4:" + digest(
        {
            "repo_id": plan["github"]["repo_id"],
            "run_id": plan["github"]["run_id"],
            "run_attempt": plan["github"]["run_attempt"],
            "case_id": case_id,
            "request_spec_digest": plan["request_spec_digest"],
        }
    )


def source_identity(plan: dict) -> dict:
    return {"pr": plan["pr"], "merge": plan["merge"], "control": plan["control"]}


def request_id(plan: dict) -> str:
    github = plan["github"]
    return f"{github['repo_id']}/{github['run_id']}/{github['run_attempt']}"


@dataclass(frozen=True)
class VerifiedRequest:
    serialized: bytes

    @property
    def plan(self) -> dict:
        return load_json(self.serialized)


def verify_request(
    plan: dict,
    *,
    policy: ControlPolicy,
    catalog: dict[str, dict],
    run: dict,
    prepare_job: dict,
    artifacts: dict,
    slots: dict,
    manifest: dict,
    intent: dict,
    current_pr: dict,
    now: int,
) -> VerifiedRequest:
    if not policy.enabled:
        raise ContractError("GITCODE_PROVIDER_DISABLED")
    encoded = canonical(plan)
    validate_plan_shape(plan)
    if (
        type(plan["schema"]) is not int
        or plan["schema"] != 4
        or plan["source_provider"] != "gitcode"
        or plan["test_mode"] != "merge_result"
    ):
        raise ContractError("UNSUPPORTED_SOURCE_PROTOCOL")
    if plan["plan_digest"] != digest(
        {k: v for k, v in plan.items() if k != "plan_digest"}
    ):
        raise ContractError("PLAN_DIGEST_MISMATCH")
    validate_pr(
        plan["pr"],
        allowed_repo_id=policy.gitcode_repo_id,
        allowed_refs=policy.allowed_base_refs,
        allowed_head_repo_ids=tuple(
            ident for _, ident in policy.allowed_head_repositories
        ),
    )
    shape(current_pr, {"identity", "state", "authorized"})
    if (
        current_pr["state"] != "open"
        or current_pr["authorized"] is not True
        or canonical(current_pr["identity"]) != canonical(plan["pr"])
    ):
        raise ContractError("PR_VERSION_STALE_OR_UNAUTHORIZED")
    shape(plan["control"], CONTROL_KEYS)
    control = plan["control"]
    repo, default_ref = MODEL_REPOSITORIES[policy.github_repo_id]
    expected_control = {
        "upstream_repo_id": policy.upstream_repo_id,
        "upstream_default_ref": policy.upstream_default_ref,
        "upstream_control_sha": policy.control_sha,
        "github_repo_id": policy.github_repo_id,
        "default_ref": default_ref,
        "control_sha": policy.control_sha,
        "runner_sha256": policy.runner_sha256,
    }
    if canonical(control) != canonical(expected_control):
        raise ContractError("CONTROL_RELEASE_MISMATCH")
    shape(plan["github"], GITHUB_KEYS)
    gh = plan["github"]
    for name in ("repo_id", "workflow_id", "run_id", "run_attempt", "prepare_job_id"):
        positive(gh[name])
    if gh["run_attempt"] != 1 or run.get("run_attempt") != 1:
        raise ContractError("USE_TRUSTED_BRIDGE_FOR_WHOLE_RUN_RERUN")
    if (
        gh["repo"] != repo
        or gh["repo_id"] != policy.github_repo_id
        or gh["workflow_id"] != policy.workflow_id
        or gh["workflow_path"] != policy.workflow_path
    ):
        raise ContractError("CONTROL_WORKFLOW_MISMATCH")
    expected_run = {
        "id": gh["run_id"],
        "run_attempt": 1,
        "repository_id": policy.github_repo_id,
        "head_sha": policy.control_sha,
        "head_branch": default_ref,
        "workflow_id": policy.workflow_id,
        "path": policy.workflow_path,
        "event": "workflow_dispatch",
    }
    if any(
        type(run.get(k)) is not type(v) or run.get(k) != v
        for k, v in expected_run.items()
    ):
        raise ContractError("ACTUAL_WORKFLOW_RUN_MISMATCH")
    expected_job = {
        "id": gh["prepare_job_id"],
        "run_id": gh["run_id"],
        "run_attempt": 1,
        "name": policy.prepare_job_name,
        "status": "completed",
        "conclusion": "success",
    }
    if any(
        type(prepare_job.get(k)) is not type(v) or prepare_job.get(k) != v
        for k, v in expected_job.items()
    ):
        raise ContractError("PREPARE_NOT_SUCCESSFUL")
    shape(plan["bridge"], BRIDGE_KEYS)
    for key, value in plan["bridge"].items():
        positive(value) if key in ("bridge_attempt", "event_created_at") else text(
            value, limit=160
        )
    if plan["bridge"]["actor"] not in policy.allowed_actors:
        raise ContractError("BRIDGE_ACTOR_NOT_AUTHORIZED")
    positive(plan["deadline"])
    if (
        not plan["bridge"]["event_created_at"] <= now < plan["deadline"]
        or plan["deadline"] - plan["bridge"]["event_created_at"]
        > policy.max_duration_seconds
    ):
        raise ContractError("REQUEST_DEADLINE_INVALID_OR_EXPIRED")
    if (
        plan["pipeline"] not in ("smoke", "nightly")
        or plan["policy_version"] != policy.policy_version
        or plan["environment_version"] != policy.environment_version
    ):
        raise ContractError("TRUSTED_TEST_POLICY_MISMATCH")
    shape(plan["merge"], MERGE_KEYS)
    merge = plan["merge"]
    if (
        merge["policy"] != MERGE_POLICY
        or merge["builder_version"] != policy.git_builder_version
    ):
        raise ContractError("MERGE_POLICY_MISMATCH")
    require_sha(merge["tested_merge_sha"])
    require_sha(merge["tested_tree_sha"])
    if not isinstance(plan["cases"], list) or any(
        not isinstance(c, dict) or "test_id" not in c for c in plan["cases"]
    ):
        raise ContractError("INVALID_CASE_SET")
    selectors = [{"test_id": c["test_id"]} for c in plan["cases"]]
    if policy.github_repo_id == 1438625957:
        catalog = vllm_blue_catalog()
        if plan["pipeline"] != "smoke":
            raise ContractError("VLLM_NIGHTLY_NOT_AUTHORIZED")
    if canonical(plan["cases"]) != canonical(select_cases(selectors, catalog)):
        raise ContractError("UNTRUSTED_CASE_RESOURCES")
    validate_pipeline(plan["pipeline"], gh["workflow_path"], plan["cases"])
    if plan["request_spec_digest"] != spec_digest(plan):
        raise ContractError("REQUEST_SPEC_DIGEST_MISMATCH")
    _verify_bundles(plan, artifacts, manifest, intent, selectors)
    _verify_slots(plan, policy.app_id, slots)
    return VerifiedRequest(encoded)


def _verify_bundles(plan, observations, manifest, intent, selectors):
    shape(plan["artifacts"], {"intent", "source", "runner", "manifest"})
    shape(observations, set(plan["artifacts"]))
    gh, refs = plan["github"], plan["artifacts"]
    prefixes = {
        "intent": "dispatch-intent",
        "source": "gitcode-merge-source",
        "runner": "trusted-runner",
        "manifest": "source-manifest",
    }
    ids = set()
    for role, ref in refs.items():
        shape(ref, {"id", "name", "size", "zip_sha256", "content_sha256"})
        positive(ref["id"])
        positive(ref["size"])
        hex_digest(ref["zip_sha256"])
        hex_digest(ref["content_sha256"])
        if (
            ref["id"] in ids
            or ref["size"] > 512 * 1024**2
            or ref["name"] != f"{prefixes[role]}-{gh['run_id']}-{gh['run_attempt']}"
        ):
            raise ContractError("ARTIFACT_IDENTITY_MISMATCH")
        ids.add(ref["id"])
        expected = {
            **ref,
            "repo_id": gh["repo_id"],
            "run_id": gh["run_id"],
            "run_attempt": gh["run_attempt"],
            "expired": False,
        }
        if canonical(observations[role]) != canonical(expected):
            raise ContractError("ARTIFACT_NOT_READY_OR_DIGEST_MISMATCH")
    shape(
        manifest,
        {
            "schema",
            "source_provider",
            "test_mode",
            "pr",
            "merge",
            "control",
            "github_run_id",
            "run_attempt",
            "source_artifact_id",
            "bundle_sha256",
            "dependencies",
        },
    )
    expected_manifest = {
        "schema": 2,
        "source_provider": "gitcode",
        "test_mode": "merge_result",
        **source_identity(plan),
        "github_run_id": gh["run_id"],
        "run_attempt": gh["run_attempt"],
        "source_artifact_id": refs["source"]["id"],
        "bundle_sha256": refs["source"]["content_sha256"],
        "dependencies": manifest["dependencies"],
    }
    if (
        not isinstance(manifest["dependencies"], list)
        or len(manifest["dependencies"]) > 256
    ):
        raise ContractError("INVALID_DEPENDENCY_MANIFEST")
    if (
        canonical(manifest) != canonical(expected_manifest)
        or digest(manifest) != refs["manifest"]["content_sha256"]
        or control_runner(plan) != refs["runner"]["content_sha256"]
    ):
        raise ContractError("SOURCE_MANIFEST_MISMATCH")
    expected_intent = {
        "schema": 1,
        "source_provider": "gitcode",
        "pr": plan["pr"],
        "control_repo_id": gh["repo_id"],
        "control_sha": plan["control"]["control_sha"],
        "workflow_path": gh["workflow_path"],
        "pipeline": plan["pipeline"],
        "selectors": selectors,
        "bridge": plan["bridge"],
        "deadline": plan["deadline"],
    }
    if (
        canonical(intent) != canonical(expected_intent)
        or digest(intent) != refs["intent"]["content_sha256"]
    ):
        raise ContractError("DISPATCH_INTENT_MISMATCH")


def control_runner(plan):
    return plan["control"]["runner_sha256"]


def _verify_slots(plan, app_id, observations):
    cases = planned_slot_ids(plan)
    shape(plan["result_slots"], cases)
    shape(observations, cases)
    seen = set()
    for case_id, slot in plan["result_slots"].items():
        shape(
            slot,
            {
                "id",
                "app_id",
                "repo_id",
                "control_sha",
                "run_id",
                "run_attempt",
                "case_id",
                "external_id",
            },
        )
        positive(slot["id"])
        gh = plan["github"]
        expected = {
            "id": slot["id"],
            "app_id": app_id,
            "repo_id": gh["repo_id"],
            "control_sha": plan["control"]["control_sha"],
            "run_id": gh["run_id"],
            "run_attempt": gh["run_attempt"],
            "case_id": case_id,
            "external_id": external_id(plan, case_id),
        }
        if (
            slot["id"] in seen
            or canonical(slot) != canonical(expected)
            or canonical(observations[case_id]) != canonical(expected)
        ):
            raise ContractError("RESULT_SLOT_IDENTITY_MISMATCH")
        seen.add(slot["id"])


def validate_receipt(plan: dict, receipt: dict) -> None:
    shape(receipt, RECEIPT_KEYS)
    for key in ("pr_generation", "execution_generation", "publication_revision"):
        positive(receipt[key])
    if type(receipt["duplicate"]) is not bool:
        raise ContractError("INVALID_RECEIPT")
    text(receipt["canonical_execution_id"], limit=100)
    expected = {
        "request_id": request_id(plan),
        "plan_digest": plan["plan_digest"],
        "request_spec_digest": plan["request_spec_digest"],
        "source_snapshot_id": plan["bridge"]["source_snapshot_id"],
        "dispatch_intent_id": plan["bridge"]["dispatch_intent_id"],
    }
    if any(receipt.get(k) != v for k, v in expected.items()):
        raise ContractError("ADMISSION_RECEIPT_MISMATCH")


def aggregate_results(
    plan: dict, receipt: dict, results: list[dict], *, current: bool
) -> dict:
    """Versioned test status only: never manufacture an atomic merge permission."""
    validate_receipt(plan, receipt)
    if receipt["duplicate"]:
        raise ContractError("FOLLOW_CANONICAL_RECEIPT_FOR_DUPLICATE")
    expected = {c["case_id"] for c in plan["cases"]}
    seen, states = set(), []
    for result in results:
        shape(
            result,
            {
                "case_id",
                "receipt",
                "identity",
                "status",
                "exit_code",
                "cleanup_verified",
                "summary",
            },
        )
        case_id = result["case_id"]
        if case_id not in expected or case_id in seen:
            raise ContractError("CONFLICTING_OR_UNPLANNED_RESULT")
        seen.add(case_id)
        if canonical(result["receipt"]) != canonical(receipt) or canonical(
            result["identity"]
        ) != canonical(source_identity(plan)):
            raise ContractError("RESULT_EXECUTION_IDENTITY_MISMATCH")
        if type(result["cleanup_verified"]) is not bool or result["status"] not in {
            "PASS",
            "FAIL",
            "INFRA_ERROR",
            "CANCELLED",
            "TIMED_OUT",
            "NOT_RUN",
            "RESOURCE_TIMEOUT",
        }:
            raise ContractError("INVALID_CASE_RESULT")
        if (
            type(result["exit_code"]) is not int
            or not -255 <= result["exit_code"] <= 255
        ):
            raise ContractError("INVALID_CASE_EXIT_CODE")
        if result["status"] == "PASS" and result["exit_code"] != 0:
            raise ContractError("PASS_EXIT_CODE_MISMATCH")
        if (
            not isinstance(result["summary"], list)
            or len(result["summary"]) > 20
            or any(
                not isinstance(x, str) or len(x) > 180 or any(ord(c) < 32 for c in x)
                for x in result["summary"]
            )
        ):
            raise ContractError("RESULT_DIAGNOSTIC_LIMIT")
        canonical(result, limit=32 * 1024)
        states.append(result["status"] == "PASS" and result["cleanup_verified"])
    status = (
        "STALE"
        if not current
        else ("PENDING" if seen != expected else ("PASS" if all(states) else "FAIL"))
    )
    return {
        "test_result": status,
        "merge_gate": "NOT_PROVIDED",
        "merge_gate_mode": "versioned_only",
        "identity": source_identity(plan),
        "receipt": receipt,
    }
