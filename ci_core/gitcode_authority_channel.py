"""Fresh current-generation proof over existing Artifact -> Blue Pull -> Check.

No listener or credential is introduced. Challenges are immutable, one per
owning Run/Attempt; replies use the precreated fixed authority slot. A reply
proves a serialized observation, NOT atomic merge permission. Unknown, expired,
unreachable or revoked requests never get a current grant. Duplicate Waiters
own their challenge in their own Run and explicitly bind the canonical target.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ci_core.gitcode_artifacts import JSON_LIMIT, unzip_one
from ci_core.gitcode_contract import (
    AUTHORITY_PROTOCOL,
    VerifiedRequest,
    canonical,
    digest,
    load_json,
    positive,
    request_id,
    shape,
    source_identity,
    validate_receipt,
)

CHALLENGE_TTL = 180
RESPONSE_TTL = 15


class AuthorityChannelError(ValueError):
    pass


def _plan(value):
    if not isinstance(value, VerifiedRequest):
        raise AuthorityChannelError("VERIFIED_AUTHORITY_REQUEST_REQUIRED")
    plan = value.plan
    if (
        plan.get("authority_protocol") != AUTHORITY_PROTOCOL
        or "authority" not in plan["result_slots"]
    ):
        raise AuthorityChannelError("AUTHORITY_PROTOCOL_AND_FIXED_SLOT_REQUIRED")
    return plan


def artifact_name(plan):
    return f"lite-authority-{plan['github']['run_id']}-1"


def _binding(plan, receipt):
    validate_receipt(plan, receipt)
    return {
        "request_id": request_id(plan),
        "plan_digest": plan["plan_digest"],
        "receipt": receipt,
        "identity": source_identity(plan),
    }


def _targets(owner, owner_receipt, target, target_receipt):
    if target_receipt["duplicate"]:
        raise AuthorityChannelError("AUTHORITY_TARGET_MUST_BE_CANONICAL")
    if owner_receipt["duplicate"]:
        if request_id(owner) == request_id(target):
            raise AuthorityChannelError("AUTHORITY_CANONICAL_CYCLE")
        for key in (
            "canonical_execution_id",
            "pr_generation",
            "execution_generation",
            "publication_revision",
        ):
            if owner_receipt[key] != target_receipt[key]:
                raise AuthorityChannelError("AUTHORITY_CANONICAL_GENERATION_MISMATCH")
        if owner["request_spec_digest"] != target["request_spec_digest"] or canonical(
            source_identity(owner)
        ) != canonical(source_identity(target)):
            raise AuthorityChannelError("AUTHORITY_CANONICAL_SPEC_MISMATCH")
    elif canonical(_binding(owner, owner_receipt)) != canonical(
        _binding(target, target_receipt)
    ):
        raise AuthorityChannelError("AUTHORITY_TARGET_CHANGED")


def _validate(challenge, owner, owner_receipt, target, target_receipt, now):
    shape(
        challenge, {"protocol", "nonce", "owner", "target", "issued_at", "expires_at"}
    )
    if challenge["protocol"] != AUTHORITY_PROTOCOL:
        raise AuthorityChannelError("AUTHORITY_PROTOCOL_MISMATCH")
    nonce = challenge["nonce"]
    if (
        not isinstance(nonce, str)
        or len(nonce) != 32
        or any(c not in "0123456789abcdef" for c in nonce)
    ):
        raise AuthorityChannelError("AUTHORITY_NONCE_INVALID")
    issued, expires = challenge["issued_at"], challenge["expires_at"]
    positive(issued)
    positive(expires)
    if (
        not issued
        <= now
        < expires
        <= min(issued + CHALLENGE_TTL, owner["deadline"], target["deadline"])
    ):
        raise AuthorityChannelError("AUTHORITY_CHALLENGE_EXPIRED_OR_FUTURE")
    _targets(owner, owner_receipt, target, target_receipt)
    if canonical(challenge["owner"]) != canonical(
        _binding(owner, owner_receipt)
    ) or canonical(challenge["target"]) != canonical(_binding(target, target_receipt)):
        raise AuthorityChannelError("AUTHORITY_CHALLENGE_BINDING_MISMATCH")


def _read(reader, metadata):
    zipped = reader.github.download_artifact(
        metadata["id"], limit=metadata["size_in_bytes"]
    )
    if (
        len(zipped) != metadata["size_in_bytes"]
        or hashlib.sha256(zipped).hexdigest() != metadata["digest"][7:]
    ):
        raise AuthorityChannelError("AUTHORITY_ARTIFACT_DIGEST_MISMATCH")
    return load_json(unzip_one(zipped, "authority-challenge.json", limit=JSON_LIMIT))


class AuthorityResponder:
    def __init__(
        self, reader, channel, *, resolve_request, cleanup_journal=None, clock=time.time
    ):
        self.cleanup_journal = cleanup_journal
        self.reader, self.channel, self.resolve_request, self.clock = (
            reader,
            channel,
            resolve_request,
            clock,
        )

    def cleanup(self, verified, receipt):
        """Neutral closure only after the independently observed owner terminates.

        The durable sidecar records a confirmed exact-slot closure; it does not
        change coordinator versions, receipts, leases or execution outcomes.
        """
        owner = _plan(verified)
        validate_receipt(owner, receipt)
        if self.cleanup_journal is None:
            raise AuthorityChannelError("DURABLE_AUTHORITY_CLOSURE_JOURNAL_REQUIRED")
        key = digest(
            {"protocol": AUTHORITY_PROTOCOL, "owner": _binding(owner, receipt)}
        )
        _, remembered = self.cleanup_journal.begin(key)
        slot_id = owner["result_slots"]["authority"]["id"]
        payload = {
            "schema": 4,
            "phase": "AUTHORITY_CLOSED",
            "request_id": request_id(owner),
            "plan_digest": owner["plan_digest"],
            "receipt": receipt,
            "owner_run_status": "completed",
        }
        binding = {"slot_id": slot_id, "payload_digest": digest(payload)}
        if remembered is not None:
            if canonical(remembered) != canonical(binding):
                raise AuthorityChannelError("AUTHORITY_CLOSURE_IDENTITY_CHANGED")
            return True
        run = self.reader._actual_run(owner["github"]["run_id"], allow_completed=True)
        if run["status"] != "completed":
            return False
        self.channel._publish(
            owner, "authority", payload, terminal=True, conclusion="neutral"
        )
        self.cleanup_journal.bind(key, binding)
        return True

    def respond(self, verified, receipt, *, current):
        owner = _plan(verified)
        run = self.reader.actual_run(owner["github"]["run_id"])
        metadata = self.reader._named(artifact_name(owner), run)
        if metadata is None:
            return None
        challenge = _read(self.reader, metadata)
        target_ref = challenge.get("target", {})
        target_verified = self.resolve_request(target_ref.get("request_id"))
        target = _plan(target_verified)
        envelope = self.channel.read_receipt(target)
        if envelope is None:
            raise AuthorityChannelError("CANONICAL_RECEIPT_UNAVAILABLE")
        target_receipt = envelope["receipt"]
        now = int(self.clock())
        _validate(challenge, owner, receipt, target, target_receipt, now)
        try:
            authorized = current(target_verified, target_receipt)
            if type(authorized) is not bool:
                raise AuthorityChannelError("AUTHORITY_CALLBACK_BOOLEAN_REQUIRED")
        except Exception:
            authorized = False
        observed = now  # Conservative start-of-observation bound, not end time.
        finished = int(self.clock())
        if (
            not observed
            <= finished
            < min(challenge["expires_at"], observed + RESPONSE_TTL)
        ):
            raise AuthorityChannelError("AUTHORITY_OBSERVATION_EXPIRED")
        payload = {
            "schema": 4,
            "phase": "AUTHORITY_RESPONSE",
            "request_id": request_id(owner),
            "plan_digest": owner["plan_digest"],
            "receipt": receipt,
            "challenge_digest": digest(challenge),
            "challenge": challenge,
            "authorized": authorized,
            "observed_at": observed,
            "expires_at": min(challenge["expires_at"], observed + RESPONSE_TTL),
        }
        self.channel._publish(owner, "authority", payload)
        return authorized


class HostedCurrent:
    def __init__(
        self, reader, channel, upload, *, state_directory, owner_run_id, clock=time.time
    ):
        positive(owner_run_id)
        self.reader, self.channel, self.upload, self.clock = (
            reader,
            channel,
            upload,
            clock,
        )
        self.owner_run_id = owner_run_id
        self.root = Path(state_directory)
        if self.root.is_symlink():
            raise AuthorityChannelError("AUTHORITY_STATE_SYMLINK_FORBIDDEN")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "authority-challenges.sqlite3"
        if self.path.is_symlink():
            raise AuthorityChannelError("AUTHORITY_JOURNAL_SYMLINK_FORBIDDEN")
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS challenges (owner TEXT PRIMARY KEY, document TEXT NOT NULL, artifact_id INTEGER)"
            )
        self.path.chmod(0o600)
        self._owner = None

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _own_plan(self, target):
        if self.owner_run_id == target["github"]["run_id"]:
            return target
        if self._owner is None:
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                value = self.reader.load_results(
                    self.owner_run_id, Path(directory) / "owner", now=int(self.clock())
                )
            if value is None:
                raise AuthorityChannelError("OWNING_REQUEST_UNAVAILABLE")
            self._owner = _plan(value)
        return self._owner

    def __call__(self, target):
        _plan(VerifiedRequest(canonical(target)))
        owner = self._own_plan(target)
        run = self.reader.actual_run(self.owner_run_id)
        owner_envelope, target_envelope = (
            self.channel.read_receipt(owner),
            self.channel.read_receipt(target),
        )
        if owner_envelope is None or target_envelope is None:
            return None
        owner_receipt, target_receipt = (
            owner_envelope["receipt"],
            target_envelope["receipt"],
        )
        _targets(owner, owner_receipt, target, target_receipt)
        now = int(self.clock())
        key = request_id(owner)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT document,artifact_id FROM challenges WHERE owner=?", (key,)
            ).fetchone()
            owns = row is None
            if owns:
                challenge = {
                    "protocol": AUTHORITY_PROTOCOL,
                    "nonce": uuid.uuid4().hex,
                    "owner": _binding(owner, owner_receipt),
                    "target": _binding(target, target_receipt),
                    "issued_at": now,
                    "expires_at": min(
                        now + CHALLENGE_TTL, owner["deadline"], target["deadline"]
                    ),
                }
                db.execute(
                    "INSERT INTO challenges VALUES(?,?,NULL)",
                    (key, canonical(challenge).decode()),
                )
                artifact_id = None
            else:
                challenge, artifact_id = load_json(row[0].encode()), row[1]
        _validate(challenge, owner, owner_receipt, target, target_receipt, now)
        if artifact_id is None and owns:
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                path = Path(directory) / "authority-challenge.json"
                path.write_bytes(canonical(challenge))
                try:
                    artifact_id = positive(self.upload(artifact_name(owner), path))
                except (OSError, TimeoutError):
                    artifact_id = None
            if artifact_id is not None:
                with self._db() as db:
                    db.execute(
                        "UPDATE challenges SET artifact_id=? WHERE owner=?",
                        (artifact_id, key),
                    )
        if artifact_id is None:
            metadata = self.reader._named(artifact_name(owner), run)
            if metadata is None:
                return None  # Unknown upload is never blindly retried.
            artifact_id = metadata["id"]
        metadata = self.reader._metadata(
            artifact_id, artifact_name(owner), run, limit=JSON_LIMIT
        )
        if canonical(_read(self.reader, metadata)) != canonical(challenge):
            raise AuthorityChannelError("AUTHORITY_UPLOADED_CHALLENGE_CHANGED")
        with self._db() as db:
            db.execute(
                "UPDATE challenges SET artifact_id=? WHERE owner=?", (artifact_id, key)
            )
        check = self.channel.read_slot(owner, "authority")
        payload = self.channel._payload(check)
        if payload.get("phase") == "SLOT_CREATED":
            return None
        shape(
            payload,
            {
                "schema",
                "phase",
                "request_id",
                "plan_digest",
                "receipt",
                "challenge_digest",
                "challenge",
                "authorized",
                "observed_at",
                "expires_at",
            },
        )
        if (
            payload["schema"] != 4
            or payload["phase"] != "AUTHORITY_RESPONSE"
            or payload["request_id"] != key
            or payload["plan_digest"] != owner["plan_digest"]
            or canonical(payload["receipt"]) != canonical(owner_receipt)
            or payload["challenge_digest"] != digest(challenge)
            or canonical(payload["challenge"]) != canonical(challenge)
        ):
            raise AuthorityChannelError("AUTHORITY_RESPONSE_BINDING_MISMATCH")
        observed, expires = payload["observed_at"], payload["expires_at"]
        positive(observed)
        positive(expires)
        now = int(self.clock())
        if (
            not challenge["issued_at"]
            <= observed
            <= now
            < expires
            <= min(challenge["expires_at"], observed + RESPONSE_TTL)
        ):
            return None
        if (
            type(payload["authorized"]) is not bool
            or check.get("status") != "in_progress"
            or check.get("conclusion") is not None
        ):
            raise AuthorityChannelError("AUTHORITY_RESPONSE_STATE_INVALID")
        return payload["authorized"]
