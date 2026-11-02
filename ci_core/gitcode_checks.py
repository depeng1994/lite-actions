"""Schema-4 Check Run channel with durable unknown-create recovery.

Only slot creation recovery uses a bounded, precisely filtered listing. Once a
slot ID is bound, readers and writers use that ID forever, including on 404.
The caller supplies an independently authorized, repository-bound App client.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    aggregate_results,
    canonical,
    digest,
    external_id,
    load_json,
    planned_slot_ids,
    positive,
    request_id,
    source_identity,
    validate_receipt,
)


class ChannelError(ValueError):
    pass


class CreationJournal:
    """Durable at-most-one POST ownership, including process-crash windows."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ChannelError("UNKNOWN_CHECK_JOURNAL_VERSION")
            if version == 0:
                if connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise ChannelError("UNVERSIONED_CHECK_JOURNAL")
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE creations (
                      identity TEXT PRIMARY KEY, stage TEXT NOT NULL,
                      slot_json TEXT
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
        self.path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def begin(self, identity: str) -> tuple[bool, dict | None]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM creations WHERE identity=?", (identity,)
                ).fetchone()
                if row:
                    connection.commit()
                    return False, json.loads(row["slot_json"]) if row[
                        "slot_json"
                    ] else None
                # Persist UNKNOWN before POST. A crash here may lose liveness,
                # but can never cause an automatic second create request.
                connection.execute(
                    "INSERT INTO creations VALUES(?,'UNKNOWN',NULL)", (identity,)
                )
                connection.commit()
                return True, None
            except BaseException:
                connection.rollback()
                raise

    def bind(self, identity: str, slot: dict) -> None:
        encoded = canonical(slot).decode()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM creations WHERE identity=?", (identity,)
                ).fetchone()
                if row is None or (
                    row["slot_json"] is not None and row["slot_json"] != encoded
                ):
                    raise ChannelError("CHECK_SLOT_BINDING_IMMUTABLE")
                connection.execute(
                    "UPDATE creations SET stage='BOUND',slot_json=? WHERE identity=?",
                    (encoded, identity),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


class CheckRunChannel:
    def __init__(
        self,
        client,
        *,
        repo_id: int,
        app_id: int,
        journal: CreationJournal | None = None,
    ):
        positive(app_id)
        if repo_id not in MODEL_REPOSITORIES:
            raise ChannelError("CHECK_REPOSITORY_NOT_AUTHORIZED")
        expected_repo = MODEL_REPOSITORIES[repo_id][0]
        if client.repo_id != repo_id or client.repo != expected_repo:
            raise ChannelError("CHECK_CLIENT_REPOSITORY_MISMATCH")
        self.client, self.repo_id, self.app_id = client, repo_id, app_id
        self.journal = journal

    def _plan(self, plan):
        gh = plan["github"]
        if (
            gh["repo_id"] != self.repo_id
            or gh["repo"] != self.client.repo
            or gh["run_attempt"] != 1
        ):
            raise ChannelError("CHECK_PLAN_IDENTITY_MISMATCH")
        positive(gh["run_id"])

    @staticmethod
    def name(plan, case_id):
        gh = plan["github"]
        return f"lite-v4-{gh['run_id']}-{gh['run_attempt']}-{case_id}-{plan['request_spec_digest'][:12]}"

    def _binding(self, plan, case_id, check_id):
        positive(check_id)
        self._plan(plan)
        if case_id not in planned_slot_ids(plan):
            raise ChannelError("CHECK_CASE_NOT_PLANNED")
        gh = plan["github"]
        return {
            "id": check_id,
            "app_id": self.app_id,
            "repo_id": self.repo_id,
            "control_sha": plan["control"]["control_sha"],
            "run_id": gh["run_id"],
            "run_attempt": gh["run_attempt"],
            "case_id": case_id,
            "external_id": external_id(plan, case_id),
        }

    def _observe(self, plan, case_id, check, *, expected_id=None):
        if not isinstance(check, dict) or type(check.get("id")) is not int:
            raise ChannelError("INVALID_CHECK_RESPONSE")
        slot = self._binding(plan, case_id, check["id"])
        if (
            check.get("name") != self.name(plan, case_id)
            or check.get("head_sha") != slot["control_sha"]
            or (check.get("app") or {}).get("id") != self.app_id
            or check.get("external_id") != slot["external_id"]
            or (expected_id is not None and check["id"] != expected_id)
        ):
            raise ChannelError("CHECK_SLOT_IDENTITY_MISMATCH")
        # The REST response URL must be this repository, not a caller-supplied URL.
        expected_url = (
            f"https://api.github.com/repos/{self.client.repo}/check-runs/{check['id']}"
        )
        if check.get("url") != expected_url:
            raise ChannelError("CHECK_REPOSITORY_RESPONSE_MISMATCH")
        return slot

    def _recover_creation(self, plan, case_id):
        sha = plan["control"]["control_sha"]
        # GitHub truncates the reference endpoint above 1000 suites. Do not
        # mistake a truncated name-filtered response for a complete search.
        suites = self.client.request(
            "GET", f"/commits/{sha}/check-suites?per_page=1&page=1"
        )
        count = suites.get("total_count") if isinstance(suites, dict) else None
        if type(count) is not int or not 0 <= count < 1000:
            raise ChannelError("CHECK_RECOVERY_SCOPE_NOT_PROVABLY_COMPLETE")
        query = urlencode(
            {
                "check_name": self.name(plan, case_id),
                "app_id": self.app_id,
                "filter": "all",
                "per_page": 100,
                "page": 1,
            }
        )
        data = self.client.request("GET", f"/commits/{sha}/check-runs?{query}")
        if (
            not isinstance(data, dict)
            or type(data.get("total_count")) is not int
            or not isinstance(data.get("check_runs"), list)
        ):
            raise ChannelError("INVALID_CHECK_RECOVERY_RESPONSE")
        checks = data["check_runs"]
        if data["total_count"] != len(checks) or len(checks) != 1:
            raise ChannelError("CHECK_CREATE_UNKNOWN_OR_AMBIGUOUS")
        return self._observe(plan, case_id, checks[0])

    def create_slot(self, plan, case_id):
        self._plan(plan)
        if self.journal is None:
            raise ChannelError("DURABLE_CREATION_JOURNAL_REQUIRED")
        identity = digest(
            {
                "repo_id": self.repo_id,
                "control_sha": plan["control"]["control_sha"],
                "app_id": self.app_id,
                "external_id": external_id(plan, case_id),
            }
        )
        owns_post, slot = self.journal.begin(identity)
        if slot is not None:
            check = self.client.request("GET", f"/check-runs/{slot['id']}")
            observed = self._observe(plan, case_id, check, expected_id=slot["id"])
            if canonical(observed) != canonical(slot):
                raise ChannelError("CHECK_SLOT_BINDING_IMMUTABLE")
            return slot
        if owns_post:
            gh = plan["github"]
            payload = {
                "name": self.name(plan, case_id),
                "head_sha": plan["control"]["control_sha"],
                "external_id": external_id(plan, case_id),
                "status": "queued",
                "details_url": f"https://github.com/{self.client.repo}/actions/runs/{gh['run_id']}",
                "output": {
                    "title": "Awaiting verified Blue admission",
                    "summary": canonical(
                        {
                            "schema": 4,
                            "phase": "SLOT_CREATED",
                            "request_id": request_id(plan),
                            "case_id": case_id,
                            "request_spec_digest": plan["request_spec_digest"],
                        }
                    ).decode(),
                },
            }
            try:
                check = self.client.request("POST", "/check-runs", payload)
            except (OSError, TimeoutError):
                # A transport failure is not evidence that GitHub did not commit.
                slot = self._recover_creation(plan, case_id)
            else:
                slot = self._observe(plan, case_id, check)
        else:
            slot = self._recover_creation(plan, case_id)
        self.journal.bind(identity, slot)
        return slot

    def precreate(self, plan):
        result = {}
        for case_id in sorted(planned_slot_ids(plan)):
            result[case_id] = self.create_slot(plan, case_id)
        return result

    def read_slot(self, plan, case_id):
        self._plan(plan)
        slot = plan["result_slots"][case_id]
        check = self.client.request("GET", f"/check-runs/{positive(slot['id'])}")
        observed = self._observe(plan, case_id, check, expected_id=slot["id"])
        if canonical(observed) != canonical(slot):
            raise ChannelError("CHECK_PLAN_SLOT_MISMATCH")
        return check

    @staticmethod
    def _payload(check):
        summary = (check.get("output") or {}).get("summary")
        if not isinstance(summary, str):
            raise ChannelError("MISSING_CHECK_RESULT_ENVELOPE")
        return load_json(summary.encode(), limit=32 * 1024)

    def _publish(self, plan, case_id, payload, *, terminal=False, conclusion=None):
        canonical(payload, limit=32 * 1024)
        check = self.read_slot(plan, case_id)
        current = self._payload(check)
        if current.get("phase") != "SLOT_CREATED":
            if current.get("plan_digest") != plan["plan_digest"]:
                raise ChannelError("CHECK_PLAN_BINDING_IMMUTABLE")
            if canonical(current.get("receipt")) != canonical(payload.get("receipt")):
                raise ChannelError("CHECK_RECEIPT_BINDING_IMMUTABLE")
        if check.get("status") == "completed":
            if (
                canonical(current) == canonical(payload)
                and check.get("conclusion") == conclusion
            ):
                return check
            raise ChannelError("TERMINAL_CHECK_RESULT_IMMUTABLE")
        phases = {
            "SLOT_CREATED": 0,
            "ADMITTED": 1,
            "CASE_RESULT": 2,
            "AGGREGATE_RESULT": 2,
            "AUTHORITY_RESPONSE": 2,
            "AUTHORITY_CLOSED": 3,
        }
        if phases.get(current.get("phase"), -1) > phases.get(payload.get("phase"), -1):
            raise ChannelError("CHECK_PHASE_REGRESSION")
        if current.get("phase") == "SLOT_CREATED":
            expected = {
                "schema": 4,
                "phase": "SLOT_CREATED",
                "request_id": request_id(plan),
                "case_id": case_id,
                "request_spec_digest": plan["request_spec_digest"],
            }
            if canonical(current) != canonical(expected):
                raise ChannelError("CREATED_SLOT_ENVELOPE_MISMATCH")
        patch = {
            "status": "completed" if terminal else "in_progress",
            "output": {
                "title": "Versioned merge-result test (not atomic merge permission)",
                "summary": canonical(payload, limit=32 * 1024).decode(),
            },
        }
        if terminal:
            if conclusion not in (
                "success",
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
                "neutral",
            ):
                raise ChannelError("INVALID_CHECK_CONCLUSION")
            patch["conclusion"] = conclusion
        updated = self.client.request("PATCH", f"/check-runs/{check['id']}", patch)
        self._observe(plan, case_id, updated, expected_id=check["id"])
        if canonical(self._payload(updated)) != canonical(payload):
            raise ChannelError("CHECK_RESULT_UPDATE_NOT_CONFIRMED")
        return updated

    def publish_receipt(self, plan, receipt, *, canonical_execution=None):
        validate_receipt(plan, receipt)
        if receipt["duplicate"]:
            if not isinstance(canonical_execution, dict) or set(
                canonical_execution
            ) != {"request_id", "plan_digest", "github", "result_slots"}:
                raise ChannelError("CANONICAL_RESULT_POINTER_REQUIRED")
        elif canonical_execution is not None:
            raise ChannelError("UNEXPECTED_CANONICAL_POINTER")
        payload = {
            "schema": 4,
            "phase": "ADMITTED",
            "plan_digest": plan["plan_digest"],
            "receipt": receipt,
            "canonical": canonical_execution,
        }
        return self._publish(plan, "aggregate", payload)

    def read_receipt(self, plan):
        check = self.read_slot(plan, "aggregate")
        payload = self._payload(check)
        if payload.get("phase") == "SLOT_CREATED":
            return None
        if (
            payload.get("schema") != 4
            or payload.get("phase") not in ("ADMITTED", "AGGREGATE_RESULT")
            or payload.get("plan_digest") != plan["plan_digest"]
        ):
            raise ChannelError("INVALID_ADMISSION_ENVELOPE")
        validate_receipt(plan, payload["receipt"])
        return payload

    def publish_case(self, plan, receipt, result):
        aggregate_results(plan, receipt, [result], current=False)
        case_id = result["case_id"]
        status = result["status"]
        conclusion = (
            "success"
            if status == "PASS" and result["cleanup_verified"]
            else {
                "CANCELLED": "cancelled",
                "TIMED_OUT": "timed_out",
                "NOT_RUN": "action_required",
            }.get(status, "failure")
        )
        payload = {
            "schema": 4,
            "phase": "CASE_RESULT",
            "plan_digest": plan["plan_digest"],
            "receipt": receipt,
            "result": result,
        }
        return self._publish(
            plan, case_id, payload, terminal=True, conclusion=conclusion
        )

    def read_case(self, plan, receipt, case_id):
        check = self.read_slot(plan, case_id)
        payload = self._payload(check)
        if check.get("status") != "completed":
            return None
        if (
            payload.get("schema") != 4
            or payload.get("phase") != "CASE_RESULT"
            or payload.get("plan_digest") != plan["plan_digest"]
        ):
            raise ChannelError("INVALID_CASE_RESULT_ENVELOPE")
        if canonical(payload.get("receipt")) != canonical(receipt):
            raise ChannelError("CHECK_RECEIPT_BINDING_IMMUTABLE")
        result = payload["result"]
        aggregate_results(plan, receipt, [result], current=False)
        if result["case_id"] != case_id:
            raise ChannelError("CROSS_CASE_RESULT")
        expected_success = result["status"] == "PASS" and result["cleanup_verified"]
        if (check.get("conclusion") == "success") != expected_success:
            raise ChannelError("CHECK_CONCLUSION_PAYLOAD_CONFLICT")
        return result

    def publish_aggregate(self, plan, receipt, results, *, current):
        outcome = aggregate_results(plan, receipt, results, current=current)
        payload = {
            "schema": 4,
            "phase": "AGGREGATE_RESULT",
            "plan_digest": plan["plan_digest"],
            "receipt": receipt,
            "outcome": outcome,
            "identity": source_identity(plan),
        }
        terminal = outcome["test_result"] != "PENDING"
        conclusion = (
            None
            if not terminal
            else ("success" if outcome["test_result"] == "PASS" else "failure")
        )
        return self._publish(
            plan, "aggregate", payload, terminal=terminal, conclusion=conclusion
        )
