"""Disabled-by-default trusted GitCode workflow attestation candidate.

A signature authenticates the reviewed emitter, not platform event freshness by
itself. Original event provenance must first be established by live P0 evidence.
Blue uses only a pinned public key; a dedicated emitter private key must never be
the Checks App key. Replay, observed revocations and latest-wins are ledger gates
outside this module. No live key is created, configured or read by import.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ci_core.gitcode_api import repository_id
from ci_core.gitcode_contract import (
    BRIDGE_KEYS,
    PR_KEYS,
    ControlPolicy,
    canonical,
    hex_digest,
    load_json,
    positive,
    shape,
    text,
)
from ci_core.gitcode_source import require_ref, require_sha

PROTOCOL = "lite-actions-gitcode-attestation-v1"
DOMAIN = PROTOCOL.encode("ascii") + b"\x00"
MAX_ENVELOPE = 64 * 1024
MAX_INTENT = 16 * 1024
INTENT_KEYS = {
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
}


class AttestationError(ValueError):
    pass


def _platform_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise AttestationError("INVALID_GITCODE_PLATFORM_ID")
    return value


def _repo_id(value):
    if not isinstance(value, str):
        raise AttestationError("STRING_REPOSITORY_ID_REQUIRED")
    return repository_id(value)


def _workflow_path(value):
    value = text(value, limit=256)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or not value.startswith(".gitcode/workflows/")
        or path.suffix not in {".yml", ".yaml"}
    ):
        raise AttestationError("TRUSTED_GITCODE_WORKFLOW_PATH_REQUIRED")
    return value


@dataclass(frozen=True)
class AttestationPolicy:
    control: ControlPolicy
    key_id: str
    public_key_file: Path
    public_key_sha256: str
    issuer_repo_id: str
    head_repo_id: str
    workflow_path: str
    workflow_source_sha: str
    workflow_id: str
    allowed_event_kinds: tuple[str, ...]
    allowed_platform_events: tuple[str, ...]
    allowed_run_statuses: tuple[str, ...] = ("RUNNING",)
    allow_external_issuer: bool = False
    max_original_event_age_seconds: int = 300
    clock_skew_seconds: int = 30
    enabled: bool = False

    def __post_init__(self):
        if not isinstance(self.control, ControlPolicy):
            raise AttestationError("REVIEWED_CONTROL_POLICY_REQUIRED")
        if not isinstance(self.key_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", self.key_id
        ):
            raise AttestationError("INVALID_ISSUER_KEY_ID")
        hex_digest(self.public_key_sha256)
        _repo_id(self.issuer_repo_id)
        _repo_id(self.head_repo_id)
        if type(self.allow_external_issuer) is not bool or (
            self.issuer_repo_id != self.control.gitcode_repo_id
            and not self.allow_external_issuer
        ):
            raise AttestationError("TRUSTED_TARGET_ISSUER_REQUIRED")
        _workflow_path(self.workflow_path)
        require_sha(self.workflow_source_sha)
        _platform_id(self.workflow_id)
        if (
            type(self.max_original_event_age_seconds) is not int
            or not 1 <= self.max_original_event_age_seconds <= 3600
            or type(self.clock_skew_seconds) is not int
            or not 0 <= self.clock_skew_seconds <= 60
            or type(self.enabled) is not bool
            or not isinstance(self.allowed_event_kinds, tuple)
            or not self.allowed_event_kinds
            or len(set(self.allowed_event_kinds)) != len(self.allowed_event_kinds)
        ):
            raise AttestationError("INVALID_ATTESTATION_POLICY")
        for kind in self.allowed_event_kinds:
            text(kind, limit=80)
        for values in (self.allowed_platform_events, self.allowed_run_statuses):
            if (
                not isinstance(values, tuple)
                or not values
                or len(set(values)) != len(values)
            ):
                raise AttestationError("PINNED_ACTION_API_ENUMS_REQUIRED")
            for value in values:
                if not isinstance(value, str) or not re.fullmatch(
                    r"[A-Z][A-Z0-9_]{0,79}", value
                ):
                    raise AttestationError("INVALID_ACTION_API_ENUM")


@dataclass(frozen=True)
class VerifiedAttestation:
    key_id: str
    issuer_repo_id: str
    original_event_id: str
    original_event_at: int
    source_snapshot_id: str
    source_snapshot: dict
    workflow_source_sha: str
    workflow_path: str
    workflow_id: str
    gitcode_run_id: str
    attempt: int
    capture_job_id: str
    source_key: str
    run_head_sha: str
    run_head_branch: str
    platform_event: str
    actor: str
    trigger_kind: str
    nonce: str
    issued_at: int
    envelope_digest: str


def _binding(payload, intent):
    shape(
        payload,
        {
            "issuer",
            "workflow",
            "action",
            "original_event",
            "nonce",
            "issued_at",
            "intent",
            "intent_digest",
        },
    )
    shape(intent, INTENT_KEYS)
    if (
        type(intent["schema"]) is not int
        or intent["schema"] != 1
        or intent["source_provider"] != "gitcode"
    ):
        raise AttestationError("INVALID_SIGNED_INTENT_PROTOCOL")
    canonical(intent, limit=MAX_INTENT)
    if (
        canonical(payload["intent"], limit=MAX_INTENT)
        != canonical(intent, limit=MAX_INTENT)
        or payload["intent_digest"] != hashlib.sha256(canonical(intent)).hexdigest()
    ):
        raise AttestationError("SIGNED_INTENT_IDENTITY_MISMATCH")
    shape(intent["pr"], PR_KEYS)
    pr = intent["pr"]
    for field in ("repo_id", "head_repo_id", "base_repo_id"):
        _repo_id(pr[field])
    positive(pr["number"])
    for field in ("head_sha", "base_sha"):
        require_sha(pr[field])
    for field in ("head_ref", "base_ref"):
        require_ref(pr[field])
    shape(intent["bridge"], BRIDGE_KEYS)
    bridge = intent["bridge"]
    for key, value in bridge.items():
        if key in {"bridge_attempt", "event_created_at"}:
            positive(value)
        else:
            text(value, limit=160)
    selectors = intent["selectors"]
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 64:
        raise AttestationError("INVALID_SIGNED_SELECTORS")
    names = []
    for selector in selectors:
        shape(selector, {"test_id"})
        names.append(text(selector["test_id"], limit=80))
    if len(set(names)) != len(names):
        raise AttestationError("DUPLICATE_SIGNED_SELECTOR")
    text(intent["pipeline"], limit=30)
    shape(payload["issuer"], {"key_id", "repo_id", "actor"})
    shape(payload["workflow"], {"id", "path", "source_sha"})
    shape(
        payload["action"],
        {
            "run_id",
            "attempt",
            "capture_job_id",
            "head_sha",
            "head_branch",
            "platform_event",
        },
    )
    shape(
        payload["original_event"],
        {
            "id",
            "created_at",
            "event_kind",
            "source_snapshot_id",
            "source_snapshot",
        },
    )
    issuer, workflow, action, event = (
        payload["issuer"],
        payload["workflow"],
        payload["action"],
        payload["original_event"],
    )
    if not isinstance(issuer["key_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", issuer["key_id"]
    ):
        raise AttestationError("INVALID_ISSUER_KEY_ID")
    _repo_id(issuer["repo_id"])
    text(issuer["actor"], limit=100)
    _platform_id(workflow["id"])
    _workflow_path(workflow["path"])
    require_sha(workflow["source_sha"])
    _platform_id(action["run_id"])
    positive(action["attempt"])
    _platform_id(action["capture_job_id"])
    require_sha(action["head_sha"])
    require_ref(action["head_branch"])
    if not isinstance(action["platform_event"], str) or not re.fullmatch(
        r"[A-Z][A-Z0-9_]{0,79}", action["platform_event"]
    ):
        raise AttestationError("INVALID_PLATFORM_EVENT")
    positive(event["created_at"])
    positive(payload["issued_at"])
    positive(intent["deadline"])
    for field in ("id", "source_snapshot_id"):
        text(event[field], limit=160)
    text(event["event_kind"], limit=80)
    if not isinstance(payload["nonce"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{16,100}", payload["nonce"]
    ):
        raise AttestationError("INVALID_ATTESTATION_NONCE")
    if (
        canonical(event["source_snapshot"]) != canonical(pr)
        or event["source_snapshot_id"] != bridge["source_snapshot_id"]
        or event["id"] != bridge["bridge_event_id"]
        or event["created_at"] != bridge["event_created_at"]
        or action["run_id"] != bridge["bridge_action_run"]
        or type(bridge["bridge_attempt"]) is not int
        or action["attempt"] != bridge["bridge_attempt"]
        or issuer["actor"] != bridge["actor"]
        or payload["nonce"] != bridge["bridge_nonce"]
        or event["created_at"] > payload["issued_at"]
        or not event["created_at"] < intent["deadline"]
    ):
        raise AttestationError("ORIGINAL_EVENT_OR_ACTION_BINDING_MISMATCH")


def create_envelope(
    intent,
    *,
    original_event,
    run_id,
    run_attempt,
    capture_job_id,
    run_head_sha,
    run_head_branch,
    platform_event,
    workflow_id,
    workflow_path,
    workflow_source_sha,
    key_id,
    issuer_repo_id,
    actor,
    issued_at,
    signer,
    max_original_event_age_seconds=300,
):
    """Sign only explicitly supplied original runtime metadata; never invent it.

    The caller must be the pinned trusted workflow. It must not fill missing
    fields from wall-clock time, current PR API snapshots, or proposed PR code.
    This helper is not wired to an automatic workflow by default.
    """
    payload = {
        "issuer": {"key_id": key_id, "repo_id": issuer_repo_id, "actor": actor},
        "workflow": {
            "id": workflow_id,
            "path": workflow_path,
            "source_sha": workflow_source_sha,
        },
        "action": {
            "run_id": run_id,
            "attempt": run_attempt,
            "capture_job_id": capture_job_id,
            "head_sha": run_head_sha,
            "head_branch": run_head_branch,
            "platform_event": platform_event,
        },
        "original_event": original_event,
        "nonce": intent.get("bridge", {}).get("bridge_nonce"),
        "issued_at": issued_at,
        "intent": intent,
        "intent_digest": hashlib.sha256(
            canonical(intent, limit=MAX_INTENT)
        ).hexdigest(),
    }
    _binding(payload, intent)
    if (
        type(max_original_event_age_seconds) is not int
        or not 1 <= max_original_event_age_seconds <= 3600
        or issued_at - original_event["created_at"] > max_original_event_age_seconds
    ):
        raise AttestationError("ORIGINAL_EVENT_TOO_OLD_TO_SIGN")
    if not callable(signer):
        raise AttestationError("DEDICATED_SIGNER_REQUIRED")
    signature = signer(DOMAIN + canonical(payload, limit=MAX_ENVELOPE))
    if not isinstance(signature, bytes) or not 256 <= len(signature) <= 1024:
        raise AttestationError("RSA_2048_OR_STRONGER_SIGNATURE_REQUIRED")
    envelope = {
        "schema": 1,
        "protocol": PROTOCOL,
        "algorithm": "RS256",
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    # Copy caller-owned nested data; never return mutable aliases to the intent.
    return load_json(canonical(envelope, limit=MAX_ENVELOPE), limit=MAX_ENVELOPE)


def _verify_signature(policy, signature, message):
    path = Path(policy.public_key_file)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise AttestationError("PINNED_PUBLIC_KEY_PATH_REQUIRED")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or not 1 <= info.st_size <= 16384
        ):
            raise AttestationError("PUBLIC_KEY_OWNERSHIP_OR_MODE_INVALID")
        public = os.read(fd, 16385)
        if (
            hashlib.sha256(public).hexdigest() != policy.public_key_sha256
            or not public.startswith(b"-----BEGIN PUBLIC KEY-----")
            or b"PRIVATE KEY" in public
        ):
            raise AttestationError("PINNED_PUBLIC_KEY_FINGERPRINT_MISMATCH")
        os.lseek(fd, 0, os.SEEK_SET)
        with tempfile.TemporaryDirectory(prefix="gitcode-attestation-") as temporary:
            sigfile = Path(temporary) / "signature.bin"
            sigfile.write_bytes(signature)
            sigfile.chmod(0o600)
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/openssl",
                        "dgst",
                        "-sha256",
                        "-verify",
                        f"/proc/self/fd/{fd}",
                        "-signature",
                        str(sigfile),
                    ],
                    input=message,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    pass_fds=(fd,),
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LC_ALL": "C",
                        "OPENSSL_CONF": "/dev/null",
                    },
                )
            except (OSError, subprocess.SubprocessError):
                raise AttestationError("ATTESTATION_VERIFICATION_UNAVAILABLE") from None
            if result.returncode:
                raise AttestationError("ATTESTATION_SIGNATURE_INVALID")
    finally:
        os.close(fd)


def _source_key(payload):
    """One trusted capture identity; never split by attacker-controlled payload.

    Ledger enrollment must reject different signed payloads for this same key,
    including across nonce/event ID/key rotation and changed H/B/selectors.
    """
    return hashlib.sha256(
        canonical(
            {
                "issuer_repo_id": payload["issuer"]["repo_id"],
                "workflow_id": payload["workflow"]["id"],
                "capture_job_id": payload["action"]["capture_job_id"],
                "gitcode_run_id": payload["action"]["run_id"],
                "attempt": payload["action"]["attempt"],
                "pipeline": payload["intent"]["pipeline"],
            }
        )
    ).hexdigest()


def verify_envelope(envelope, intent, policy: AttestationPolicy, *, now: int):
    if not isinstance(policy, AttestationPolicy) or not policy.enabled:
        raise AttestationError("GITCODE_ATTESTATION_DISABLED")
    positive(now)
    if isinstance(envelope, bytes):
        envelope = load_json(envelope, limit=MAX_ENVELOPE)
    elif not isinstance(envelope, dict):
        raise AttestationError("ATTESTATION_OBJECT_REQUIRED")
    encoded = canonical(envelope, limit=MAX_ENVELOPE)
    shape(envelope, {"schema", "protocol", "algorithm", "payload", "signature"})
    if (
        type(envelope["schema"]) is not int
        or envelope["schema"] != 1
        or envelope["protocol"] != PROTOCOL
        or envelope["algorithm"] != "RS256"
    ):
        raise AttestationError("UNSUPPORTED_ATTESTATION_PROTOCOL")
    payload = envelope["payload"]
    _binding(payload, intent)
    event, issuer, workflow, action = (
        payload["original_event"],
        payload["issuer"],
        payload["workflow"],
        payload["action"],
    )
    control = policy.control
    if (
        issuer["key_id"] != policy.key_id
        or issuer["repo_id"] != policy.issuer_repo_id
        or issuer["actor"] not in control.allowed_actors
        or workflow
        != {
            "id": policy.workflow_id,
            "path": policy.workflow_path,
            "source_sha": policy.workflow_source_sha,
        }
        or intent["control_repo_id"] != control.github_repo_id
        or intent["control_sha"] != control.control_sha
        or intent["workflow_path"] != control.workflow_path
        or intent["pipeline"] not in {"smoke", "nightly"}
        or intent["pr"]["repo_id"] != control.gitcode_repo_id
        or intent["pr"]["base_repo_id"] != control.gitcode_repo_id
        or intent["pr"]["head_repo_id"] != policy.head_repo_id
        or intent["pr"]["base_ref"] not in control.allowed_base_refs
        or event["event_kind"] not in policy.allowed_event_kinds
        or action["platform_event"] not in policy.allowed_platform_events
    ):
        raise AttestationError("PINNED_EMITTER_OR_RELEASE_MISMATCH")
    if (
        event["created_at"] > now + policy.clock_skew_seconds
        or payload["issued_at"] > now + policy.clock_skew_seconds
        or now - event["created_at"] > policy.max_original_event_age_seconds
        or now >= intent["deadline"]
        or intent["deadline"] - event["created_at"] > control.max_duration_seconds
    ):
        raise AttestationError("ORIGINAL_EVENT_EXPIRED_OR_FUTURE")
    raw_signature = envelope["signature"]
    if not isinstance(raw_signature, str) or not 1 <= len(raw_signature) <= 1400:
        raise AttestationError("INVALID_SIGNATURE_ENCODING")
    try:
        signature = base64.b64decode(raw_signature, validate=True)
    except (ValueError, binascii.Error):
        raise AttestationError("INVALID_SIGNATURE_ENCODING") from None
    if (
        not 256 <= len(signature) <= 1024
        or base64.b64encode(signature).decode("ascii") != raw_signature
    ):
        raise AttestationError("INVALID_SIGNATURE_ENCODING")
    _verify_signature(
        policy, signature, DOMAIN + canonical(payload, limit=MAX_ENVELOPE)
    )
    return VerifiedAttestation(
        key_id=issuer["key_id"],
        issuer_repo_id=issuer["repo_id"],
        original_event_id=event["id"],
        original_event_at=event["created_at"],
        source_snapshot_id=event["source_snapshot_id"],
        source_snapshot=load_json(canonical(event["source_snapshot"])),
        workflow_source_sha=workflow["source_sha"],
        workflow_path=workflow["path"],
        workflow_id=workflow["id"],
        gitcode_run_id=action["run_id"],
        attempt=action["attempt"],
        capture_job_id=action["capture_job_id"],
        source_key=_source_key(payload),
        run_head_sha=action["head_sha"],
        run_head_branch=action["head_branch"],
        platform_event=action["platform_event"],
        actor=issuer["actor"],
        trigger_kind=event["event_kind"],
        nonce=payload["nonce"],
        issued_at=payload["issued_at"],
        envelope_digest=hashlib.sha256(encoded).hexdigest(),
    )
