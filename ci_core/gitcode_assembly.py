"""Reviewed schema-4 dependency assembly; no implicit enablement or credential fallback.

Configuration is trusted deployment JSON, never a Plan or proposed source file.
Bridge and Blue must share their durable authority journal. Ephemeral Hosted
runners can produce/read immutable artifacts, but cannot invent a shared current-
generation proof: Hosted current-authority transport remains explicitly required.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ci_core.gitcode_admission import GitCodeAdmissionBroker
from ci_core.gitcode_api import GitCodeClient, repository_id
from ci_core.gitcode_app import (
    API_VERSION,
    ChecksAppClient,
    InstallationTokens,
    OpenSSLSigner,
    ReadOnlyChecksClient,
)
from ci_core.gitcode_artifacts import JSON_LIMIT, GitCodeArtifactReader
from ci_core.gitcode_bridge import DispatchJournal, GitCodeBridge
from ci_core.gitcode_checks import CheckRunChannel, CreationJournal
from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    STAGING_GITHUB_REPO_ID,
    TORCHTITAN_REPOSITORY_IDS,
    ControlPolicy,
    canonical,
    load_json,
    positive,
    request_id,
    select_cases,
    shape,
    text,
)
from ci_core.gitcode_interfaces import CASES
from ci_core.gitcode_producer import GitCodeProducer
from ci_core.gitcode_runtime import GitCodeRouterAdapter
from ci_core.gitcode_source import require_sha
from ci_core.gitcode_state import Coordinator
from ci_core.gitcode_store import GitCodeRequestStore
from ci_core.github import GitHubClient


class AssemblyError(ValueError):
    pass


CONFIG_KEYS = {
    "schema",
    "sdk_sha",
    "authority_mode",
    "policy",
    "catalog",
    "personal_repo",
    "upstream_repo",
    "github_actor",
    "checks_installation_id",
}


def load_release(path: Path) -> dict:
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise AssemblyError("TRUSTED_ABSOLUTE_CONFIG_PATH_REQUIRED")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or not 0 < info.st_size <= JSON_LIMIT
    ):
        raise AssemblyError("TRUSTED_CONFIG_OWNERSHIP_OR_MODE_INVALID")
    value = load_json(path.read_bytes(), limit=JSON_LIMIT)
    shape(
        value,
        CONFIG_KEYS
        | (
            set(value)
            & {"publication", "authority_protocol", "attestation", "manual_capture"}
        ),
    )
    if value.get("authority_protocol") not in (None, "artifact-check-v1"):
        raise AssemblyError("UNKNOWN_AUTHORITY_PROTOCOL")
    publication = value.get("publication", {"enabled": False})
    if (
        not isinstance(publication, dict)
        or type(publication.get("enabled")) is not bool
    ):
        raise AssemblyError("EXPLICIT_PUBLICATION_POLICY_REQUIRED")
    shape(publication, {"enabled", "actor"} if publication["enabled"] else {"enabled"})
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise AssemblyError("UNKNOWN_ASSEMBLY_SCHEMA")
    require_sha(value["sdk_sha"])
    if value["authority_mode"] != "observed_history_versioned_only":
        raise AssemblyError("UNREVIEWED_AUTHORITY_MODE")
    raw = value["policy"]
    if not isinstance(raw, dict):
        raise AssemblyError("CONTROL_POLICY_REQUIRED")
    for key in ("allowed_actors", "allowed_base_refs"):
        if not isinstance(raw.get(key), list) or not raw[key]:
            raise AssemblyError("POLICY_AUTHORIZATION_LIST_REQUIRED")
    try:
        policy = ControlPolicy(
            **{
                **raw,
                "allowed_actors": tuple(raw["allowed_actors"]),
                "allowed_base_refs": tuple(raw["allowed_base_refs"]),
                "allowed_head_repositories": tuple(
                    tuple(item) for item in raw.get("allowed_head_repositories", [])
                ),
            }
        )
    except TypeError:
        raise AssemblyError("INVALID_CONTROL_POLICY_FIELDS") from None
    if policy.github_repo_id not in TORCHTITAN_REPOSITORY_IDS or not policy.enabled:
        raise AssemblyError("EXPLICIT_TORCHTITAN_RELEASE_REQUIRED")
    if policy.github_repo_id == STAGING_GITHUB_REPO_ID and (
        value["personal_repo"] != "depeng1994/torchtitan-npu"
        or value["upstream_repo"] != "depeng1994/torchtitan-npu"
    ):
        raise AssemblyError("PERSONAL_STAGING_REPOSITORY_PATH_REQUIRED")
    repository_id(policy.gitcode_repo_id)
    repository_id(policy.upstream_repo_id)
    positive(value["checks_installation_id"])
    if value["github_actor"] not in policy.allowed_actors:
        raise AssemblyError("DISPATCH_ACTOR_NOT_AUTHORIZED")
    catalog = value["catalog"]
    if not isinstance(catalog, dict) or not catalog or set(catalog) - CASES:
        raise AssemblyError("TRUSTED_TORCHTITAN_CATALOG_REQUIRED")
    for actor in policy.allowed_actors:
        text(actor, limit=100)
    select_cases([{"test_id": name} for name in catalog], catalog)
    attestation = value.get("attestation", {"enabled": False})
    if (
        not isinstance(attestation, dict)
        or type(attestation.get("enabled")) is not bool
    ):
        raise AssemblyError("EXPLICIT_ATTESTATION_POLICY_REQUIRED")
    attestation_policy = None
    if not attestation["enabled"]:
        shape(attestation, {"enabled"})
    else:
        from ci_core.gitcode_api import REPOSITORY
        from ci_core.gitcode_attestation import AttestationPolicy

        required = {
            "enabled",
            "key_id",
            "public_key_file",
            "public_key_sha256",
            "issuer_repo",
            "issuer_repo_id",
            "head_repo_id",
            "workflow_path",
            "workflow_source_sha",
            "workflow_id",
            "allowed_event_kinds",
            "allowed_platform_events",
        }
        optional = {
            "allowed_run_statuses",
            "max_original_event_age_seconds",
            "clock_skew_seconds",
            "allow_external_issuer",
        }
        shape(attestation, required | (set(attestation) & optional))
        if not isinstance(attestation["issuer_repo"], str) or not REPOSITORY.fullmatch(
            attestation["issuer_repo"]
        ):
            raise AssemblyError("PINNED_ATTESTATION_ISSUER_REPOSITORY_REQUIRED")
        values = {
            key: value for key, value in attestation.items() if key != "issuer_repo"
        }
        for key in (
            "allowed_event_kinds",
            "allowed_platform_events",
            "allowed_run_statuses",
        ):
            if key in values:
                if not isinstance(values[key], list):
                    raise AssemblyError("ATTESTATION_ENUM_LIST_REQUIRED")
                values[key] = tuple(values[key])
        values["public_key_file"] = Path(values["public_key_file"])
        attestation_policy = AttestationPolicy(control=policy, **values)
        allowed_heads = {policy.gitcode_repo_id} | {
            ident for _, ident in policy.allowed_head_repositories
        }
        if attestation_policy.head_repo_id not in allowed_heads:
            raise AssemblyError("ATTESTATION_HEAD_NOT_IN_CONTROL_POLICY")
    manual = value.get("manual_capture", {"enabled": False})
    if not isinstance(manual, dict) or type(manual.get("enabled")) is not bool:
        raise AssemblyError("EXPLICIT_MANUAL_CAPTURE_POLICY_REQUIRED")
    manual_policy = None
    if not manual["enabled"]:
        shape(manual, {"enabled"})
    else:
        from ci_core.gitcode_manual_capture import ManualCapturePolicy

        required = {
            "enabled",
            "workflow_id",
            "workflow_path",
            "job_identifier",
            "step_identifier",
            "step_name",
            "script_sha256",
            "shell",
            "working_directory",
            "request_authority",
        }
        optional = {
            "max_capture_age_seconds",
            "clock_skew_seconds",
            "allowed_run_statuses",
        }
        shape(manual, required | (set(manual) & optional))
        values = dict(manual)
        if "allowed_run_statuses" in values:
            if not isinstance(values["allowed_run_statuses"], list):
                raise AssemblyError("MANUAL_STATUS_LIST_REQUIRED")
            values["allowed_run_statuses"] = tuple(values["allowed_run_statuses"])
        manual_policy = ManualCapturePolicy(control=policy, **values)
        if attestation_policy is not None:
            raise AssemblyError("SOURCE_AUTHORITY_MODES_MUST_NOT_MIX")
    return {
        **value,
        "policy": policy,
        "attestation_policy": attestation_policy,
        "manual_capture_policy": manual_policy,
    }


def _state_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise AssemblyError("ABSOLUTE_NON_SYMLINK_STATE_DIRECTORY_REQUIRED")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise AssemblyError("OWNER_ONLY_STATE_DIRECTORY_REQUIRED")
    return path


def _bind_authority_directory(root, config):
    binding = {
        "schema": 1,
        "gitcode_repo_id": config["policy"].gitcode_repo_id,
        "personal_repo": config["personal_repo"],
        "authority_mode": config["authority_mode"],
        "github_repo_id": config["policy"].github_repo_id,
        "deployment_profile": config["policy"].deployment_profile,
    }
    path = root / "authority-binding.json"
    try:
        with path.open("xb") as stream:
            stream.write(canonical(binding))
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
    except FileExistsError:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or canonical(load_json(path.read_bytes())) != canonical(binding)
        ):
            raise AssemblyError("AUTHORITY_DIRECTORY_BINDING_MISMATCH") from None


def _secret(environment, name):
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16384
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise AssemblyError("REQUIRED_CONTROL_CREDENTIAL_UNAVAILABLE")
    return value


@dataclass(repr=False)
class AssembledRuntime:
    policy: ControlPolicy
    producer: GitCodeProducer | None
    reader: GitCodeArtifactReader
    bridge: GitCodeBridge
    authority: object
    state_directory: Path
    clock: object
    role: str
    publisher: object = None
    coordinator: object = None
    hosted_current: object = None
    authority_protocol: str | None = None

    def current(self, plan):
        if self.authority is None:
            if self.hosted_current is None:
                raise AssemblyError("HOSTED_CURRENT_AUTHORITY_TRANSPORT_REQUIRED")
            return self.hosted_current(plan)
        if (
            plan["control"]["control_sha"] != self.policy.control_sha
            or plan["github"]["workflow_id"] != self.policy.workflow_id
        ):
            raise AssemblyError("CURRENT_REQUEST_RELEASE_MISMATCH")
        self.reader._actual_run(plan["github"]["run_id"], allow_completed=True)
        proof = self.authority.verify_plan(plan)
        authority = self.authority.read_authority(
            plan["pr"]["repo_id"], plan["pr"]["number"]
        )
        if not (
            authority.state == "open"
            and authority.authorized
            and canonical(authority.pr) == canonical(plan["pr"])
            and authority.revision == proof.version_revision
        ):
            return False
        if self.coordinator is None:
            raise AssemblyError("SHARED_COORDINATOR_AUTHORITY_REQUIRED")
        # PR freshness alone cannot prove current selector/rerun publication
        # ownership. Use the exact retained receipt and transactional generation.
        self.coordinator.reconcile(
            plan["pr"]["repo_id"],
            plan["pr"]["number"],
            lambda: authority,
            now=int(self.clock()),
        )
        reference = self.coordinator.request_reference(request_id(plan))
        envelope = self.reader.channel.read_receipt(plan)
        if (
            reference["plan_digest"] != plan["plan_digest"]
            or envelope is None
            or canonical(envelope["receipt"]) != canonical(reference["receipt"])
        ):
            raise AssemblyError("CURRENT_COORDINATOR_RECEIPT_MISMATCH")
        self.coordinator.assert_startable(
            reference["execution_id"], now=int(self.clock())
        )
        return True

    def resolve_canonical(self, pointer):
        shape(pointer, {"request_id", "plan_digest", "github", "result_slots"})
        verified = self.reader.load_results(
            positive(pointer["github"]["run_id"]), now=int(self.clock())
        )
        if verified is None:
            raise AssemblyError("CANONICAL_REQUEST_NOT_YET_AVAILABLE")
        plan = verified.plan
        expected = {
            "request_id": request_id(plan),
            "plan_digest": plan["plan_digest"],
            "github": plan["github"],
            "result_slots": plan["result_slots"],
        }
        if canonical(expected) != canonical(pointer):
            raise AssemblyError("CANONICAL_REQUEST_POINTER_MISMATCH")
        return verified

    def cancel_run(self, run_id):
        run_id = positive(run_id)
        run = self.reader._actual_run(run_id, allow_completed=True)
        if run["status"] == "completed":
            return True
        self.reader.github.request("POST", f"/actions/runs/{run_id}/cancel")
        return True

    def publish_result(self, plan, outcome):
        if self.publisher is None:
            raise AssemblyError("GITCODE_VERSIONED_PUBLICATION_ADAPTER_REQUIRED")
        verified = self.reader.load_results(
            plan["github"]["run_id"], now=int(self.clock())
        )
        if verified is None or canonical(verified.plan) != canonical(plan):
            raise AssemblyError("PUBLICATION_VERIFIED_PLAN_MISMATCH")
        envelope = self.reader.channel.read_receipt(plan)
        if envelope is None or canonical(envelope["receipt"]) != canonical(
            outcome.get("receipt")
        ):
            raise AssemblyError("PUBLICATION_RECEIPT_MISMATCH")
        receipt = envelope["receipt"]
        results = [
            self.reader.channel.read_case(plan, receipt, case["case_id"])
            for case in plan["cases"]
        ]
        return self.publisher.publish(
            verified, receipt, results, current=lambda: self.current(plan)
        )


def _assemble(
    config_path, *, environment, upload, state_directory, role, clock=time.time
):
    config = load_release(config_path)
    root = _state_directory(state_directory)
    policy = config["policy"]
    phase = environment.get("LITE_ACTIONS_HOSTED_PHASE") if role == "hosted" else role
    if role == "hosted" and phase not in {"prepare", "wait"}:
        raise AssemblyError("TRUSTED_HOSTED_PHASE_REQUIRED")
    checks_writer = role == "blue" or phase == "prepare"
    github = GitHubClient(
        _secret(environment, "LITE_ACTIONS_GITHUB_TOKEN"),
        MODEL_REPOSITORIES[policy.github_repo_id][0],
        api_version=API_VERSION,
        schema4_repo_id=policy.github_repo_id,
    )
    token = _secret(environment, "LITE_ACTIONS_GITCODE_TOKEN")
    personal = GitCodeClient(
        config["personal_repo"],
        policy.gitcode_repo_id,
        token=token,
        allowed_head_repositories=dict(policy.allowed_head_repositories),
    )
    upstream = GitCodeClient(
        config["upstream_repo"], policy.upstream_repo_id, token=token
    )
    release = _state_directory(root / policy.control_sha / str(policy.workflow_id))
    if checks_writer:
        key_path = environment.get("LITE_ACTIONS_CHECKS_KEY_FILE")
        if not isinstance(key_path, str) or not key_path:
            raise AssemblyError("CHECKS_APP_KEY_PATH_REQUIRED")
        tokens = InstallationTokens(
            app_id=policy.app_id,
            installation_id=config["checks_installation_id"],
            repo_id=policy.github_repo_id,
            signer=OpenSSLSigner(Path(key_path)),
            clock=clock,
        )
        checks_client = ChecksAppClient(tokens)
    else:
        checks_client = ReadOnlyChecksClient(github, app_id=policy.app_id)
    channel = CheckRunChannel(
        checks_client,
        repo_id=policy.github_repo_id,
        app_id=policy.app_id,
        journal=CreationJournal(release / "checks.sqlite3") if checks_writer else None,
    )
    reader = GitCodeArtifactReader(
        policy=policy,
        github=github,
        personal=personal,
        channel=channel,
        catalog=config["catalog"],
    )
    bridge = GitCodeBridge(
        policy=policy,
        github=github,
        personal=personal,
        upstream=upstream,
        journal=DispatchJournal(release / "dispatch.sqlite3"),
        catalog=config["catalog"],
        github_actor=config["github_actor"],
    )
    producer = None
    if role == "hosted" and phase == "prepare":
        producer = GitCodeProducer(
            policy=policy,
            catalog=config["catalog"],
            github=github,
            personal=personal,
            upstream=upstream,
            channel=channel,
            artifact_journal=CreationJournal(release / "artifacts.sqlite3"),
            upload=upload,
            clock=clock,
            authority_protocol=config.get("authority_protocol"),
        )
    authority = None
    if role in {"blue", "bridge"}:
        if (root / "authority.sqlite3").exists():
            raise AssemblyError("LEGACY_SEPARATE_AUTHORITY_LEDGER_MIGRATION_REQUIRED")
        _bind_authority_directory(root, config)
        from ci_core.gitcode_platform import ObservedGitCodeAuthority

        attestation_policy = config["attestation_policy"]
        action_client = None
        if attestation_policy is not None:
            action_client = GitCodeClient(
                config["attestation"]["issuer_repo"],
                attestation_policy.issuer_repo_id,
                token=token,
            )
        shared_coordinator = Coordinator(root / "coordinator.sqlite3")
        authority = ObservedGitCodeAuthority(
            personal,
            policy,
            config["catalog"],
            root / "coordinator.sqlite3",
            clock=clock,
            coordinator=shared_coordinator,
            attestation_policy=attestation_policy,
            manual_capture_policy=config["manual_capture_policy"],
            action_client=action_client,
        )
        reader.read_snapshot = authority.read_snapshot
    publisher = None
    publication = config.get("publication", {"enabled": False})
    if publication["enabled"] and role in {"blue", "bridge"}:
        from ci_core.gitcode_publication import (
            GitCodeResultClient,
            VersionedGitCodePublisher,
        )

        actor = text(publication["actor"], limit=100)
        if actor not in policy.allowed_actors:
            raise AssemblyError("PUBLICATION_ACTOR_NOT_AUTHORIZED")
        result_client = GitCodeResultClient(
            config["personal_repo"],
            policy.gitcode_repo_id,
            token=_secret(environment, "LITE_ACTIONS_GITCODE_PUBLICATION_TOKEN"),
            allow_comment_publication=True,
        )
        publisher = VersionedGitCodePublisher(
            result_client,
            CreationJournal(root / "gitcode-publication.sqlite3"),
            actor=actor,
        )
    runtime = AssembledRuntime(
        policy, producer, reader, bridge, authority, root, clock, role, publisher
    )
    runtime.authority_protocol = config.get("authority_protocol")
    if role in {"blue", "bridge"}:
        runtime.coordinator = authority.coordinator
    if role == "hosted" and runtime.authority_protocol is not None:
        from ci_core.gitcode_authority_channel import HostedCurrent

        run_id = environment.get("GITHUB_RUN_ID")
        if (
            not isinstance(run_id, str)
            or not run_id.isascii()
            or not run_id.isdecimal()
        ):
            raise AssemblyError("HOSTED_AUTHORITY_RUN_ID_REQUIRED")
        runtime.hosted_current = HostedCurrent(
            reader,
            channel,
            upload,
            state_directory=root / "authority-client",
            owner_run_id=positive(int(run_id)),
            clock=clock,
        )
    return runtime


def assemble_hosted(config_path, *, environment, upload, state_directory):
    return _assemble(
        config_path,
        environment=environment,
        upload=upload,
        state_directory=state_directory,
        role="hosted",
    )


def assemble_bridge(config_path, *, environment, upload, state_directory):
    return _assemble(
        config_path,
        environment=environment,
        upload=upload,
        state_directory=state_directory,
        role="bridge",
    )


def assemble_blue(config_paths, *, environment, state_directory):
    if not config_paths:
        raise AssemblyError("APPROVED_BLUE_RELEASE_CONFIG_REQUIRED")
    # Validate every release and shared source identity before opening coordinator
    # storage (which may require a version migration).
    configurations = [load_release(path) for path in config_paths]
    bindings = {
        (
            config["personal_repo"],
            config["policy"].gitcode_repo_id,
            config["policy"].github_repo_id,
            config["policy"].deployment_profile,
        )
        for config in configurations
    }
    if len(bindings) != 1:
        raise AssemblyError("BLUE_AUTHORITY_REPOSITORY_MISMATCH")

    def no_upload(*args):
        raise AssemblyError("BLUE_CANNOT_UPLOAD_HOSTED_ARTIFACTS")

    root = _state_directory(state_directory)
    runtimes = [
        _assemble(
            config_path,
            environment=environment,
            upload=no_upload,
            state_directory=root,
            role="blue",
        )
        for config_path in config_paths
    ]
    coordinator = Coordinator(root / "coordinator.sqlite3")
    store = GitCodeRequestStore(root / "requests")

    def enroll_for(runtime):
        def enroll(intent, run):
            if runtime.authority.manual_capture_policy is not None:
                runtime.authority.enroll_manual(intent, github_run=run)
                return True
            envelope = runtime.reader.early_attestation(run["id"])
            if envelope is None:
                return False
            runtime.authority.enroll_attested(intent, envelope, github_run=run)
            return True

        return enroll

    brokers = []
    for runtime in runtimes:
        runtime.coordinator = coordinator
        brokers.append(
            GitCodeAdmissionBroker(
                reader=runtime.reader,
                coordinator=coordinator,
                store=store,
                workspace=root / "admission",
                read_authority=runtime.authority.read_authority,
                verify_event=runtime.authority.verify_event,
                verify_plan=runtime.authority.verify_plan,
                enroll_intent=enroll_for(runtime)
                if (
                    runtime.authority.attestation_policy is not None
                    or runtime.authority.manual_capture_policy is not None
                )
                else None,
                clock=lambda: int(time.time()),
            )
        )
    publishers = {
        (
            runtime.policy.github_repo_id,
            runtime.policy.control_sha,
            runtime.policy.workflow_id,
        ): runtime.publisher
        for runtime in runtimes
        if runtime.publisher is not None
    }
    responders = {}
    for runtime, broker in zip(runtimes, brokers):
        if runtime.authority_protocol is not None:
            from ci_core.gitcode_authority_channel import AuthorityResponder

            responders[
                (
                    runtime.policy.github_repo_id,
                    runtime.policy.control_sha,
                    runtime.policy.workflow_id,
                )
            ] = AuthorityResponder(
                runtime.reader,
                runtime.reader.channel,
                resolve_request=broker.resolve_request,
                cleanup_journal=CreationJournal(root / "authority-closures.sqlite3"),
            )
    return GitCodeRouterAdapter(brokers, publishers=publishers, responders=responders)
