"""Transactional GitCode generations, canonical executions and fenced cleanup.

This is a ledger inside the existing Blue service, not a second scheduler.
Physical device/port locks still belong to ci_core.resources. API authority and
EventProof must be supplied by trusted adapters; dispatch integers are not proof.
A platform without a verified event revision/history must not enable this adapter.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ci_core.gitcode_contract import (
    VerifiedRequest,
    aggregate_results,
    canonical,
    digest,
    positive,
    request_id,
    text,
    validate_pr,
)

STATE_VERSION = 6


class StateError(ValueError):
    pass


@dataclass(frozen=True)
class Authority:
    pr: dict
    revision: int
    state: str = "open"
    authorized: bool = True


@dataclass(frozen=True)
class EventProof:
    """Verified ordered event-journal evidence, never deserialized from Plan."""

    event_id: str
    source_snapshot_id: str
    event_order: int
    version_revision: int
    authorized_rerun: bool = False


@dataclass(frozen=True)
class Admission:
    receipt: dict
    canonical: dict


SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_observations (
 repo_id TEXT NOT NULL, number INTEGER NOT NULL, revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL, PRIMARY KEY(repo_id,number)
);
CREATE TABLE IF NOT EXISTS source_intents (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT NOT NULL,
 id TEXT NOT NULL UNIQUE, semantic TEXT NOT NULL UNIQUE,
 number INTEGER NOT NULL, revision INTEGER NOT NULL,
 intent TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 trigger_kind TEXT NOT NULL, actor TEXT NOT NULL, authorized_rerun INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_invalidations (
 repo_id TEXT NOT NULL, number INTEGER NOT NULL, revision INTEGER NOT NULL,
 observed_at INTEGER NOT NULL, PRIMARY KEY(repo_id,number,revision)
);
CREATE TABLE IF NOT EXISTS source_attestations (
 intent_id TEXT PRIMARY KEY REFERENCES source_intents(id),
 envelope_digest TEXT NOT NULL, key_id TEXT NOT NULL, event_id TEXT NOT NULL,
 original_event_at INTEGER NOT NULL, action_run_id TEXT NOT NULL, payload TEXT NOT NULL
);
"""

_SCHEMA = """
CREATE TABLE pr_versions (
 repo_id TEXT NOT NULL, number INTEGER NOT NULL, generation INTEGER NOT NULL,
 version_json TEXT NOT NULL, revision INTEGER NOT NULL, observed_at INTEGER NOT NULL, available INTEGER NOT NULL,
 PRIMARY KEY(repo_id, number)
);
CREATE TABLE pipeline_heads (
 repo_id TEXT NOT NULL, number INTEGER NOT NULL, generation INTEGER NOT NULL,
 pipeline TEXT NOT NULL, semantic_key TEXT NOT NULL, rerun_intent TEXT NOT NULL,
 execution_generation INTEGER NOT NULL, publication_revision INTEGER NOT NULL,
 event_order INTEGER NOT NULL, spec_digest TEXT,
 PRIMARY KEY(repo_id, number, generation, pipeline)
);
CREATE TABLE intents (
 id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, number INTEGER NOT NULL,
 generation INTEGER NOT NULL, pipeline TEXT NOT NULL, semantic_key TEXT NOT NULL,
 execution_generation INTEGER NOT NULL, publication_revision INTEGER NOT NULL,
 event_order INTEGER NOT NULL, intent_json TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE deliveries (
 request_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents(id),
 identity_json TEXT NOT NULL
);
CREATE TABLE executions (
 id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, number INTEGER NOT NULL,
 generation INTEGER NOT NULL, pipeline TEXT NOT NULL, semantic_key TEXT NOT NULL,
 execution_generation INTEGER NOT NULL, publication_revision INTEGER NOT NULL,
 spec_digest TEXT NOT NULL, canonical_request_id TEXT NOT NULL,
 plan_json TEXT NOT NULL, deadline INTEGER NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 UNIQUE(repo_id, number, generation, pipeline, execution_generation)
);
CREATE TABLE requests (
 id TEXT PRIMARY KEY REFERENCES deliveries(request_id), execution_id TEXT NOT NULL REFERENCES executions(id),
 plan_digest TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE cases (
 execution_id TEXT NOT NULL REFERENCES executions(id), case_id TEXT NOT NULL,
 state TEXT NOT NULL, result_json TEXT,
 PRIMARY KEY(execution_id, case_id)
);
CREATE TABLE leases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL REFERENCES executions(id),
 case_id TEXT NOT NULL, lease_id TEXT NOT NULL UNIQUE, cleanup_json TEXT NOT NULL,
 state TEXT NOT NULL, UNIQUE(execution_id, case_id)
);
CREATE TABLE publication_receipts (
 execution_id TEXT PRIMARY KEY REFERENCES executions(id),
 plan_digest TEXT NOT NULL, receipt_digest TEXT NOT NULL, published_at INTEGER NOT NULL
);
CREATE TABLE outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
 UNIQUE(kind, entity_id)
);
CREATE TABLE outbox_attempts (
 outbox_id INTEGER PRIMARY KEY REFERENCES outbox(id), sequence INTEGER NOT NULL
);
"""


def _json(value) -> str:
    return canonical(value).decode()


def _scope(pr):
    return pr["repo_id"], pr["number"]


def _version(authority):
    validate_pr(authority.pr, allowed_head_repo_ids=None)
    positive(authority.revision, "TRUSTED_AUTHORITY_REVISION_REQUIRED")
    if (
        authority.state not in ("open", "closed", "merged")
        or type(authority.authorized) is not bool
    ):
        raise StateError("INVALID_AUTHORITY_OBSERVATION")
    return {
        "pr": authority.pr,
        "state": authority.state,
        "authorized": authority.authorized,
    }


