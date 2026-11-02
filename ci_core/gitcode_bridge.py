"""Durable, fail-closed GitCode-to-GitHub dispatch transport.

The bridge records the complete intent before POST. A lost response is resolved
by the current REST API's returned Run ID, or one bounded nonce-bound query.
It never blindly retries POST, picks the latest run, edits a default branch,
imports PR code, or claims that dispatch success is a test/merge result.

The journal must survive the hosting Action's retry/restart. Hosting-platform
artifact persistence and authenticated event revision verification are separate
release gates; an ephemeral SQLite file alone does not satisfy those gates.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from ci_core.gitcode_contract import (
    BRIDGE_KEYS,
    MODEL_REPOSITORIES,
    ControlPolicy,
    canonical,
    digest,
    positive,
    select_cases,
    shape,
    text,
    validate_pipeline,
    validate_pr,
)
from ci_core.gitcode_prepare import verify_control_release


class BridgeError(ValueError):
    pass


class DispatchJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise BridgeError("UNKNOWN_DISPATCH_JOURNAL_VERSION")
            if version == 0:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise BridgeError("UNVERSIONED_DISPATCH_JOURNAL")
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE dispatches (
                      intent_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL,
                      fingerprint TEXT NOT NULL, run_id INTEGER
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def begin(self, intent: dict) -> tuple[bool, int | None]:
        key, encoded = (
            intent["bridge"]["dispatch_intent_id"],
            canonical(intent).decode(),
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM dispatches WHERE intent_id=?", (key,)
                ).fetchone()
                if row:
                    if row["intent_json"] != encoded or row["fingerprint"] != digest(
                        intent
                    ):
                        raise BridgeError("DISPATCH_INTENT_IMMUTABLE")
                    db.commit()
                    return False, row["run_id"]
                db.execute(
                    "INSERT INTO dispatches VALUES(?,?,?,NULL)",
                    (key, encoded, digest(intent)),
                )
                db.commit()
                return True, None
            except BaseException:
                db.rollback()
                raise

    def bind(self, intent: dict, run_id: int) -> None:
        positive(run_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                key = intent["bridge"]["dispatch_intent_id"]
                row = db.execute(
                    "SELECT * FROM dispatches WHERE intent_id=?", (key,)
                ).fetchone()
                if (
                    row is None
                    or row["fingerprint"] != digest(intent)
                    or row["run_id"] not in (None, run_id)
                ):
                    raise BridgeError("DISPATCH_RUN_BINDING_IMMUTABLE")
                db.execute(
                    "UPDATE dispatches SET run_id=? WHERE intent_id=?", (run_id, key)
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise


class GitCodeBridge:
    def __init__(
        self,
        *,
        policy: ControlPolicy,
        github,
        personal,
        upstream,
        journal: DispatchJournal,
        catalog: dict,
        github_actor: str,
    ):
        self.policy, self.github = policy, github
        self.personal, self.upstream, self.journal = personal, upstream, journal
        self.catalog, self.github_actor = catalog, text(github_actor, limit=100)
        if (
            github.repo_id != policy.github_repo_id
            or github.repo != MODEL_REPOSITORIES[policy.github_repo_id][0]
        ):
            raise BridgeError("BRIDGE_GITHUB_CLIENT_IDENTITY_MISMATCH")
        if (
            personal.repo_id != policy.gitcode_repo_id
            or upstream.repo_id != policy.upstream_repo_id
        ):
            raise BridgeError("BRIDGE_GITCODE_CLIENT_IDENTITY_MISMATCH")

    def release_observations(self):
        policy = self.policy
        default_ref = MODEL_REPOSITORIES[policy.github_repo_id][1]
        return {
            "upstream": self.upstream.control_tip(),
            "repository": self.github.request("GET", ""),
            "branch": self.github.request("GET", "/branches/" + default_ref),
            "workflow": self.github.request(
                "GET", f"/actions/workflows/{policy.workflow_id}"
            ),
        }

    def validate_intent(self, intent: dict, *, now: int):
        policy = self.policy
        if not policy.enabled:
            raise BridgeError("GITCODE_PROVIDER_DISABLED")
        shape(
            intent,
            {
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
        )
        if (
            type(intent["schema"]) is not int
            or intent["schema"] != 1
            or intent["source_provider"] != "gitcode"
        ):
            raise BridgeError("INVALID_DISPATCH_INTENT_PROTOCOL")
        validate_pr(
            intent["pr"],
            allowed_repo_id=policy.gitcode_repo_id,
            allowed_refs=policy.allowed_base_refs,
        )
        if (
            intent["control_repo_id"] != policy.github_repo_id
            or intent["control_sha"] != policy.control_sha
            or intent["workflow_path"] != policy.workflow_path
            or intent["pipeline"] not in ("smoke", "nightly")
        ):
            raise BridgeError("INTENT_CONTROL_RELEASE_MISMATCH")
        validate_pipeline(
            intent["pipeline"],
            intent["workflow_path"],
            select_cases(intent["selectors"], self.catalog),
        )
        shape(intent["bridge"], BRIDGE_KEYS)
        for key, value in intent["bridge"].items():
            positive(value) if key in ("bridge_attempt", "event_created_at") else text(
                value, limit=160
            )
        bridge = intent["bridge"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", bridge["bridge_nonce"]):
            raise BridgeError("INVALID_BRIDGE_NONCE")
        positive(intent["deadline"])
        if (
            bridge["actor"] not in policy.allowed_actors
            or not bridge["event_created_at"] <= now < intent["deadline"]
        ):
            raise BridgeError("INTENT_ACTOR_OR_DEADLINE_INVALID")
        if (
            intent["deadline"] - bridge["event_created_at"]
            > policy.max_duration_seconds
        ):
            raise BridgeError("INTENT_DEADLINE_TOO_LONG")
        current = self.personal.open_pull_snapshot(
            intent["pr"]["number"],
            allowed_base_refs=policy.allowed_base_refs,
            allowed_actors=policy.allowed_actors,
        )
        if current != {"identity": intent["pr"], "state": "open", "authorized": True}:
            raise BridgeError("INTENT_PR_VERSION_STALE")
        verify_control_release(policy, **self.release_observations())
        canonical(intent, limit=16 * 1024)

    @staticmethod
    def run_name(intent):
        return "lite-gitcode:" + intent["bridge"]["bridge_nonce"]

    def validate_run(self, run, intent):
        policy = self.policy
        repo, branch = MODEL_REPOSITORIES[policy.github_repo_id]
        if not isinstance(run, dict):
            raise BridgeError("INVALID_DISPATCH_RUN_RESPONSE")
        positive(run.get("id"))
        if (
            run.get("run_attempt") != 1
            or run.get("event") != "workflow_dispatch"
            or (run.get("repository") or {}).get("id") != policy.github_repo_id
            or (run.get("repository") or {}).get("full_name") != repo
            or run.get("head_sha") != policy.control_sha
            or run.get("head_branch") != branch
            or run.get("workflow_id") != policy.workflow_id
            or run.get("path")
            not in (policy.workflow_path, policy.workflow_path + "@" + branch)
            or run.get("display_title") != self.run_name(intent)
            or (run.get("actor") or {}).get("login") != self.github_actor
        ):
            raise BridgeError("DISPATCH_RUN_IDENTITY_MISMATCH")
        return run["id"]

    def recover(self, intent):
        """Recover one exact nonce, not 'the newest' run for a commit."""
        start = dt.datetime.fromtimestamp(
            intent["bridge"]["event_created_at"] - 60, dt.timezone.utc
        )
        end = dt.datetime.fromtimestamp(intent["deadline"], dt.timezone.utc)
        query = urlencode(
            {
                "event": "workflow_dispatch",
                "head_sha": self.policy.control_sha,
                "branch": MODEL_REPOSITORIES[self.policy.github_repo_id][1],
                "actor": self.github_actor,
                "per_page": 100,
                "page": 1,
                "created": start.isoformat().replace("+00:00", "Z")
                + ".."
                + end.isoformat().replace("+00:00", "Z"),
            }
        )
        response = self.github.request(
            "GET", f"/actions/workflows/{self.policy.workflow_id}/runs?{query}"
        )
        if (
            not isinstance(response, dict)
            or type(response.get("total_count")) is not int
            or not isinstance(response.get("workflow_runs"), list)
        ):
            raise BridgeError("INVALID_DISPATCH_RECOVERY_RESPONSE")
        runs = response["workflow_runs"]
        if response["total_count"] != len(runs) or len(runs) > 100:
            raise BridgeError("DISPATCH_RECOVERY_SCOPE_INCOMPLETE")
        matches = [
            run for run in runs if run.get("display_title") == self.run_name(intent)
        ]
        if len(matches) != 1:
            raise BridgeError("DISPATCH_UNKNOWN_OR_AMBIGUOUS")
        return self.validate_run(matches[0], intent)

    def dispatch(self, intent: dict, *, now: int) -> dict:
        self.validate_intent(intent, now=now)
        owns_post, run_id = self.journal.begin(intent)
        if run_id is None and owns_post:
            payload = {
                "ref": MODEL_REPOSITORIES[self.policy.github_repo_id][1],
                "return_run_details": True,
                "inputs": {
                    "source_provider": "gitcode",
                    "gitcode_request": canonical(intent).decode(),
                },
            }
            try:
                response = self.github.request(
                    "POST",
                    f"/actions/workflows/{self.policy.workflow_id}/dispatches",
                    payload,
                )
            except (OSError, TimeoutError):
                response = None
            if (
                isinstance(response, dict)
                and type(response.get("workflow_run_id")) is int
            ):
                run_id = positive(response["workflow_run_id"])
                expected_api = f"https://api.github.com/repos/{self.github.repo}/actions/runs/{run_id}"
                expected_html = (
                    f"https://github.com/{self.github.repo}/actions/runs/{run_id}"
                )
                if (
                    response.get("run_url") != expected_api
                    or response.get("html_url") != expected_html
                ):
                    raise BridgeError("DISPATCH_RETURNED_URL_IDENTITY_MISMATCH")
                # Remember the returned ID before another network request can fail.
                self.journal.bind(intent, run_id)
        if run_id is None:
            run_id = self.recover(intent)
            self.journal.bind(intent, run_id)
        run = self.github.request("GET", f"/actions/runs/{run_id}")
        self.validate_run(run, intent)
        return {
            "run_id": run_id,
            "run_attempt": 1,
            "repository_id": self.policy.github_repo_id,
            "bridge_nonce": intent["bridge"]["bridge_nonce"],
            "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
            "control_sha": self.policy.control_sha,
            "test_result": "NOT_YET_AVAILABLE",
            "merge_gate": "NOT_PROVIDED",
        }
