"""Read schema-4 inputs from exact GitHub Artifact/Check IDs, never PR Python.

This adapter creates *no* resource lease. The coordinator still has to establish
an authenticated event proof, claim the execution, and fence every launch. The
GitHub API has no artifact run_attempt field: attempt 1 is proved by the actual
Run plus immutable attempt-qualified names; native reruns are rejected.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from ci_core.gitcode_contract import (
    BRIDGE_KEYS,
    MODEL_REPOSITORIES,
    PLAN_KEYS,
    ContractError,
    ControlPolicy,
    VerifiedRequest,
    canonical,
    digest,
    hex_digest,
    load_json,
    positive,
    select_cases,
    shape,
    text,
    validate_pr,
    verify_request,
)
from ci_core.gitcode_source import SourceHandle, extract_verified_source

JSON_LIMIT = 256 * 1024
BUNDLE_LIMIT = 512 * 1024**2
MEMBERS = {
    "request": "ci-request.json",
    "intent": "dispatch-intent.json",
    "manifest": "source-manifest.json",
    "source": "source.tar.gz",
    "runner": "runner.tar.gz",
}
PREFIXES = {
    "intent": "dispatch-intent",
    "manifest": "source-manifest",
    "source": "gitcode-merge-source",
    "runner": "trusted-runner",
}


class ArtifactError(ValueError):
    pass


def unzip_one(payload: bytes, member: str, *, limit: int) -> bytes:
    """Only one ordinary file; bound decompression before allocating its body."""
    if not isinstance(payload, bytes) or len(payload) > BUNDLE_LIMIT:
        raise ArtifactError("ARTIFACT_DOWNLOAD_SIZE_LIMIT")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != member:
                raise ArtifactError("UNEXPECTED_ARTIFACT_MEMBERS")
            info = entries[0]
            mode = info.external_attr >> 16
            if (
                info.is_dir()
                or info.flag_bits & 1
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or not 0 <= info.file_size <= limit
            ):
                raise ArtifactError("UNSAFE_OR_OVERSIZED_ARTIFACT_MEMBER")
            with archive.open(info) as stream:
                content = stream.read(limit + 1)
            if len(content) != info.file_size or len(content) > limit:
                raise ArtifactError("ARTIFACT_MEMBER_SIZE_MISMATCH")
            return content
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise ArtifactError("INVALID_ARTIFACT_ZIP") from exc


@dataclass(frozen=True)
class LoadedRequest:
    verified: VerifiedRequest
    source: SourceHandle
    runner_path: Path
    source_directory: Path
    runner_directory: Path
    intent: dict
    run: dict

    def verify_files(self) -> None:
        self.source.verify()
        expected = self.verified.plan["control"]["runner_sha256"]
        from ci_core.gitcode_source import sha256_file

        if self.runner_path.is_symlink() or sha256_file(self.runner_path) != expected:
            raise ArtifactError("TRUSTED_RUNNER_DIGEST_MISMATCH")


class GitCodeArtifactReader:
    """Trusted clients and policy are supplied by deployment, not by a Request."""

    def __init__(self, *, policy: ControlPolicy, github, personal, channel, catalog):
        self.policy, self.github, self.personal = policy, github, personal
        self.channel, self.catalog = channel, catalog
        repo = MODEL_REPOSITORIES[policy.github_repo_id][0]
        if (
            github.repo != repo
            or github.repo_id != policy.github_repo_id
            or personal.repo_id != policy.gitcode_repo_id
            or channel.repo_id != policy.github_repo_id
            or channel.app_id != policy.app_id
        ):
            raise ArtifactError("ARTIFACT_CLIENT_IDENTITY_MISMATCH")

    def actual_run(self, run_id: int) -> dict:
        if not self.policy.enabled:
            raise ArtifactError("GITCODE_PROVIDER_DISABLED")
        positive(run_id)
        run = self.github.request("GET", f"/actions/runs/{run_id}")
        policy = self.policy
        repo, ref = MODEL_REPOSITORIES[policy.github_repo_id]
        expected = {
            "id": run_id,
            "run_attempt": 1,
            "head_sha": policy.control_sha,
            "head_branch": ref,
            "workflow_id": policy.workflow_id,
            "event": "workflow_dispatch",
        }
        if not isinstance(run, dict) or any(
            type(run.get(k)) is not type(v) or run.get(k) != v
            for k, v in expected.items()
        ):
            raise ArtifactError("ACTUAL_RUN_IDENTITY_OR_ATTEMPT_MISMATCH")
        repository = run.get("repository") or {}
        if (
            type(repository.get("id")) is not int
            or repository.get("id") != policy.github_repo_id
            or repository.get("full_name") != repo
            or run.get("path") not in (policy.workflow_path, policy.workflow_path + "@" + ref)
            or (run.get("actor") or {}).get("login") not in policy.allowed_actors
            or run.get("status") not in ("queued", "in_progress")
        ):
            raise ArtifactError("ACTUAL_RUN_NOT_AUTHORIZED_OR_ACTIVE")
        return {
            **run,
            "path": policy.workflow_path,
            "repository_id": repository["id"],
            "_ci_repo": repo,
            "_ci_control_sha": run["head_sha"],
        }

    def _metadata(self, artifact_id, name, run, *, limit):
        positive(artifact_id)
        item = self.github.request("GET", f"/actions/artifacts/{artifact_id}")
        if not isinstance(item, dict):
            raise ArtifactError("INVALID_ARTIFACT_METADATA")
        owner = item.get("workflow_run") or {}
        if (
            type(item.get("id")) is not int
            or item["id"] != artifact_id
            or item.get("name") != name
            or item.get("expired") is not False
            or item.get("url") != f"{self.github.api}/actions/artifacts/{artifact_id}"
            or type(item.get("size_in_bytes")) is not int
            or not 0 < item["size_in_bytes"] <= limit
            or type(owner.get("id")) is not int
            or owner.get("id") != run["id"]
            or type(owner.get("repository_id")) is not int
            or owner.get("repository_id") != self.policy.github_repo_id
            or owner.get("head_sha") != self.policy.control_sha
            or owner.get("head_branch") != run["head_branch"]
        ):
            raise ArtifactError("ARTIFACT_OWNER_NAME_SIZE_OR_EXPIRY_MISMATCH")
        value = item.get("digest")
        if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ArtifactError("ARTIFACT_API_DIGEST_REQUIRED")
        return item

    def _named(self, name, run):
        query = urlencode({"name": name, "per_page": 10, "page": 1})
        listing = self.github.request("GET", f"/actions/runs/{run['id']}/artifacts?{query}")
        if (
            not isinstance(listing, dict)
            or type(listing.get("total_count")) is not int
            or not isinstance(listing.get("artifacts"), list)
            or listing["total_count"] != len(listing["artifacts"])
            or listing["total_count"] > 1
        ):
            raise ArtifactError("AMBIGUOUS_OR_INCOMPLETE_ARTIFACT_LISTING")
        if not listing["artifacts"]:
            return None
        item = listing["artifacts"][0]
        if not isinstance(item, dict) or item.get("name") != name:
            raise ArtifactError("UNEXPECTED_NAMED_ARTIFACT")
        return self._metadata(item.get("id"), name, run, limit=JSON_LIMIT)

    def _read(self, metadata, role):
        payload = self.github.download_artifact(metadata["id"], limit=metadata["size_in_bytes"])
        if (
            len(payload) != metadata["size_in_bytes"]
            or hashlib.sha256(payload).hexdigest() != metadata["digest"][7:]
        ):
            raise ArtifactError("ARTIFACT_ZIP_DIGEST_OR_SIZE_MISMATCH")
        limit = BUNDLE_LIMIT if role in ("source", "runner") else JSON_LIMIT
        content = unzip_one(payload, MEMBERS[role], limit=limit)
        if role in ("request", "intent", "manifest"):
            document = load_json(content)
            return document, digest(document)
        return content, hashlib.sha256(content).hexdigest()

    def _snapshot(self, number):
        return self.personal.open_pull_snapshot(
            number,
            allowed_base_refs=self.policy.allowed_base_refs,
            allowed_actors=self.policy.allowed_actors,
        )

    def _validate_intent(self, intent, run, now):
        shape(intent, {
            "schema", "source_provider", "pr", "control_repo_id", "control_sha",
            "workflow_path", "pipeline", "selectors", "bridge", "deadline",
        })
        if (
            type(intent["schema"]) is not int or intent["schema"] != 1
            or intent["source_provider"] != "gitcode"
            or intent["control_repo_id"] != self.policy.github_repo_id
            or intent["control_sha"] != self.policy.control_sha
            or intent["workflow_path"] != self.policy.workflow_path
            or intent["pipeline"] not in ("smoke", "nightly")
            or (self.policy.github_repo_id == 1438625957 and intent["pipeline"] != "smoke")
        ):
            raise ArtifactError("EARLY_INTENT_CONTROL_MISMATCH")
        validate_pr(intent["pr"], allowed_repo_id=self.policy.gitcode_repo_id,
                    allowed_refs=self.policy.allowed_base_refs)
        select_cases(intent["selectors"], self.catalog)
        shape(intent["bridge"], BRIDGE_KEYS)
        bridge = intent["bridge"]
        for key, value in bridge.items():
            if key in ("bridge_attempt", "event_created_at"):
                positive(value)
            else:
                text(value, limit=160)
        if (
            bridge["actor"] not in self.policy.allowed_actors
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", bridge["bridge_nonce"])
            or run.get("display_title") != "lite-gitcode:" + bridge["bridge_nonce"]
        ):
            raise ArtifactError("EARLY_INTENT_ACTOR_OR_NONCE_MISMATCH")
        positive(intent["deadline"])
        if not bridge["event_created_at"] <= now < intent["deadline"] or (
            intent["deadline"] - bridge["event_created_at"] > self.policy.max_duration_seconds
        ):
            raise ArtifactError("EARLY_INTENT_DEADLINE_INVALID")
        current = self._snapshot(intent["pr"]["number"])
        if canonical(current) != canonical({"identity": intent["pr"], "state": "open", "authorized": True}):
            raise ArtifactError("EARLY_INTENT_VERSION_STALE")
        canonical(intent, limit=16 * 1024)

    def early_intent(self, run_id: int, *, now: int):
        """Discover preparing Runs without granting any execution permission."""
        run = self.actual_run(run_id)
        item = self._named(f"dispatch-intent-{run_id}-1", run)
        if item is None:
            return None
        intent, _ = self._read(item, "intent")
        self._validate_intent(intent, run, now)
        return intent, run

    def _prepare(self, plan, run):
        job_id = positive(plan["github"].get("prepare_job_id"))
        job = self.github.request("GET", f"/actions/jobs/{job_id}")
        expected = {
            "id": job_id, "run_id": run["id"], "run_attempt": 1,
            "head_sha": self.policy.control_sha, "name": self.policy.prepare_job_name,
        }
        if not isinstance(job, dict) or any(
            type(job.get(k)) is not type(v) or job.get(k) != v
            for k, v in expected.items()
        ):
            raise ArtifactError("PREPARE_JOB_IDENTITY_MISMATCH")
        if job.get("status") in ("queued", "in_progress", "waiting", "pending"):
            return None
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise ArtifactError("PREPARE_NOT_SUCCESSFUL")
        return job

    def load(self, run_id: int, workspace: Path, *, now: int) -> LoadedRequest | None:
        """Re-read live identities; materialize only a complete successful Prepare."""
        run = self.actual_run(run_id)
        request = self._named("lite-ci-request-1", run)
        if request is None:
            return None
        plan, _ = self._read(request, "request")
        shape(plan, PLAN_KEYS, "INVALID_SCHEMA4_PLAN")
        if type(plan["schema"]) is not int or plan["schema"] != 4:
            raise ArtifactError("SCHEMA4_REQUEST_REQUIRED")
        if plan["plan_digest"] != digest({k: v for k, v in plan.items() if k != "plan_digest"}):
            raise ArtifactError("PLAN_DIGEST_MISMATCH")
        if (
            plan["github"].get("repo_id") != self.policy.github_repo_id
            or plan["github"].get("run_id") != run_id
            or plan["github"].get("run_attempt") != 1
            or plan["control"].get("control_sha") != self.policy.control_sha
        ):
            raise ArtifactError("REQUEST_CONTROL_IDENTITY_MISMATCH")
        if self._prepare(plan, run) is None:
            return None
        shape(plan["artifacts"], set(PREFIXES))
        observations, contents = {}, {}
        for role, ref in plan["artifacts"].items():
            shape(ref, {"id", "name", "size", "zip_sha256", "content_sha256"})
            name = f"{PREFIXES[role]}-{run_id}-1"
            if ref["name"] != name:
                raise ArtifactError("ARTIFACT_ATTEMPT_NAME_MISMATCH")
            limit = BUNDLE_LIMIT if role in ("source", "runner") else JSON_LIMIT
            item = self._metadata(ref["id"], name, run, limit=limit)
            hex_digest(ref["zip_sha256"])
            hex_digest(ref["content_sha256"])
            if item["size_in_bytes"] != ref["size"] or item["digest"][7:] != ref["zip_sha256"]:
                raise ArtifactError("ARTIFACT_PLAN_METADATA_MISMATCH")
            content, content_digest = self._read(item, role)
            if content_digest != ref["content_sha256"]:
                raise ArtifactError("ARTIFACT_CONTENT_DIGEST_MISMATCH")
            contents[role] = content
            observations[role] = {
                "id": item["id"], "name": item["name"], "size": item["size_in_bytes"],
                "zip_sha256": item["digest"][7:], "content_sha256": content_digest,
                "repo_id": item["workflow_run"]["repository_id"],
                "run_id": item["workflow_run"]["id"], "run_attempt": 1, "expired": False,
            }
        self._validate_intent(contents["intent"], run, now)
        slots = {}
        for case_id in plan["result_slots"]:
            check = self.channel.read_slot(plan, case_id)
            slots[case_id] = self.channel._observe(plan, case_id, check)
        # A cancelled/failed/rerun Run cannot cross the download window.
        run = self.actual_run(run_id)
        job = self._prepare(plan, run)
        if job is None:
            raise ArtifactError("PREPARE_CHANGED_DURING_DOWNLOAD")
        verified = verify_request(
            plan, policy=self.policy, catalog=self.catalog, run=run,
            prepare_job=job, artifacts=observations, slots=slots,
            manifest=contents["manifest"], intent=contents["intent"],
            current_pr=self._snapshot(plan["pr"]["number"]), now=now,
        )
        workspace = Path(workspace)
        if workspace.exists() or workspace.is_symlink():
            raise ArtifactError("ARTIFACT_WORKSPACE_MUST_BE_NEW")
        workspace.mkdir(mode=0o700, parents=True)
        try:
            source_path, runner_path = workspace / "source.tar.gz", workspace / "runner.tar.gz"
            source_path.write_bytes(contents["source"])
            runner_path.write_bytes(contents["runner"])
            source = SourceHandle(
                provider="gitcode", head_sha=plan["pr"]["head_sha"],
                base_sha=plan["pr"]["base_sha"],
                tested_merge_sha=plan["merge"]["tested_merge_sha"],
                tested_tree_sha=plan["merge"]["tested_tree_sha"],
                bundle_sha256=plan["artifacts"]["source"]["content_sha256"],
                bundle_path=str(source_path), merge_policy=plan["merge"]["policy"],
                git_version=plan["merge"]["builder_version"],
                dependencies=tuple(contents["manifest"]["dependencies"]),
            )
            source_directory = extract_verified_source(source, workspace / "source")
            from ci_core.gitcode_source import extract_verified_archive

            runner_directory = extract_verified_archive(
                runner_path, plan["control"]["runner_sha256"], workspace / "runner"
            )
            (workspace / "plan.json").write_bytes(canonical(plan))
            (workspace / "source-manifest.json").write_bytes(canonical(contents["manifest"]))
            return LoadedRequest(verified, source, runner_path, source_directory,
                                 runner_directory, contents["intent"], run)
        except BaseException:
            shutil.rmtree(workspace)
            raise
