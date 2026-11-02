"""Authenticated observed-history authority for ONE trusted Bridge/Blue host.

The revision below is a durable observation-ledger revision, NOT a GitCode
platform revision. Unobserved A->B->A cannot be detected. Unknown incoming
Action intents are never enrolled from current H/B. Manual/periodic intents
created here do NOT count as proof of a GitCode Actions-triggered execution.
Hosted runners cannot instantiate a fresh ledger to recreate these proofs.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    canonical,
    digest,
    positive,
    select_cases,
    text,
    validate_pipeline,
)
from ci_core.gitcode_state import Authority, Coordinator, EventProof


class PlatformError(ValueError):
    pass


class TerminalActionRevoked(PlatformError):
    pass


class ObservedGitCodeAuthority:
    def __init__(
        self,
        client,
        policy,
        catalog,
        journal: Path,
        clock=time.time,
        *,
        attestation_policy=None,
        manual_capture_policy=None,
        action_client=None,
        coordinator=None,
    ):
        if client.repo_id != policy.gitcode_repo_id:
            raise PlatformError("AUTHORITY_CLIENT_SCOPE_MISMATCH")
        self.client, self.policy, self.catalog, self.clock = (
            client,
            policy,
            catalog,
            clock,
        )
        self.attestation_policy = attestation_policy
        self.manual_capture_policy = manual_capture_policy
        self.action_client = action_client or client
        self.path = Path(journal)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise PlatformError("TRUSTED_ABSOLUTE_LEDGER_PATH_REQUIRED")
        if (
            (attestation_policy is not None and attestation_policy.enabled)
            or (manual_capture_policy is not None and manual_capture_policy.enabled)
        ) and coordinator is None:
            raise PlatformError("SIGNED_INGRESS_REQUIRES_SHARED_COORDINATOR")
        if self.path.exists():
            with sqlite3.connect(self.path) as old:
                tables = {
                    row[0]
                    for row in old.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            if "observations" in tables and "pr_versions" not in tables:
                raise PlatformError(
                    "LEGACY_AUTHORITY_LEDGER_REQUIRES_REVIEWED_MIGRATION"
                )
        self.coordinator = coordinator or Coordinator(self.path)
        if self.coordinator.path.resolve() != self.path.resolve():
            raise PlatformError("AUTHORITY_AND_COORDINATOR_DATABASE_MUST_MATCH")

    @contextmanager
    def connection(self):
        with self.coordinator._connection() as db:
            yield db

    @contextmanager
    def transaction(self):
        with self.coordinator._transaction() as db:
            yield db

    def _observe(self, db, number):
        positive(number)
        # Authenticated network read is serialized with persistent observation.
        try:
            snapshot = self.client.authority_snapshot(
                number,
                allowed_base_refs=self.policy.allowed_base_refs,
                allowed_actors=self.policy.allowed_actors,
            )
        except Exception:
            # Only API-read failure is committed as an availability refusal.
            # Any failure after mutation still rolls back the whole transaction.
            db.execute(
                "UPDATE pr_versions SET available=0 WHERE repo_id=? AND number=?",
                (self.policy.gitcode_repo_id, number),
            )
            db.commit()
            raise
        encoded = canonical(snapshot).decode()
        row = db.execute(
            "SELECT * FROM source_observations WHERE repo_id=? AND number=?",
            (self.policy.gitcode_repo_id, number),
        ).fetchone()
        revision = 1 if row is None else row["revision"]
        if row is not None and encoded != row["snapshot"]:
            revision += 1
            db.execute(
                "UPDATE source_intents SET revoked=1 WHERE repo_id=? AND number=?",
                (self.policy.gitcode_repo_id, number),
            )
            db.execute(
                "INSERT INTO source_invalidations VALUES(?,?,?,?)",
                (self.policy.gitcode_repo_id, number, revision, int(self.clock())),
            )
        db.execute(
            "INSERT INTO source_observations VALUES(?,?,?,?) ON CONFLICT(repo_id,number) "
            "DO UPDATE SET revision=excluded.revision,snapshot=excluded.snapshot",
            (self.policy.gitcode_repo_id, number, revision, encoded),
        )
        authority = Authority(
            snapshot["identity"], revision, snapshot["state"], snapshot["authorized"]
        )
        self.coordinator._reconcile_authority(
            db, self.policy.gitcode_repo_id, number, authority, now=int(self.clock())
        )
        return authority

    def _supersede(self, db, number, pipeline):
        db.execute(
            "UPDATE source_intents SET revoked=1 WHERE repo_id=? AND number=? AND json_extract(intent,'$.pipeline')=?",
            (self.policy.gitcode_repo_id, number, pipeline),
        )
        self.coordinator._revoke(
            db, (self.policy.gitcode_repo_id, number), pipeline=pipeline
        )

    def refresh_coordinator(self, repo_id, number):
        if repo_id != self.policy.gitcode_repo_id:
            raise PlatformError("UNAUTHORIZED_AUTHORITY_SCOPE")
        with self.transaction() as db:
            self._observe(db, number)
            return db.execute(
                "SELECT generation FROM pr_versions WHERE repo_id=? AND number=?",
                (repo_id, number),
            ).fetchone()[0]

    def read_snapshot(self, number):
        """Persist the exact authenticated observation before reader validation."""
        authority = self.read_authority(self.policy.gitcode_repo_id, number)
        return {
            "identity": authority.pr,
            "state": authority.state,
            "authorized": authority.authorized,
        }

    def read_authority(self, repo_id, number):
        if repo_id != self.policy.gitcode_repo_id:
            raise PlatformError("UNAUTHORIZED_AUTHORITY_SCOPE")
        with self.transaction() as db:
            return self._observe(db, number)

    def create_intent(
        self,
        number,
        selectors,
        *,
        pipeline,
        rerun_intent_id="initial",
        trigger_kind="periodic",
    ):
        if not self.policy.enabled:
            raise PlatformError("GITCODE_PROVIDER_DISABLED")
        if trigger_kind not in ("manual", "periodic"):
            raise PlatformError("REAL_ACTION_IDENTITY_ADAPTER_REQUIRED")
        rerun = text(rerun_intent_id, limit=160)
        if trigger_kind == "periodic" and rerun != "initial":
            raise PlatformError("PERIODIC_CANNOT_AUTHORIZE_RERUN")
        validate_pipeline(
            pipeline, self.policy.workflow_path, select_cases(selectors, self.catalog)
        )
        actor = self.client.authenticated_actor()
        if actor not in self.policy.allowed_actors:
            raise PlatformError("AUTHENTICATED_OPERATOR_NOT_AUTHORIZED")
        with self.transaction() as db:
            authority = self._observe(db, number)
            if authority.state != "open" or not authority.authorized:
                db.commit()  # Observation/revocation must survive refusal.
                raise PlatformError("PR_NOT_AUTHORIZED_FOR_INTENT")
            if trigger_kind == "periodic":
                # Recover the latest authorized rerun of the SAME effective
                # specification after a crash before dispatch; do not resurrect
                # the initial superseded intent or create a fresh generation.
                active = db.execute(
                    "SELECT intent,semantic FROM source_intents WHERE repo_id=? AND number=? AND revision=? AND revoked=0",
                    (self.policy.gitcode_repo_id, number, authority.revision),
                ).fetchall()
                matches = []
                for active_row in active:
                    value = json.loads(active_row["intent"])
                    if (
                        value["pipeline"] == pipeline
                        and value["control_sha"] == self.policy.control_sha
                        and value["workflow_path"] == self.policy.workflow_path
                        and canonical(value["selectors"]) == canonical(selectors)
                        and active_row["semantic"]
                        == digest(
                            {
                                "pr": authority.pr,
                                "revision": authority.revision,
                                "selectors": selectors,
                                "pipeline": pipeline,
                                "policy": self.policy.__dict__,
                                "rerun": value["bridge"]["rerun_intent_id"],
                                "actor": actor,
                            }
                        )
                    ):
                        matches.append(value)
                if len(matches) > 1:
                    raise PlatformError("AMBIGUOUS_ACTIVE_TRUSTED_INTENT")
                if matches:
                    return matches[0]
            semantic = digest(
                {
                    "pr": authority.pr,
                    "revision": authority.revision,
                    "selectors": selectors,
                    "pipeline": pipeline,
                    "policy": self.policy.__dict__,
                    "rerun": rerun,
                    "actor": actor,
                }
            )
            row = db.execute(
                "SELECT * FROM source_intents WHERE semantic=?", (semantic,)
            ).fetchone()
            if row is not None:
                if row["revoked"]:
                    raise PlatformError("REVOKED_INTENT_CANNOT_BE_RECREATED")
                return json.loads(row["intent"])
            now = int(self.clock())
            positive(now)
            ident = uuid.uuid4().hex
            intent = {
                "schema": 1,
                "source_provider": "gitcode",
                "pr": authority.pr,
                "control_repo_id": self.policy.github_repo_id,
                "control_sha": self.policy.control_sha,
                "workflow_path": self.policy.workflow_path,
                "pipeline": pipeline,
                "selectors": selectors,
                "bridge": {
                    "bridge_event_id": "observed-" + ident,
                    "bridge_action_run": "observer-" + ident,
                    "bridge_attempt": 1,
                    "actor": actor,
                    "bridge_nonce": ident,
                    "source_snapshot_id": "observation-" + ident,
                    "dispatch_intent_id": "intent-" + ident,
                    "rerun_intent_id": rerun,
                    "event_created_at": now,
                },
                "deadline": now + self.policy.max_duration_seconds,
            }
            # A new selected specification/rerun owns publication for this
            # pipeline immediately, even before its GitHub Run is registered.
            self._supersede(db, number, pipeline)
            db.execute(
                "INSERT INTO source_intents(repo_id,id,semantic,number,revision,intent,trigger_kind,actor,authorized_rerun) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.policy.gitcode_repo_id,
                    intent["bridge"]["dispatch_intent_id"],
                    semantic,
                    number,
                    authority.revision,
                    canonical(intent).decode(),
                    trigger_kind,
                    actor,
                    int(rerun != "initial"),
                ),
            )
            return intent

    def _action_observation(self, attested, *, enrolling):
        if getattr(attested, "capture_kind", None) == "manual_api":
            from ci_core.gitcode_manual_capture import (
                ManualCaptureError,
                verify_manual_capture,
            )

            if (
                self.manual_capture_policy is None
                or attested.key_id != self.manual_capture_policy.key_id
            ):
                raise PlatformError("MANUAL_CAPTURE_POLICY_RETIRED")
            try:
                current = verify_manual_capture(
                    attested.intent,
                    self.manual_capture_policy,
                    self.action_client,
                    now=int(self.clock()),
                    first_enrollment=False,
                )
            except ManualCaptureError as exc:
                raise TerminalActionRevoked("MANUAL_SOURCE_TERMINALLY_REVOKED") from exc
            if current.envelope_digest != attested.envelope_digest:
                raise TerminalActionRevoked("MANUAL_IMMUTABLE_CAPTURE_CHANGED")
            return current
        policy = self.attestation_policy
        if policy is None or not policy.enabled or attested.key_id != policy.key_id:
            raise PlatformError("TRUSTED_EMITTER_NOT_ENABLED_OR_KEY_RETIRED")
        if self.action_client.repo_id != attested.issuer_repo_id:
            raise PlatformError("ACTION_ISSUER_REPOSITORY_MISMATCH")
        if attested.attempt != 1:
            raise PlatformError("NATIVE_ACTION_RERUN_NOT_AUTHORIZED")
        run = self.action_client.action_run(attested.gitcode_run_id)
        expected = {
            "workflow_run_id": attested.gitcode_run_id,
            "workflow_id": attested.workflow_id,
            "file_path": attested.workflow_path,
            "attempt": attested.attempt,
            "head_sha": attested.run_head_sha,
            "head_branch": attested.run_head_branch,
            "event": attested.platform_event,
            "exist_in_default_branch": True,
        }
        if any(
            type(run.get(k)) is not type(v) or run[k] != v for k, v in expected.items()
        ):
            raise PlatformError("ATTESTED_ACTION_API_IDENTITY_MISMATCH")
        if run["status"] in {"CANCELED", "CANCELLED", "FAILED", "TIMED_OUT"}:
            raise TerminalActionRevoked("ATTESTED_ACTION_TERMINALLY_REVOKED")
        # Do not infer success from generic COMPLETED. Only reviewed platform
        # enums in the deployment policy may authorize a live/current proof.
        if run["status"] not in set(policy.allowed_run_statuses):
            raise PlatformError("ATTESTED_ACTION_NOT_ACTIVE_OR_SUCCESSFUL")
        if (
            attested.original_event_at * 1000
            > run["start_time"] + policy.clock_skew_seconds * 1000
        ):
            raise PlatformError("ORIGINAL_EVENT_AFTER_ACTION_START")
        return run

    def enroll_attested(self, intent, envelope, *, github_run):
        """Enroll only a verified original emitter envelope, never naked JSON.

        The issuer attests missing platform fields; v8 corroborates fields it
        actually exposes. Original event time—not signing/dispatch time—must
        postdate every PREVIOUSLY observed invalidation with a conservative
        clock-skew margin. A matching new current transition may then be
        observed and bound. Invisible ABA remains explicitly unprovable.
        """
        from ci_core.gitcode_attestation import verify_envelope

        policy = self.attestation_policy
        if policy is None or not policy.enabled:
            raise PlatformError("TRUSTED_EMITTER_NOT_ENABLED")
        ident = intent.get("bridge", {}).get("dispatch_intent_id")
        # A previously authenticated immutable envelope remains recoverable
        # beyond its FIRST-enrollment age budget. Fresh grants still go through
        # verify_event (deadline, key retirement, revocation and current API).
        with self.connection() as db:
            saved = db.execute(
                "SELECT source_intents.intent,source_intents.revoked,source_attestations.payload FROM source_intents JOIN source_attestations ON source_intents.id=source_attestations.intent_id WHERE source_intents.id=?",
                (ident,),
            ).fetchone()
        if saved is not None:
            if saved["intent"] != canonical(intent).decode() or canonical(
                json.loads(saved["payload"])["envelope"]
            ) != canonical(envelope):
                raise PlatformError("ATTESTED_INTENT_REPLAY_CHANGED")
            if saved["revoked"]:
                raise PlatformError("ATTESTED_INTENT_ALREADY_REVOKED")
            return
        if not self.policy.enabled:
            raise PlatformError("GITCODE_PROVIDER_DISABLED")
        if intent.get("bridge", {}).get("rerun_intent_id") != "initial":
            raise PlatformError("SIGNED_RERUN_OPERATOR_POLICY_NOT_ENABLED")
        now = int(self.clock())
        attested = verify_envelope(envelope, intent, policy, now=now)
        return self._enroll_capture(
            intent,
            envelope,
            github_run=github_run,
            policy=policy,
            attested=attested,
            trigger_kind="trusted_emitter",
        )

    def enroll_manual(self, intent, *, github_run):
        """Authorized GH operator requests a NEW manual current observation.

        GitCode log is an API-bound one-time wake, not original PR provenance.
        Exact authenticated GH owner and entire intent digest are retained.
        """
        from dataclasses import replace

        from ci_core.gitcode_manual_capture import verify_manual_capture

        policy = self.manual_capture_policy
        if not self.policy.enabled:
            raise PlatformError("GITCODE_PROVIDER_DISABLED")
        if policy is None or not policy.enabled:
            raise PlatformError("MANUAL_API_CAPTURE_NOT_ENABLED")
        if intent.get("bridge", {}).get("rerun_intent_id") != "initial":
            raise PlatformError("MANUAL_CAPTURE_NATIVE_RERUN_FORBIDDEN")
        actor = (github_run.get("actor") or {}).get("login")
        if (
            actor not in self.policy.allowed_actors
            or actor != intent["bridge"]["actor"]
            or github_run.get("display_title")
            != "lite-gitcode:" + intent["bridge"]["bridge_nonce"]
        ):
            raise PlatformError("MANUAL_GITHUB_OPERATOR_OR_NONCE_MISMATCH")
        delivery = {
            "run_id": positive(github_run.get("id")),
            "run_attempt": github_run.get("run_attempt"),
            "actor": actor,
            "repo_id": (github_run.get("repository") or {}).get("id"),
            "control_sha": github_run.get("head_sha"),
            "workflow_id": github_run.get("workflow_id"),
            "intent_digest": digest(intent),
        }
        ident = intent["bridge"]["dispatch_intent_id"]
        with self.connection() as db:
            saved = db.execute(
                "SELECT i.intent,i.revoked,a.payload FROM source_intents i JOIN source_attestations a ON i.id=a.intent_id WHERE i.id=?",
                (ident,),
            ).fetchone()
        if saved is not None:
            metadata = json.loads(saved["payload"])["metadata"]
            if (
                saved["revoked"]
                or saved["intent"] != canonical(intent).decode()
                or metadata.get("capture_kind") != "manual_api"
                or canonical(metadata.get("github_delivery")) != canonical(delivery)
            ):
                raise PlatformError("MANUAL_CAPTURE_REPLAY_OR_DELIVERY_CHANGED")
            return
        attested = verify_manual_capture(
            intent,
            policy,
            self.action_client,
            now=int(self.clock()),
            allow_terminal=True,
        )
        attested = replace(attested, github_delivery=delivery)
        return self._enroll_capture(
            intent,
            {"kind": "manual-api-wake", "intent_digest": digest(intent)},
            github_run=github_run,
            policy=policy,
            attested=attested,
            trigger_kind="manual_api",
        )

    def _enroll_capture(
        self, intent, envelope, *, github_run, policy, attested, trigger_kind
    ):
        ident = intent["bridge"]["dispatch_intent_id"]
        now = int(self.clock())
        repo, branch = MODEL_REPOSITORIES[self.policy.github_repo_id]
        if (
            github_run.get("event") != "workflow_dispatch"
            or github_run.get("run_attempt") != 1
            or github_run.get("head_sha") != self.policy.control_sha
            or github_run.get("head_branch") != branch
            or github_run.get("workflow_id") != self.policy.workflow_id
            or (github_run.get("repository") or {}).get("id")
            != self.policy.github_repo_id
            or (github_run.get("repository") or {}).get("full_name") != repo
            or (github_run.get("actor") or {}).get("login")
            not in self.policy.allowed_actors
        ):
            raise PlatformError("ATTESTATION_DELIVERY_RUN_NOT_TRUSTED")
        try:
            created = int(
                dt.datetime.fromisoformat(
                    github_run["created_at"].replace("Z", "+00:00")
                ).timestamp()
            )
        except (KeyError, ValueError, TypeError):
            raise PlatformError("ATTESTATION_DELIVERY_TIME_REQUIRED") from None
        if created > now + policy.clock_skew_seconds:
            raise PlatformError("ATTESTATION_DELIVERY_TIME_IN_FUTURE")
        if created + policy.clock_skew_seconds < attested.original_event_at:
            raise PlatformError("ATTESTATION_PRECEDES_ORIGINAL_EVENT")
        try:
            self._action_observation(attested, enrolling=True)
        except TerminalActionRevoked:
            # A verified terminal capture seen before first admission is still
            # a permanent tombstone. revision=0 means rejected/unobserved, never
            # an Authority revision or execution grant.
            with self.transaction() as db:
                collision = db.execute(
                    "SELECT intent_id FROM source_attestations WHERE json_extract(payload,'$.metadata.source_key')=?",
                    (attested.source_key,),
                ).fetchone()
                existing = db.execute(
                    "SELECT intent FROM source_intents WHERE id=?", (ident,)
                ).fetchone()
                if collision is None and existing is None:
                    db.execute(
                        "INSERT INTO source_intents(repo_id,id,semantic,number,revision,intent,revoked,trigger_kind,actor,authorized_rerun) VALUES(?,?,?,?,0,?,1,?,?,0)",
                        (
                            self.policy.gitcode_repo_id,
                            ident,
                            "rejected:" + attested.envelope_digest,
                            intent["pr"]["number"],
                            canonical(intent).decode(),
                            trigger_kind,
                            attested.actor,
                        ),
                    )
                    db.execute(
                        "INSERT INTO source_attestations VALUES(?,?,?,?,?,?,?)",
                        (
                            ident,
                            attested.envelope_digest,
                            attested.key_id,
                            attested.original_event_id,
                            attested.original_event_at,
                            attested.gitcode_run_id,
                            canonical(
                                {"envelope": envelope, "metadata": attested.__dict__}
                            ).decode(),
                        ),
                    )
            raise PlatformError("ATTESTED_CAPTURE_TERMINALLY_REVOKED") from None
        validate_pipeline(
            intent["pipeline"],
            self.policy.workflow_path,
            select_cases(intent["selectors"], self.catalog),
        )
        ident = intent["bridge"]["dispatch_intent_id"]
        number = intent["pr"]["number"]
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM source_intents WHERE id=?", (ident,)
            ).fetchone()
            if old is not None:
                proof = db.execute(
                    "SELECT * FROM source_attestations WHERE intent_id=?", (ident,)
                ).fetchone()
                if (
                    old["intent"] != canonical(intent).decode()
                    or proof is None
                    or proof["envelope_digest"] != attested.envelope_digest
                ):
                    raise PlatformError("ATTESTED_INTENT_REPLAY_CHANGED")
                if old["revoked"]:
                    raise PlatformError("ATTESTED_INTENT_ALREADY_REVOKED")
                return
            source = db.execute(
                "SELECT intent_id,envelope_digest FROM source_attestations WHERE json_extract(payload,'$.metadata.source_key')=?",
                (attested.source_key,),
            ).fetchone()
            if source is not None:
                raise PlatformError("TRUSTED_CAPTURE_SOURCE_EQUIVOCATION")
            replay = db.execute(
                "SELECT source_intents.intent FROM source_attestations JOIN source_intents ON source_intents.id=source_attestations.intent_id "
                "WHERE key_id=? AND event_id=?",
                (attested.key_id, attested.original_event_id),
            ).fetchall()

            def specification(value):
                return digest(
                    {
                        **{
                            key: value[key]
                            for key in (
                                "pr",
                                "control_repo_id",
                                "control_sha",
                                "workflow_path",
                                "pipeline",
                                "selectors",
                            )
                        },
                        "rerun_intent_id": value["bridge"]["rerun_intent_id"],
                    }
                )

            spec = specification(intent)
            if any(
                json.loads(row["intent"])["pipeline"] == intent["pipeline"]
                and specification(json.loads(row["intent"])) != spec
                for row in replay
            ):
                raise PlatformError("ORIGINAL_EVENT_SPECIFICATION_EQUIVOCATION")
            boundary = db.execute(
                "SELECT MAX(observed_at) FROM source_invalidations WHERE repo_id=? AND number=?",
                (self.policy.gitcode_repo_id, number),
            ).fetchone()[0]
            if (
                boundary is not None
                and attested.original_event_at <= boundary + policy.clock_skew_seconds
            ):
                raise PlatformError("ORIGINAL_EVENT_CROSSES_OBSERVED_INVALIDATION")
            authority = self._observe(db, number)
            if (
                not authority.authorized
                or authority.state != "open"
                or canonical(authority.pr) != canonical(intent["pr"])
            ):
                db.commit()
                raise PlatformError("ATTESTED_SNAPSHOT_NOT_CURRENT")
            if trigger_kind == "manual_api":
                # All run/job/log/API reads, including the serialized PR read,
                # have completed. Recheck with fresh time immediately before
                # any new registration; old retained intents use recovery path.
                fresh = int(self.clock())
                if (
                    fresh - attested.request_started_at > policy.max_capture_age_seconds
                    or fresh >= intent["deadline"]
                ):
                    db.commit()  # Keep any observed revocation, never the stale request.
                    raise PlatformError("MANUAL_CAPTURE_EXPIRED_DURING_VERIFICATION")
            active = db.execute(
                "SELECT intent FROM source_intents WHERE repo_id=? AND number=? AND revision=? AND revoked=0 AND json_extract(intent,'$.pipeline')=?",
                (
                    self.policy.gitcode_repo_id,
                    number,
                    authority.revision,
                    intent["pipeline"],
                ),
            ).fetchall()
            previous = db.execute(
                "SELECT intent FROM source_intents WHERE repo_id=? AND number=? AND revision=? AND json_extract(intent,'$.pipeline')=?",
                (
                    self.policy.gitcode_repo_id,
                    number,
                    authority.revision,
                    intent["pipeline"],
                ),
            ).fetchall()
            high_water = max(
                (
                    json.loads(row["intent"])["bridge"]["event_created_at"]
                    for row in previous
                ),
                default=0,
            )
            same_active_spec = bool(active) and all(
                specification(json.loads(row["intent"])) == spec for row in active
            )
            if attested.original_event_at <= high_water and not same_active_spec:
                raise PlatformError("ORIGINAL_EVENT_NOT_NEWER_THAN_PIPELINE_OWNER")
            # A second authentic capture of the SAME effective request is a
            # delivery duplicate, not a reason to cancel canonical work.
            if not active or any(
                specification(json.loads(row["intent"])) != spec for row in active
            ):
                self._supersede(db, number, intent["pipeline"])
            db.execute(
                "INSERT INTO source_intents(repo_id,id,semantic,number,revision,intent,trigger_kind,actor,authorized_rerun) VALUES(?,?,?,?,?,?,?,?,0)",
                (
                    self.policy.gitcode_repo_id,
                    ident,
                    "attested:" + attested.envelope_digest,
                    number,
                    authority.revision,
                    canonical(intent).decode(),
                    trigger_kind,
                    attested.actor,
                ),
            )
            db.execute(
                "INSERT INTO source_attestations VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    attested.envelope_digest,
                    attested.key_id,
                    attested.original_event_id,
                    attested.original_event_at,
                    attested.gitcode_run_id,
                    canonical(
                        {"envelope": envelope, "metadata": attested.__dict__}
                    ).decode(),
                ),
            )

    def _revoke_source_intent(self, db, ident):
        """Durable terminal origin revocation without canceling other duplicates."""
        db.execute("UPDATE source_intents SET revoked=1 WHERE id=?", (ident,))
        db.execute("UPDATE intents SET revoked=1 WHERE id=?", (ident,))
        for delivery in db.execute(
            "SELECT request_id,identity_json FROM deliveries WHERE intent_id=?",
            (ident,),
        ):
            self.coordinator._outbox(
                db,
                "CANCEL_RUN",
                delivery["request_id"],
                json.loads(delivery["identity_json"]),
            )
        # Only the original canonical intent owns this execution's cancellation.
        # Canceling a duplicate delivery cannot cancel the canonical execution.
        for execution in db.execute(
            "SELECT id FROM executions WHERE json_extract(plan_json,'$.bridge.dispatch_intent_id')=?",
            (ident,),
        ):
            db.execute(
                "UPDATE executions SET cancel_requested=1 WHERE id=?",
                (execution["id"],),
            )
            self.coordinator._outbox(
                db,
                "CANCEL_EXECUTION",
                execution["id"],
                {"execution_id": execution["id"]},
            )

    def verify_plan(self, plan):
        ident = text(plan.get("bridge", {}).get("dispatch_intent_id"), limit=160)
        with self.connection() as db:
            row = db.execute(
                "SELECT intent FROM source_intents WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise PlatformError("UNKNOWN_TRUSTED_PLAN_INTENT")
        intent = json.loads(row["intent"])
        if (
            canonical(plan.get("bridge")) != canonical(intent["bridge"])
            or canonical(plan.get("pr")) != canonical(intent["pr"])
            or plan.get("control", {}).get("github_repo_id")
            != intent["control_repo_id"]
            or plan.get("control", {}).get("control_sha") != intent["control_sha"]
            or plan.get("github", {}).get("workflow_path") != intent["workflow_path"]
            or plan.get("pipeline") != intent["pipeline"]
            or canonical(plan.get("cases"))
            != canonical(select_cases(intent["selectors"], self.catalog))
        ):
            raise PlatformError("PLAN_TRUSTED_INTENT_BINDING_MISMATCH")
        return self.verify_event(intent)

    def verify_event(self, intent):
        if intent.get("pr", {}).get("repo_id") != self.policy.gitcode_repo_id:
            raise PlatformError("TRUSTED_INTENT_REPOSITORY_MISMATCH")
        ident = text(intent.get("bridge", {}).get("dispatch_intent_id"), limit=160)
        # Do not read a claimed scope until the exact persisted intent is found.
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM source_intents WHERE id=?", (ident,)
            ).fetchone()
            if row is None or row["intent"] != canonical(intent).decode():
                raise PlatformError("UNKNOWN_OR_CHANGED_TRUSTED_INTENT")
            if row["revoked"]:
                raise PlatformError("TRUSTED_INTENT_REVOKED")
            if row["trigger_kind"] in {"trusted_emitter", "manual_api"}:
                from types import SimpleNamespace

                evidence = db.execute(
                    "SELECT payload FROM source_attestations WHERE intent_id=?",
                    (ident,),
                ).fetchone()
                if evidence is None:
                    raise PlatformError("ATTESTATION_LEDGER_EVIDENCE_MISSING")
                metadata = json.loads(evidence["payload"])["metadata"]
                if not metadata.get("source_key") or not metadata.get("capture_job_id"):
                    raise PlatformError("ATTESTATION_CAPTURE_IDENTITY_MISSING")
                try:
                    self._action_observation(
                        SimpleNamespace(**metadata), enrolling=False
                    )
                except TerminalActionRevoked:
                    self._revoke_source_intent(db, ident)
                    db.commit()
                    raise PlatformError("TRUSTED_INTENT_REVOKED_BY_ACTION") from None
            authority = self._observe(db, row["number"])
            revoked = db.execute(
                "SELECT revoked FROM source_intents WHERE id=?", (ident,)
            ).fetchone()[0]
            if (
                revoked
                or row["revision"] != authority.revision
                or authority.state != "open"
                or not authority.authorized
            ):
                db.commit()  # Never roll back an observed revocation on rejection.
                raise PlatformError("TRUSTED_INTENT_REVOKED")
            if int(self.clock()) >= intent["deadline"]:
                self._revoke_source_intent(db, ident)
                db.commit()
                raise PlatformError("TRUSTED_INTENT_EXPIRED")
            bridge = intent["bridge"]
            return EventProof(
                bridge["bridge_event_id"],
                bridge["source_snapshot_id"],
                row["sequence"],
                row["revision"],
                bool(row["authorized_rerun"]),
            )