def _semantic(intent):
    # Delivery/action/nonce and rerun identities do not change test specifications.
    return digest(
        {
            k: intent[k]
            for k in (
                "pr",
                "control_repo_id",
                "control_sha",
                "workflow_path",
                "pipeline",
                "selectors",
            )
        }
    )


class Coordinator:
    def __init__(
        self,
        path: Path,
        *,
        authority_max_age_seconds: int = 30,
        decision_clock: Callable[[], int] | None = None,
    ):
        if decision_clock is not None and not callable(decision_clock):
            raise StateError("INVALID_DECISION_CLOCK")
        self.decision_clock = decision_clock
        positive(authority_max_age_seconds)
        self.authority_max_age_seconds = authority_max_age_seconds
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, STATE_VERSION):
                raise StateError("UNSUPPORTED_COORDINATOR_STATE_VERSION")
            if version in (1, 2, 3, 4, 5):
                # Upgrade atomically: keep generations, leases and outbox intact.
                # Historical v1 results without an exit code are not fabricated.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if version == 1:
                        rows = connection.execute(
                            "SELECT result_json FROM cases WHERE result_json IS NOT NULL"
                        ).fetchall()
                        rows += connection.execute(
                            "SELECT payload_json FROM outbox WHERE kind='CASE_RESULT'"
                        ).fetchall()
                        for row in rows:
                            if type(json.loads(row[0]).get("exit_code")) is not int:
                                raise StateError(
                                    "SCHEMA4_RESULT_MIGRATION_REQUIRES_EXIT_EVIDENCE"
                                )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS publication_receipts (execution_id TEXT PRIMARY KEY REFERENCES executions(id), plan_digest TEXT NOT NULL, receipt_digest TEXT NOT NULL, published_at INTEGER NOT NULL)"
                    )
                    columns = [
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(publication_receipts)"
                        )
                    ]
                    if columns != [
                        "execution_id",
                        "plan_digest",
                        "receipt_digest",
                        "published_at",
                    ]:
                        raise StateError("PUBLICATION_RECEIPT_MIGRATION_CONFLICT")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS outbox_attempts ("
                        "outbox_id INTEGER PRIMARY KEY REFERENCES outbox(id), "
                        "sequence INTEGER NOT NULL)"
                    )
                    columns = [
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(outbox_attempts)"
                        )
                    ]
                    if columns != ["outbox_id", "sequence"]:
                        raise StateError("OUTBOX_ATTEMPT_MIGRATION_CONFLICT")
                    for statement in SOURCE_SCHEMA.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    # Previous coordinator versions did not retain issuer
                    # invalidation chronology. Import the known current version
                    # and establish a strict migration-time enrollment barrier.
                    for row in connection.execute(
                        "SELECT * FROM pr_versions"
                    ).fetchall():
                        old = json.loads(row["version_json"])
                        snapshot = {
                            "identity": old["pr"],
                            "state": old["state"],
                            "authorized": old["authorized"],
                        }
                        inserted = connection.execute(
                            "INSERT OR IGNORE INTO source_observations VALUES(?,?,?,?)",
                            (
                                row["repo_id"],
                                row["number"],
                                row["revision"],
                                _json(snapshot),
                            ),
                        ).rowcount
                        if inserted:
                            connection.execute(
                                "INSERT OR IGNORE INTO source_invalidations VALUES(?,?,?,?)",
                                (
                                    row["repo_id"],
                                    row["number"],
                                    row["revision"],
                                    int(time.time()),
                                ),
                            )
                    connection.execute(f"PRAGMA user_version={STATE_VERSION}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if tables:
                    raise StateError("UNVERSIONED_EXISTING_DATABASE")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + _SCHEMA
                    + SOURCE_SCHEMA
                    + f"\nPRAGMA user_version={STATE_VERSION};\nCOMMIT;"
                )
        self.path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != STATE_VERSION:
                raise StateError("UNSUPPORTED_COORDINATOR_STATE_VERSION")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _outbox(connection, kind, entity_id, payload):
        encoded = _json(payload)
        row = connection.execute(
            "SELECT payload_json FROM outbox WHERE kind=? AND entity_id=?",
            (kind, entity_id),
        ).fetchone()
        if row and row[0] != encoded:
            raise StateError("OUTBOX_IDENTITY_CONFLICT")
        connection.execute(
            "INSERT OR IGNORE INTO outbox(kind,entity_id,payload_json) VALUES(?,?,?)",
            (kind, entity_id, encoded),
        )

    def _revoke(self, connection, scope, *, generation=None, pipeline=None):
        where, args = "repo_id=? AND number=?", list(scope)
        if generation is not None:
            where += " AND generation=?"
            args.append(generation)
        if pipeline is not None:
            where += " AND pipeline=?"
            args.append(pipeline)
        connection.execute("UPDATE intents SET revoked=1 WHERE " + where, args)
        for row in connection.execute(
            "SELECT d.request_id,d.identity_json FROM deliveries d JOIN intents i ON d.intent_id=i.id WHERE i.id IN (SELECT id FROM intents WHERE "
            + where
            + ")",
            args,
        ):
            self._outbox(
                connection,
                "CANCEL_RUN",
                row["request_id"],
                json.loads(row["identity_json"]),
            )
        connection.execute(
            "UPDATE executions SET cancel_requested=1 WHERE " + where, args
        )
        for row in connection.execute("SELECT id FROM executions WHERE " + where, args):
            self._outbox(
                connection, "CANCEL_EXECUTION", row["id"], {"execution_id": row["id"]}
            )

    def reconcile(
        self,
        repo_id: str,
        number: int,
        read_authority: Callable[[], Authority],
        *,
        now: int,
    ) -> int:
        """Serialize the authoritative read with version/cancellation publication.

        An event only causes this read. Its possibly stale snapshot is not an
        input. On network failure the transaction rolls back and no grant is made.
        """
        with self._transaction() as connection:
            try:
                authority = read_authority()
            except Exception:
                # Persist refusal before surfacing the transport error. Keeping
                # H/B unchanged is not evidence that the authority is reachable.
                connection.execute(
                    "UPDATE pr_versions SET available=0 WHERE repo_id=? AND number=?",
                    (repo_id, number),
                )
                connection.commit()
                raise
            return self._reconcile_authority(
                connection, repo_id, number, authority, now=now
            )

    def _reconcile_authority(self, connection, repo_id, number, authority, *, now):
        """Apply an authenticated observation inside the caller's transaction.

        The unified source ledger uses this to commit observation, revocation,
        cancellation outbox and signed-intent enrollment as one SQLite change.
        """
        version = _version(authority)
        if _scope(authority.pr) != (repo_id, number):
            raise StateError("AUTHORITY_PR_SCOPE_MISMATCH")
        encoded = _json(version)
        row = connection.execute(
            "SELECT * FROM pr_versions WHERE repo_id=? AND number=?",
            (repo_id, number),
        ).fetchone()
        if row is None:
            generation = 1
            connection.execute(
                "INSERT INTO pr_versions VALUES(?,?,?,?,?,?,1)",
                (repo_id, number, generation, encoded, authority.revision, now),
            )
        else:
            if authority.revision < row["revision"]:
                raise StateError("AUTHORITY_REVISION_REGRESSION")
            # A trusted version ledger may have observed A->B->A between
            # coordinator polls. Revision advance still revokes old grants.
            changed = (
                encoded != row["version_json"] or authority.revision > row["revision"]
            )
            if changed and authority.revision == row["revision"]:
                raise StateError("AUTHORITY_REVISION_COLLISION")
            generation = row["generation"] + int(changed)
            if changed:
                self._revoke(connection, (repo_id, number))
            connection.execute(
                "UPDATE pr_versions SET generation=?,version_json=?,revision=?,observed_at=?,available=1 WHERE repo_id=? AND number=?",
                (generation, encoded, authority.revision, now, repo_id, number),
            )
        return generation

    def register_intent(self, intent: dict, delivery: dict, proof: EventProof) -> int:
        """Record an early, non-executable dispatch intent and its Run ownership."""
        validate_pr(intent["pr"], allowed_head_repo_ids=None)
        canonical(intent, limit=16 * 1024)
        if (
            not isinstance(proof, EventProof)
            or type(proof.authorized_rerun) is not bool
        ):
            raise StateError("VERIFIED_EVENT_PROOF_REQUIRED")
        positive(proof.event_order)
        positive(proof.version_revision)
        bridge = intent["bridge"]
        if (
            proof.event_id != bridge["bridge_event_id"]
            or proof.source_snapshot_id != bridge["source_snapshot_id"]
        ):
            raise StateError("EVENT_PROOF_IDENTITY_MISMATCH")
        intent_id = text(bridge["dispatch_intent_id"], limit=160)
        rid = text(delivery["request_id"], limit=160)
        if (
            delivery["repo_id"] != intent["control_repo_id"]
            or delivery["control_sha"] != intent["control_sha"]
        ):
            raise StateError("DELIVERY_CONTROL_MISMATCH")
        if (
            delivery["run_attempt"] != 1
            or rid != f"{delivery['repo_id']}/{delivery['run_id']}/1"
        ):
            raise StateError("NATIVE_RERUN_FORBIDDEN")
        scope, encoded = _scope(intent["pr"]), _json(intent)
        semantic = _semantic(intent)
        with self._transaction() as connection:
            version = connection.execute(
                "SELECT * FROM pr_versions WHERE repo_id=? AND number=?", scope
            ).fetchone()
            if version is None or not version["available"]:
                raise StateError("PR_NOT_RECONCILED_OR_AUTHORITY_UNAVAILABLE")
            current = json.loads(version["version_json"])
            if (
                current["state"] != "open"
                or current["authorized"] is not True
                or canonical(current["pr"]) != canonical(intent["pr"])
            ):
                raise StateError("INTENT_VERSION_STALE")
            old = connection.execute(
                "SELECT * FROM intents WHERE id=?", (intent_id,)
            ).fetchone()
            if old:
                if old["intent_json"] != encoded:
                    raise StateError("INTENT_IDENTITY_CHANGED")
                if old["revoked"] or old["generation"] != version["generation"]:
                    raise StateError("REVOKED_INTENT_CANNOT_REVIVE")
                if old["event_order"] != proof.event_order:
                    raise StateError("EVENT_PROOF_CHANGED")
                generation = old["generation"]
            else:
                # A first late intent cannot be assigned by merely matching H/B.
                if proof.version_revision != version["revision"]:
                    raise StateError("LATE_INTENT_HAS_NO_CURRENT_REVISION_PROOF")
                generation = version["generation"]
                head_key = (*scope, generation, intent["pipeline"])
                head = connection.execute(
                    "SELECT * FROM pipeline_heads WHERE repo_id=? AND number=? AND generation=? AND pipeline=?",
                    head_key,
                ).fetchone()
                rerun = text(bridge["rerun_intent_id"], limit=160)
                if head and proof.event_order < head["event_order"]:
                    raise StateError("OUT_OF_ORDER_SPECIFICATION_INTENT")
                same = (
                    head
                    and head["semantic_key"] == semantic
                    and head["rerun_intent"] == rerun
                )
                if head and not same and proof.event_order == head["event_order"]:
                    raise StateError("CONFLICTING_EVENT_ORDER")
                if (
                    head
                    and head["semantic_key"] == semantic
                    and head["rerun_intent"] != rerun
                    and not proof.authorized_rerun
                ):
                    raise StateError("WHOLE_RUN_RERUN_NOT_AUTHORIZED")
                if same:
                    execution_generation = head["execution_generation"]
                    publication = head["publication_revision"]
                    connection.execute(
                        "UPDATE pipeline_heads SET event_order=? WHERE repo_id=? AND number=? AND generation=? AND pipeline=?",
                        (proof.event_order, *head_key),
                    )
                else:
                    execution_generation = (
                        head["execution_generation"] + 1 if head else 1
                    )
                    publication = head["publication_revision"] + 1 if head else 1
                    if head:
                        self._revoke(
                            connection,
                            scope,
                            generation=generation,
                            pipeline=intent["pipeline"],
                        )
                    connection.execute(
                        "INSERT OR REPLACE INTO pipeline_heads VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                        (
                            *head_key,
                            semantic,
                            rerun,
                            execution_generation,
                            publication,
                            proof.event_order,
                        ),
                    )
                connection.execute(
                    "INSERT INTO intents(id,repo_id,number,generation,pipeline,semantic_key,execution_generation,publication_revision,event_order,intent_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        *scope,
                        generation,
                        intent["pipeline"],
                        semantic,
                        execution_generation,
                        publication,
                        proof.event_order,
                        encoded,
                    ),
                )
            previous = connection.execute(
                "SELECT * FROM deliveries WHERE request_id=?", (rid,)
            ).fetchone()
            delivery_json = _json(delivery)
            if previous and (
                previous["intent_id"] != intent_id
                or previous["identity_json"] != delivery_json
            ):
                raise StateError("REQUEST_BINDING_IMMUTABLE")
            connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?)",
                (rid, intent_id, delivery_json),
            )
            return generation

    def _locked_decision_now(self, connection, supplied_now):
        """Authorization time, never source observation or original event time.

        Production supplies a trusted clock. Explicit historical/test callers
        without that clock retain their supplied timestamp. Resolve once only
        AFTER BEGIN IMMEDIATE, including all subsequent deadline decisions.
        """
        if not connection.in_transaction:
            raise StateError("DECISION_REQUIRES_TRANSACTION_LOCK")
        try:
            now = self.decision_clock() if self.decision_clock else supplied_now
        except Exception:
            raise StateError("INVALID_DECISION_CLOCK") from None
        if type(now) is not int or now < 0:
            raise StateError("INVALID_DECISION_CLOCK")
        return now

    def _current(self, connection, execution, *, now):
        version = connection.execute(
            "SELECT * FROM pr_versions WHERE repo_id=? AND number=?",
            (execution["repo_id"], execution["number"]),
        ).fetchone()
        head = connection.execute(
            "SELECT * FROM pipeline_heads WHERE repo_id=? AND number=? AND generation=? AND pipeline=?",
            (
                execution["repo_id"],
                execution["number"],
                execution["generation"],
                execution["pipeline"],
            ),
        ).fetchone()
        observed = version["observed_at"] if version else None
        age = now - observed if observed is not None else None
        checks = (
            (version is not None, "VERSION_MISSING"),
            (head is not None, "PIPELINE_HEAD_MISSING"),
            (bool(version and version["available"]), "AUTHORITY_UNAVAILABLE"),
            (age is not None and age >= 0, "AUTHORITY_FROM_FUTURE"),
            (
                age is not None and age <= self.authority_max_age_seconds,
                "AUTHORITY_STALE",
            ),
            (
                bool(version and version["generation"] == execution["generation"]),
                "PR_GENERATION_CHANGED",
            ),
            (
                bool(
                    head
                    and head["execution_generation"]
                    == execution["execution_generation"]
                ),
                "EXECUTION_GENERATION_CHANGED",
            ),
            (
                bool(
                    head
                    and head["publication_revision"]
                    == execution["publication_revision"]
                ),
                "PUBLICATION_REVISION_CHANGED",
            ),
            (
                bool(head and head["spec_digest"] == execution["spec_digest"]),
                "SPEC_CHANGED",
            ),
            (not execution["cancel_requested"], "EXECUTION_CANCELLED"),
            (now < execution["deadline"], "EXECUTION_DEADLINE_EXPIRED"),
        )
        reason = next((reason for okay, reason in checks if not okay), None)
        if reason is not None:
            # Fixed enum and numeric timing only: never payload, credentials,
            # source URLs, intent contents or exception text from callbacks.
            logging.warning(
                "schema-4 current rejected reason=%s decision_at=%d observed_at=%s "
                "age=%s max_age=%d deadline=%d",
                reason,
                now,
                observed,
                age,
                self.authority_max_age_seconds,
                execution["deadline"],
            )
            return False
        return True

    def claim(self, verified: VerifiedRequest, *, now: int) -> Admission:
        if not isinstance(verified, VerifiedRequest):
            raise StateError("VERIFIED_REQUEST_REQUIRED")
        plan = verified.plan
        rid, intent_id = request_id(plan), plan["bridge"]["dispatch_intent_id"]
        with self._transaction() as connection:
            now = self._locked_decision_now(connection, now)
            delivery = connection.execute(
                "SELECT * FROM deliveries WHERE request_id=?", (rid,)
            ).fetchone()
            intent = connection.execute(
                "SELECT * FROM intents WHERE id=?", (intent_id,)
            ).fetchone()
            if not delivery or not intent or delivery["intent_id"] != intent_id:
                raise StateError("EARLY_INTENT_NOT_REGISTERED")
            existing = connection.execute(
                "SELECT * FROM requests WHERE id=?", (rid,)
            ).fetchone()
            if existing:
                if existing["plan_digest"] != plan["plan_digest"]:
                    raise StateError("REQUEST_PLAN_IMMUTABLE")
                execution = connection.execute(
                    "SELECT * FROM executions WHERE id=?", (existing["execution_id"],)
                ).fetchone()
                if not self._current(connection, execution, now=now):
                    raise StateError("EXISTING_ADMISSION_REVOKED")
                return self._admission(json.loads(existing["receipt_json"]), execution)
            if intent["revoked"]:
                raise StateError("REVOKED_INTENT_CANNOT_REVIVE")
            scope = _scope(plan["pr"])
            version = connection.execute(
                "SELECT * FROM pr_versions WHERE repo_id=? AND number=?", scope
            ).fetchone()
            head_key = (*scope, intent["generation"], intent["pipeline"])
            head = connection.execute(
                "SELECT * FROM pipeline_heads WHERE repo_id=? AND number=? AND generation=? AND pipeline=?",
                head_key,
            ).fetchone()
            current_version = json.loads(version["version_json"])
            if (
                version["generation"] != intent["generation"]
                or not head
                or head["publication_revision"] != intent["publication_revision"]
                or current_version["state"] != "open"
                or current_version["authorized"] is not True
                or canonical(current_version["pr"]) != canonical(plan["pr"])
                or now >= plan["deadline"]
            ):
                raise StateError("REQUEST_GENERATION_NOT_CURRENT")
            recorded_intent = json.loads(intent["intent_json"])
            # The verified Plan's intent hash is immutable; do not let a different
            # verified payload reuse an earlier registration under the same ID.
            if plan["artifacts"]["intent"]["content_sha256"] != digest(recorded_intent):
                raise StateError("REGISTERED_INTENT_DIGEST_MISMATCH")
            if head["spec_digest"] not in (None, plan["request_spec_digest"]):
                raise StateError("NONDETERMINISTIC_SPEC_FOR_SAME_INTENT")
            connection.execute(
                "UPDATE pipeline_heads SET spec_digest=? WHERE repo_id=? AND number=? AND generation=? AND pipeline=?",
                (plan["request_spec_digest"], *head_key),
            )
            execution = connection.execute(
                "SELECT * FROM executions WHERE repo_id=? AND number=? AND generation=? AND pipeline=? AND execution_generation=?",
                (*head_key, intent["execution_generation"]),
            ).fetchone()
            duplicate = execution is not None
            if execution is None:
                eid = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        eid,
                        *scope,
                        intent["generation"],
                        intent["pipeline"],
                        intent["semantic_key"],
                        intent["execution_generation"],
                        intent["publication_revision"],
                        plan["request_spec_digest"],
                        rid,
                        _json(plan),
                        plan["deadline"],
                    ),
                )
                for case in plan["cases"]:
                    connection.execute(
                        "INSERT INTO cases VALUES(?,?,?,NULL)",
                        (eid, case["case_id"], "PLANNED"),
                    )
                execution = connection.execute(
                    "SELECT * FROM executions WHERE id=?", (eid,)
                ).fetchone()
            if not self._current(connection, execution, now=now):
                raise StateError("CANONICAL_EXECUTION_NOT_CURRENT")
            receipt = {
                "request_id": rid,
                "plan_digest": plan["plan_digest"],
                "request_spec_digest": plan["request_spec_digest"],
                "source_snapshot_id": plan["bridge"]["source_snapshot_id"],
                "dispatch_intent_id": intent_id,
                "canonical_execution_id": execution["id"],
                "pr_generation": intent["generation"],
                "execution_generation": intent["execution_generation"],
                "publication_revision": intent["publication_revision"],
                "duplicate": duplicate,
            }
            connection.execute(
                "INSERT INTO requests VALUES(?,?,?,?)",
                (rid, execution["id"], plan["plan_digest"], _json(receipt)),
            )
            self._outbox(
                connection,
                "ADMISSION_RECEIPT",
                rid,
                {
                    "receipt": receipt,
                    "canonical": self._admission(receipt, execution).canonical,
                },
            )
            return self._admission(receipt, execution)

    @staticmethod
    def _admission(receipt, execution):
        plan = json.loads(execution["plan_json"])
        canonical_identity = {
            "request_id": execution["canonical_request_id"],
            "plan_digest": plan["plan_digest"],
            "github": plan["github"],
            "result_slots": plan["result_slots"],
        }
        return Admission(receipt, canonical_identity)

    def assert_startable(self, execution_id: str, *, now: int) -> None:
        with self._transaction() as connection:
            now = self._locked_decision_now(connection, now)
            execution = connection.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if execution is None or not self._current(connection, execution, now=now):
                raise StateError("EXECUTION_START_OR_PUBLICATION_REVOKED")

    def claim_case(self, execution_id: str, case_id: str, *, now: int) -> bool:
        with self._transaction() as connection:
            now = self._locked_decision_now(connection, now)
            execution = connection.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if execution is None or not self._current(connection, execution, now=now):
                raise StateError("EXECUTION_START_OR_PUBLICATION_REVOKED")
            row = connection.execute(
                "UPDATE cases SET state='CLAIMED' WHERE execution_id=? AND case_id=? AND state='PLANNED'",
                (execution_id, case_id),
            )
            return row.rowcount == 1

    def bind_lease(
        self, execution_id: str, case_id: str, lease_id: str, cleanup: dict, *, now: int
    ) -> int:
        """Persist captured container/process markers BEFORE any remote launch.

        The caller must already hold the existing physical ResourceManager lease.
        The returned fencing token does not itself grant any additional devices.
        """
        text(lease_id, limit=160)
        canonical(cleanup, limit=64 * 1024)
        if not isinstance(cleanup.get("nodes"), list) or not cleanup["nodes"]:
            raise StateError("RECOVERY_BINDINGS_REQUIRED")
        for node in cleanup["nodes"]:
            if not all(
                node.get(k)
                for k in (
                    "host_id",
                    "container_id",
                    "image_id",
                    "task_marker",
                    "device_ids",
                )
            ):
                raise StateError("RECOVERY_BINDINGS_REQUIRED")
        with self._transaction() as connection:
            now = self._locked_decision_now(connection, now)
            execution = connection.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            case = connection.execute(
                "SELECT * FROM cases WHERE execution_id=? AND case_id=?",
                (execution_id, case_id),
            ).fetchone()
            if (
                execution is None
                or not self._current(connection, execution, now=now)
                or case is None
                or case["state"] != "CLAIMED"
            ):
                raise StateError("LEASE_START_REVOKED")
            old = connection.execute(
                "SELECT * FROM leases WHERE execution_id=? AND case_id=?",
                (execution_id, case_id),
            ).fetchone()
            if old:
                if (
                    old["lease_id"] != lease_id
                    or old["cleanup_json"] != _json(cleanup)
                    or old["state"] != "ACTIVE"
                ):
                    raise StateError("LEASE_BINDING_IMMUTABLE")
                return old["id"]
            row = connection.execute(
                "INSERT INTO leases(execution_id,case_id,lease_id,cleanup_json,state) VALUES(?,?,?,?,?)",
                (execution_id, case_id, lease_id, _json(cleanup), "ACTIVE"),
            )
            return row.lastrowid

    def complete_lease(
        self,
        execution_id: str,
        case_id: str,
        lease_id: str,
        fencing_token: int,
        *,
        cleanup_verified: bool,
    ) -> bool:
        """An old generation may clean ONLY its own exact persisted binding."""
        if type(cleanup_verified) is not bool:
            raise StateError("CLEANUP_PROOF_REQUIRED")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE execution_id=? AND case_id=? AND lease_id=? AND id=?",
                (execution_id, case_id, lease_id, fencing_token),
            ).fetchone()
            if row is None:
                return False
            target = "RELEASED" if cleanup_verified else "QUARANTINED"
            if row["state"] != "ACTIVE":
                return row["state"] == "RELEASED" and cleanup_verified
            connection.execute(
                "UPDATE leases SET state=? WHERE id=?", (target, fencing_token)
            )
            self._outbox(
                connection,
                "LEASE_CLEANUP",
                lease_id,
                {
                    "execution_id": execution_id,
                    "case_id": case_id,
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "cleanup_verified": cleanup_verified,
                },
            )
            return cleanup_verified

    def recoverable_leases(self) -> list[dict]:
        """Recovery reads no Artifact, source repository, Python payload or Docker name."""
        with self._connection() as connection:
            return [
                {**dict(row), "cleanup": json.loads(row["cleanup_json"])}
                for row in connection.execute(
                    "SELECT * FROM leases WHERE state IN ('ACTIVE','QUARANTINED') ORDER BY id"
                )
            ]

    def record_case_result(self, execution_id: str, case_id: str, result: dict) -> None:
        canonical(result, limit=32 * 1024)
        with self._transaction() as connection:
            case = connection.execute(
                "SELECT * FROM cases WHERE execution_id=? AND case_id=?",
                (execution_id, case_id),
            ).fetchone()
            if case is None or case["state"] == "PLANNED":
                raise StateError("CASE_NOT_CLAIMED")
            execution = connection.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            plan = json.loads(execution["plan_json"])
            request = connection.execute(
                "SELECT receipt_json FROM requests WHERE id=?",
                (execution["canonical_request_id"],),
            ).fetchone()
            receipt = json.loads(request[0])
            aggregate_results(plan, receipt, [result], current=False)
            if result["case_id"] != case_id:
                raise StateError("CASE_RESULT_IDENTITY_MISMATCH")
            item = next(item for item in plan["cases"] if item["case_id"] == case_id)
            encoded = _json(result)
            if case["result_json"] is not None and case["result_json"] != encoded:
                raise StateError("CASE_RESULT_IMMUTABLE")
            if (
                result.get("cleanup_verified") is not True
                and result.get("status") == "PASS"
            ):
                raise StateError("UNCLEAN_CASE_CANNOT_PASS")
            lease = connection.execute(
                "SELECT state FROM leases WHERE execution_id=? AND case_id=?",
                (execution_id, case_id),
            ).fetchone()
            if (
                lease is None
                and item["kind"] != "worker_probe"
                and result.get("status") == "PASS"
            ):
                raise StateError("NPU_PASS_REQUIRES_CAPTURED_LEASE")
            if lease and (
                lease["state"] == "ACTIVE"
                or (result.get("status") == "PASS" and lease["state"] != "RELEASED")
            ):
                raise StateError("LEASE_CLEANUP_NOT_COMPLETE")
            connection.execute(
                "UPDATE cases SET state='COMPLETED',result_json=? WHERE execution_id=? AND case_id=?",
                (encoded, execution_id, case_id),
            )
            self._outbox(
                connection, "CASE_RESULT", execution_id + "/" + case_id, result
            )

    def request_reference(self, rid: str) -> dict:
        """Read a retained receipt/digest; no remote API or source import."""
        text(rid, limit=160)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE id=?", (rid,)
            ).fetchone()
            if row is None:
                raise StateError("UNKNOWN_RETAINED_REQUEST")
            return {
                "request_id": row["id"],
                "execution_id": row["execution_id"],
                "plan_digest": row["plan_digest"],
                "receipt": json.loads(row["receipt_json"]),
            }

    def registered_scopes(self, limit: int = 1000) -> list[tuple[str, int]]:
        positive(limit)
        if limit > 1000:
            raise StateError("REGISTERED_SCOPE_LIMIT")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT repo_id,number FROM pr_versions ORDER BY repo_id,number LIMIT ?",
                (limit + 1,),
            ).fetchall()
        if len(rows) > limit:
            raise StateError("REGISTERED_SCOPE_VISIBILITY_INCOMPLETE")
        return [(row["repo_id"], row["number"]) for row in rows]

    def iter_registered_scopes(self, page_size: int = 100):
        """Keyset pages over retained history without holding a DB lock over I/O.

        The upper bound is captured once so newly appended scopes cannot make
        a tick unbounded. Concurrent lower-key inserts are picked up next tick.
        Absence in a scan never implies closure or permission to execute.
        """
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise StateError("INVALID_SCOPE_PAGE_SIZE")
        with self._connection() as connection:
            last = connection.execute(
                "SELECT repo_id,number FROM pr_versions "
                "ORDER BY repo_id DESC,number DESC LIMIT 1"
            ).fetchone()
        if last is None:
            return
        upper = (last["repo_id"], last["number"])
        after = ("", 0)
        while True:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT repo_id,number FROM pr_versions "
                    "WHERE (repo_id,number) > (?,?) AND (repo_id,number) <= (?,?) "
                    "ORDER BY repo_id,number LIMIT ?",
                    (*after, *upper, page_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                after = (row["repo_id"], row["number"])
                yield after

    def pending_outbox(self, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StateError("INVALID_OUTBOX_PAGE_SIZE")
        with self._connection() as connection:
            return [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "entity_id": row["entity_id"],
                    "payload": json.loads(row["payload_json"]),
                    "payload_digest": digest(json.loads(row["payload_json"])),
                }
                for row in connection.execute(
                    "SELECT outbox.* FROM outbox LEFT JOIN outbox_attempts "
                    "ON outbox.id=outbox_attempts.outbox_id "
                    "WHERE acknowledged=0 ORDER BY COALESCE(sequence,0), "
                    "CASE WHEN kind IN ('CANCEL_RUN','CANCEL_EXECUTION','LEASE_CLEANUP') THEN 0 ELSE 1 END,id LIMIT ?",
                    (limit,),
                )
            ]

    def attempted(self, outbox_id: int, payload_digest: str) -> None:
        """Rotate durable retry order before I/O, including crash/lost-response.

        This is not an acknowledgement or execution grant. Fixed slot identity
        and idempotent cancellation callbacks remain responsible for replay.
        """
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            if row is None or digest(json.loads(row[0])) != payload_digest:
                raise StateError("OUTBOX_ATTEMPT_IDENTITY_MISMATCH")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM outbox_attempts"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO outbox_attempts VALUES(?,?) "
                "ON CONFLICT(outbox_id) DO UPDATE SET sequence=excluded.sequence",
                (outbox_id, sequence),
            )

    def acknowledge(self, outbox_id: int, payload_digest: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            if row is None or digest(json.loads(row[0])) != payload_digest:
                raise StateError("OUTBOX_ACK_IDENTITY_MISMATCH")
            connection.execute(
                "UPDATE outbox SET acknowledged=1 WHERE id=?", (outbox_id,)
            )

    def cancel_execution(self, execution_id: str) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise StateError("UNKNOWN_EXECUTION")
            connection.execute(
                "UPDATE executions SET cancel_requested=1 WHERE id=?", (execution_id,)
            )
            self._outbox(
                connection,
                "CANCEL_EXECUTION",
                execution_id,
                {"execution_id": execution_id},
            )

    def execution_snapshot(self, execution_id: str) -> dict:
        """Read one canonical execution and cleanup bindings without network IO."""
        text(execution_id, limit=100)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise StateError("UNKNOWN_EXECUTION")
            request = db.execute(
                "SELECT receipt_json FROM requests WHERE id=?",
                (row["canonical_request_id"],),
            ).fetchone()
            return {
                "id": row["id"],
                "plan": json.loads(row["plan_json"]),
                "receipt": json.loads(request[0]),
                "cancel_requested": bool(row["cancel_requested"]),
                "aggregate_delivered": db.execute(
                    "SELECT execution_id FROM publication_receipts WHERE execution_id=?",
                    (execution_id,),
                ).fetchone()
                is not None,
                "cases": [
                    {
                        "case_id": c["case_id"],
                        "state": c["state"],
                        "result": json.loads(c["result_json"])
                        if c["result_json"] is not None
                        else None,
                    }
                    for c in db.execute(
                        "SELECT * FROM cases WHERE execution_id=? ORDER BY case_id",
                        (execution_id,),
                    )
                ],
                "leases": [
                    {**dict(lease), "cleanup": json.loads(lease["cleanup_json"])}
                    for lease in db.execute(
                        "SELECT * FROM leases WHERE execution_id=? ORDER BY id",
                        (execution_id,),
                    )
                ],
            }

    def iter_authority_requests(
        self, *, now: int, page_size: int = 100, include_expired: bool = False
    ):
        """Page unexpired retained requests, including duplicate and delivered owners.

        Hosted challenges belong to the waiting Run, even if its receipt follows
        a canonical execution. Keep history and bound this pass by initial rowid.
        """
        if type(now) is not int or now < 0 or type(include_expired) is not bool:
            raise StateError("INVALID_AUTHORITY_SCAN_CLOCK")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise StateError("INVALID_AUTHORITY_SCAN_PAGE_SIZE")
        with self._connection() as db:
            upper = db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM requests"
            ).fetchone()[0]
        after = 0
        while after < upper:
            with self._connection() as db:
                rows = db.execute(
                    "SELECT r.rowid,r.id,r.plan_digest,r.receipt_json,r.execution_id "
                    "FROM requests r JOIN executions e ON e.id=r.execution_id "
                    "WHERE r.rowid>? AND r.rowid<=? AND (? OR e.deadline>?) "
                    "ORDER BY r.rowid LIMIT ?",
                    (after, upper, int(include_expired), now, page_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                after = row[0]
                yield {
                    "request_id": row["id"],
                    "plan_digest": row["plan_digest"],
                    "receipt": json.loads(row["receipt_json"]),
                    "execution_id": row["execution_id"],
                }

    def execution_snapshots(self, limit: int = 1000) -> list[dict]:
        positive(limit)
        if limit > 1000:
            raise StateError("EXECUTION_VISIBILITY_LIMIT")
        with self._connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM executions ORDER BY rowid LIMIT ?", (limit + 1,)
                )
            ]
        if len(ids) > limit:
            raise StateError("EXECUTION_VISIBILITY_INCOMPLETE")
        return [self.execution_snapshot(value) for value in ids]

    def claim_terminal_case(self, execution_id: str, case_id: str) -> None:
        """Permit a non-executing terminal report after revocation, never a launch."""
        with self._transaction() as db:
            db.execute(
                "UPDATE cases SET state='CLAIMED' WHERE execution_id=? AND case_id=? AND state='PLANNED'",
                (execution_id, case_id),
            )

    def retry_unlaunched_case(
        self, execution_id: str, case_id: str, *, now: int
    ) -> None:
        """Only external occupancy before captured lease/staging may retry."""
        with self._transaction() as db:
            now = self._locked_decision_now(db, now)
            execution = db.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            lease = db.execute(
                "SELECT id FROM leases WHERE execution_id=? AND case_id=?",
                (execution_id, case_id),
            ).fetchone()
            if (
                execution is None
                or lease is not None
                or not self._current(db, execution, now=now)
            ):
                raise StateError("CLAIM_RETRY_REVOKED_OR_ALREADY_BOUND")
            changed = db.execute(
                "UPDATE cases SET state='PLANNED' WHERE execution_id=? AND case_id=? AND state='CLAIMED' AND result_json IS NULL",
                (execution_id, case_id),
            )
            if changed.rowcount != 1:
                raise StateError("CASE_CANNOT_BE_REPLAYED")

    def assert_lease_active(
        self,
        execution_id: str,
        case_id: str,
        lease_id: str,
        fencing_token: int,
        *,
        now: int,
    ) -> None:
        """Check captured ownership and current generation immediately before use."""
        if type(fencing_token) is not int or fencing_token < 1:
            raise StateError("INVALID_FENCING_TOKEN")
        with self._transaction() as db:
            now = self._locked_decision_now(db, now)
            execution = db.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            lease = db.execute(
                "SELECT * FROM leases WHERE execution_id=? AND case_id=? AND lease_id=? AND id=?",
                (execution_id, case_id, lease_id, fencing_token),
            ).fetchone()
            if (
                execution is None
                or not self._current(db, execution, now=now)
                or lease is None
                or lease["state"] != "ACTIVE"
            ):
                raise StateError("LEASE_OWNERSHIP_OR_GENERATION_REVOKED")

    def pending_execution_snapshots(
        self, *, recovery: bool = False, limit: int = 1000
    ) -> list[dict]:
        """Bounded outstanding work only; completed history is not polled each tick."""
        positive(limit)
        if limit > 1000 or type(recovery) is not bool:
            raise StateError("EXECUTION_VISIBILITY_LIMIT")
        where = (
            "a.execution_id IS NULL OR EXISTS (SELECT 1 FROM leases l WHERE l.execution_id=e.id AND l.state IN ('ACTIVE','QUARANTINED'))"
            if recovery
            else "a.execution_id IS NULL AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.execution_id=e.id AND c.state!='COMPLETED')"
        )
        with self._connection() as db:
            rows = db.execute(
                "SELECT e.id FROM executions e LEFT JOIN publication_receipts a ON a.execution_id=e.id WHERE "
                + where
                + " ORDER BY e.rowid LIMIT ?",
                (limit + 1,),
            ).fetchall()
        if len(rows) > limit:
            raise StateError("EXECUTION_VISIBILITY_INCOMPLETE")
        return [self.execution_snapshot(row[0]) for row in rows]

    def mark_aggregate_delivered(
        self, execution_id: str, receipt: dict, *, now: int
    ) -> None:
        """Acknowledge confirmed fixed-ID aggregate delivery, never infer it from API absence."""
        with self._transaction() as db:
            execution = db.execute(
                "SELECT * FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
            if execution is None:
                raise StateError("UNKNOWN_EXECUTION")
            request = db.execute(
                "SELECT * FROM requests WHERE id=?",
                (execution["canonical_request_id"],),
            ).fetchone()
            if request is None or canonical(receipt) != canonical(
                json.loads(request["receipt_json"])
            ):
                raise StateError("PUBLICATION_RECEIPT_MISMATCH")
            plan = json.loads(execution["plan_json"])
            cases = db.execute(
                "SELECT * FROM cases WHERE execution_id=?", (execution_id,)
            ).fetchall()
            if len(cases) != len(plan["cases"]) or any(
                c["state"] != "COMPLETED" or c["result_json"] is None for c in cases
            ):
                raise StateError("INCOMPLETE_AGGREGATE_CANNOT_BE_ACKNOWLEDGED")
            encoded = digest(receipt)
            old = db.execute(
                "SELECT * FROM publication_receipts WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if old and (
                old["plan_digest"] != plan["plan_digest"]
                or old["receipt_digest"] != encoded
            ):
                raise StateError("PUBLICATION_RECEIPT_IMMUTABLE")
            db.execute(
                "INSERT OR IGNORE INTO publication_receipts VALUES(?,?,?,?)",
                (execution_id, plan["plan_digest"], encoded, now),
            )
