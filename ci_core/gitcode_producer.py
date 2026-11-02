"""Trusted schema-4 Prepare orchestration, with Request published last.

The artifact uploader is a hosting-platform adapter. It must perform at most one
upload per call and return the numeric Artifact ID. Journals must be restored
across hosting-job recovery. No Source Bundle Python is imported or executed.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from ci_core.gitcode_artifacts import BUNDLE_LIMIT, JSON_LIMIT, GitCodeArtifactReader
from ci_core.gitcode_bridge import GitCodeBridge
from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    TORCHTITAN_REPOSITORY_IDS,
    canonical,
    digest,
    positive,
    seal_plan,
    select_cases,
)
from ci_core.gitcode_prepare import manifest_for_source, prepare_pr_source
from ci_core.gitcode_source import sha256_file


class ProducerError(ValueError):
    pass


class GitCodeProducer:
    def __init__(
        self,
        *,
        policy,
        catalog,
        github,
        personal,
        upstream,
        channel,
        artifact_journal,
        upload,
        clock,
        source_builder=prepare_pr_source,
        authority_protocol=None,
    ):
        if authority_protocol not in (None, "artifact-check-v1"):
            raise ProducerError("UNKNOWN_AUTHORITY_PROTOCOL")
        self.authority_protocol = authority_protocol
        self.policy, self.catalog = policy, catalog
        self.github, self.personal, self.upstream = github, personal, upstream
        self.channel, self.journal = channel, artifact_journal
        self.upload, self.clock, self.source_builder = upload, clock, source_builder
        self.reader = GitCodeArtifactReader(
            policy=policy,
            github=github,
            personal=personal,
            channel=channel,
            catalog=catalog,
        )
        self.bridge = GitCodeBridge(
            policy=policy,
            github=github,
            personal=personal,
            upstream=upstream,
            journal=None,
            catalog=catalog,
            github_actor=policy.allowed_actors[0],
        )

    def _recover_upload(self, run, name):
        query = urlencode({"name": name, "per_page": 2, "page": 1})
        result = self.github.request(
            "GET", f"/actions/runs/{run['id']}/artifacts?{query}"
        )
        rows = result.get("artifacts") if isinstance(result, dict) else None
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or type(result.get("total_count")) is not int
            or result["total_count"] != 1
            or rows[0].get("name") != name
        ):
            raise ProducerError("ARTIFACT_PUBLICATION_UNKNOWN_OR_AMBIGUOUS")
        return positive(rows[0].get("id"))

    def _publish(self, role, path: Path, run, *, name: str) -> dict:
        if path.is_symlink() or not path.is_file():
            raise ProducerError("ARTIFACT_ORDINARY_FILE_REQUIRED")
        limit = BUNDLE_LIMIT if role in {"runner", "source"} else JSON_LIMIT
        if not 0 < path.stat().st_size <= limit:
            raise ProducerError("ARTIFACT_CONTENT_SIZE_LIMIT")
        expected = sha256_file(path)
        identity = digest(
            {
                "repo_id": self.policy.github_repo_id,
                "run_id": run["id"],
                "attempt": 1,
                "name": name,
            }
        )
        owns_upload, remembered = self.journal.begin(identity)
        if remembered is not None:
            if remembered["content_sha256"] != expected:
                raise ProducerError("ARTIFACT_CONTENT_IMMUTABLE")
            ident = remembered["id"]
        elif owns_upload:
            try:
                ident = positive(self.upload(name, path))
            except (OSError, TimeoutError):
                ident = self._recover_upload(run, name)
        else:
            ident = self._recover_upload(run, name)
        meta = self.reader._metadata(ident, name, run, limit=limit)
        _, observed_digest = self.reader._read(meta, role)
        if observed_digest != expected:
            raise ProducerError("UPLOADED_CONTENT_DIGEST_MISMATCH")
        ref = {
            "id": ident,
            "name": name,
            "size": meta["size_in_bytes"],
            "zip_sha256": meta["digest"][7:],
            "content_sha256": expected,
        }
        self.journal.bind(identity, ref)
        return ref

    def _job(self, run):
        listing = self.github.request(
            "GET", f"/actions/runs/{run['id']}/attempts/1/jobs?per_page=100"
        )
        rows = listing.get("jobs") if isinstance(listing, dict) else None
        if (
            not isinstance(rows, list)
            or listing.get("total_count") != len(rows)
            or len(rows) > 100
        ):
            raise ProducerError("PREPARE_JOB_VISIBILITY_INCOMPLETE")
        matches = [x for x in rows if x.get("name") == self.policy.prepare_job_name]
        if len(matches) != 1:
            raise ProducerError("PREPARE_JOB_NOT_UNIQUE")
        job = matches[0]
        if (
            job.get("run_id") != run["id"]
            or job.get("run_attempt") != 1
            or job.get("head_sha") != self.policy.control_sha
            or job.get("status") != "in_progress"
            or job.get("conclusion") is not None
        ):
            raise ProducerError("PREPARE_JOB_NOT_CURRENTLY_RUNNING")
        positive(job.get("id"))
        return job

    def prepare(
        self, *, run_id: int, intent: dict, runner_path: Path, workspace: Path
    ) -> dict:
        """Return the sealed Plan, not a hardware grant or a successful Prepare.

        The external Worker independently requires this Job to have completed
        successfully, even after the final request Artifact becomes visible.
        """
        run = self.reader.actual_run(run_id)
        now = int(self.clock())
        self.bridge.validate_intent(intent, now=now)
        self.reader._validate_intent(intent, run, now)
        job = self._job(run)
        runner_path = Path(runner_path)
        if (
            runner_path.is_symlink()
            or not runner_path.is_file()
            or not 0 < runner_path.stat().st_size <= BUNDLE_LIMIT
            or sha256_file(runner_path) != self.policy.runner_sha256
        ):
            raise ProducerError("UNTRUSTED_RUNNER_BUNDLE")
        workspace = Path(workspace)
        if workspace.exists() or workspace.is_symlink():
            raise ProducerError("PREPARE_WORKSPACE_MUST_BE_NEW")
        workspace.mkdir(parents=True, mode=0o700)
        intent_path = workspace / "dispatch-intent.json"
        intent_path.write_bytes(canonical(intent))
        prefix = f"{run_id}-1"
        refs = {
            "intent": self._publish(
                "intent", intent_path, run, name=f"dispatch-intent-{prefix}"
            )
        }
        handle, pr = self.source_builder(
            self.personal,
            intent["pr"]["number"],
            workspace / "merge-build",
            allowed_base_refs=self.policy.allowed_base_refs,
            allowed_actors=self.policy.allowed_actors,
            expected_git_version=self.policy.git_builder_version.removeprefix("git-"),
            expected_identity=intent["pr"],
        )
        if canonical(pr) != canonical(intent["pr"]):
            raise ProducerError("PREPARED_PR_IDENTITY_MISMATCH")
        handle.verify()
        if "git-" + handle.git_version != self.policy.git_builder_version:
            raise ProducerError("MERGE_BUILDER_VERSION_MISMATCH")
        if self.policy.github_repo_id in TORCHTITAN_REPOSITORY_IDS:
            from ci_core.gitcode_interfaces import inspect_bundle

            inspect_bundle(
                handle,
                [row["test_id"] for row in intent["selectors"]],
                workspace / "source-inspection",
            )
        # Never trust an upload wrapper's checksum alone: read back each exact
        # Artifact and independently verify its outer ZIP and inner content.
        refs["source"] = self._publish(
            "source",
            Path(handle.bundle_path),
            run,
            name=f"gitcode-merge-source-{prefix}",
        )
        refs["runner"] = self._publish(
            "runner", runner_path, run, name=f"trusted-runner-{prefix}"
        )
        repo, ref = MODEL_REPOSITORIES[self.policy.github_repo_id]
        control = {
            "upstream_repo_id": self.policy.upstream_repo_id,
            "upstream_default_ref": self.policy.upstream_default_ref,
            "upstream_control_sha": self.policy.control_sha,
            "github_repo_id": self.policy.github_repo_id,
            "default_ref": ref,
            "control_sha": self.policy.control_sha,
            "runner_sha256": self.policy.runner_sha256,
        }
        merge = {
            "policy": handle.merge_policy,
            "builder_version": "git-" + handle.git_version,
            "tested_merge_sha": handle.tested_merge_sha,
            "tested_tree_sha": handle.tested_tree_sha,
        }
        manifest = manifest_for_source(
            handle,
            pr=pr,
            merge=merge,
            control=control,
            github_run_id=run_id,
            run_attempt=1,
            source_artifact_id=refs["source"]["id"],
        )
        manifest_path = workspace / "source-manifest.json"
        manifest_path.write_bytes(canonical(manifest))
        refs["manifest"] = self._publish(
            "manifest", manifest_path, run, name=f"source-manifest-{prefix}"
        )
        plan = seal_plan(
            {
                "schema": 4,
                "source_provider": "gitcode",
                "test_mode": "merge_result",
                "pr": pr,
                "merge": merge,
                "control": control,
                "github": {
                    "repo": repo,
                    "repo_id": self.policy.github_repo_id,
                    "workflow_id": self.policy.workflow_id,
                    "workflow_path": self.policy.workflow_path,
                    "run_id": run_id,
                    "run_attempt": 1,
                    "prepare_job_id": job["id"],
                },
                "bridge": intent["bridge"],
                "pipeline": intent["pipeline"],
                "policy_version": self.policy.policy_version,
                "environment_version": self.policy.environment_version,
                "deadline": intent["deadline"],
                "cases": select_cases(intent["selectors"], self.catalog),
                "artifacts": refs,
                "result_slots": {},
                **(
                    {"authority_protocol": self.authority_protocol}
                    if self.authority_protocol
                    else {}
                ),
            }
        )
        plan["result_slots"] = self.channel.precreate(plan)
        plan = seal_plan(plan)
        # A new Head/Base/control or cancellation during upload invalidates the
        # entire request. Orphaned data/slots cannot create a resource lease.
        self.bridge.validate_intent(intent, now=int(self.clock()))
        self.reader._validate_intent(
            intent, self.reader.actual_run(run_id), int(self.clock())
        )
        if self._job(run)["id"] != job["id"]:
            raise ProducerError("PREPARE_JOB_CHANGED")
        request_path = workspace / "ci-request.json"
        request_path.write_bytes(canonical(plan))
        self._publish("request", request_path, run, name="lite-ci-request-1")
        return plan
