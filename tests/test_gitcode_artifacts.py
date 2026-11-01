"""Real ZIP/tar consumption with mocked platform APIs, not platform E2E."""

import copy
import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

from ci_core.gitcode_artifacts import ArtifactError, GitCodeArtifactReader, unzip_one
from ci_core.gitcode_checks import CheckRunChannel
from ci_core.gitcode_contract import canonical, digest, external_id, seal_plan
from ci_core.gitcode_source import SourceError
from tests.test_gitcode_contract import fixture


def tar_bytes(files=None, *, link=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in (files or {"payload.py": b"raise RuntimeError('PR must never run on controller')\n"}).items():
            info = tarfile.TarInfo(name)
            if link:
                info.type, info.linkname = tarfile.SYMTYPE, "/tmp/outside"
                archive.addfile(info)
            else:
                info.size, info.mode = len(content), 0o644
                archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def zip_bytes(name, content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return stream.getvalue()


class ArtifactAPI:
    """API responses have actual REST shapes; metadata is not copied from Plan."""

    def __init__(self, *, source=None, runner=None, probe_only=False):
        self.plan, context = fixture()
        plan = self.plan
        plan["bridge"]["bridge_nonce"] = "trusted-nonce-00000000001"
        if probe_only:
            plan["cases"] = plan["cases"][:1]
        source = tar_bytes() if source is None else source
        runner = tar_bytes({"entry.py": b"# trusted controller runner\n"}) if runner is None else runner
        plan["control"]["runner_sha256"] = hashlib.sha256(runner).hexdigest()
        self.policy = replace(context["policy"], runner_sha256=plan["control"]["runner_sha256"])
        self.repo, self.repo_id = plan["github"]["repo"], plan["github"]["repo_id"]
        self.api = "https://api.github.com/repos/" + self.repo
        self.calls, self.downloads = [], []
        self.run = {
            **context["run"], "repository": {"id": self.repo_id, "full_name": self.repo},
            "actor": {"login": "depeng1994"}, "status": "in_progress",
            "display_title": "lite-gitcode:" + plan["bridge"]["bridge_nonce"],
            "created_at": "2026-11-01T00:00:00Z", "name": "Dispatcher-Smoke", "run_number": 1,
        }
        self.job = {**context["prepare_job"], "head_sha": self.policy.control_sha}
        self.intent = {
            **context["intent"], "bridge": plan["bridge"],
            "selectors": [{"test_id": item["test_id"]} for item in plan["cases"]],
        }
        self.manifest = {
            **context["manifest"], "control": plan["control"],
            "bundle_sha256": hashlib.sha256(source).hexdigest(),
        }
        self.metadata, self.zips = {}, {}
        members = {
            "intent": ("dispatch-intent.json", canonical(self.intent)),
            "source": ("source.tar.gz", source),
            "runner": ("runner.tar.gz", runner),
            "manifest": ("source-manifest.json", canonical(self.manifest)),
        }
        for role, (name, content) in members.items():
            ref = plan["artifacts"][role]
            metadata = self.add_artifact(ref["id"], ref["name"], name, content)
            ref.update(size=metadata["size_in_bytes"], zip_sha256=metadata["digest"][7:],
                       content_sha256=hashlib.sha256(content).hexdigest())
        plan = seal_plan(plan)
        plan["result_slots"] = {
            case_id: {**slot, "external_id": external_id(plan, case_id)}
            for case_id, slot in plan["result_slots"].items()
            if case_id == "aggregate" or case_id in {c["case_id"] for c in plan["cases"]}
        }
        self.plan = seal_plan(plan)
        self.add_artifact(999, "lite-ci-request-1", "ci-request.json", canonical(self.plan))
        self.checks = {}
        for case_id, slot in self.plan["result_slots"].items():
            self.checks[slot["id"]] = {
                "id": slot["id"], "name": CheckRunChannel.name(self.plan, case_id),
                "head_sha": self.policy.control_sha, "app": {"id": self.policy.app_id},
                "external_id": slot["external_id"], "url": self.api + f"/check-runs/{slot['id']}",
                "status": "queued", "conclusion": None,
                "output": {"summary": canonical({
                    "schema": 4, "phase": "SLOT_CREATED", "request_id": f"{self.repo_id}/100/1",
                    "case_id": case_id, "request_spec_digest": self.plan["request_spec_digest"],
                }).decode()},
            }
        self.personal = Mock(repo_id="42")
        self.personal.open_pull_snapshot.return_value = {
            "identity": self.plan["pr"], "state": "open", "authorized": True,
        }
        self.channel = CheckRunChannel(self, repo_id=self.repo_id, app_id=self.policy.app_id)
        self.reader = GitCodeArtifactReader(policy=self.policy, github=self,
                                           personal=self.personal, channel=self.channel,
                                           catalog=context["catalog"])

    def add_artifact(self, artifact_id, name, member, content):
        payload = zip_bytes(member, content)
        self.zips[artifact_id] = payload
        metadata = {
            "id": artifact_id, "name": name, "size_in_bytes": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "expired": False,
            "url": self.api + f"/actions/artifacts/{artifact_id}",
            "workflow_run": {"id": 100, "repository_id": self.repo_id,
                             "head_sha": self.policy.control_sha, "head_branch": "main"},
        }
        self.metadata[artifact_id] = metadata
        return metadata

    def request(self, method, path, data=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/actions/runs/100":
            return copy.deepcopy(self.run)
        if method == "GET" and path == "/actions/jobs/101":
            return copy.deepcopy(self.job)
        if method == "GET" and path.startswith("/actions/runs/100/artifacts?"):
            name = parse_qs(urlsplit(path).query)["name"][0]
            items = [m for m in self.metadata.values() if m["name"] == name]
            return {"total_count": len(items), "artifacts": copy.deepcopy(items)}
        if method == "GET" and path.startswith("/actions/artifacts/"):
            return copy.deepcopy(self.metadata[int(path.rsplit("/", 1)[-1])])
        if path.startswith("/check-runs/"):
            check = self.checks[int(path.rsplit("/", 1)[-1])]
            if method == "PATCH":
                check.update(copy.deepcopy(data))
            return copy.deepcopy(check)
        raise AssertionError((method, path))

    def download_artifact(self, artifact_id, *, limit):
        self.downloads.append(artifact_id)
        return self.zips[artifact_id]

    def rewrite_plan(self, change):
        change(self.plan)
        self.plan = seal_plan(self.plan)
        self.add_artifact(999, "lite-ci-request-1", "ci-request.json", canonical(self.plan))


class ArtifactReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.destination = Path(self.tmp.name) / "new-inputs"
        self.api = ArtifactAPI()

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        return self.api.reader.load(100, self.destination, now=1100)

    def test_complete_inputs_pin_merge_source_and_do_not_import_source(self):
        loaded = self.load()
        self.assertEqual(loaded.source.tested_merge_sha, self.api.plan["merge"]["tested_merge_sha"])
        self.assertNotEqual(loaded.source.tested_merge_sha, self.api.plan["pr"]["head_sha"])
        self.assertEqual(loaded.run["head_sha"], self.api.policy.control_sha)
        self.assertIn("must never run", (loaded.source_directory / "payload.py").read_text())
        loaded.verify_files()
        self.assertEqual(set(self.api.downloads), {201, 202, 203, 204, 999})
        self.assertTrue(all("/commits/" not in path for _, path in self.api.calls))

    def test_missing_request_waits_without_workspace(self):
        del self.api.metadata[999]
        self.assertIsNone(self.load())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.api.downloads, [])

    def test_pending_prepare_does_not_download_payloads(self):
        self.api.job.update(status="in_progress", conclusion=None)
        self.assertIsNone(self.load())
        self.assertEqual(self.api.downloads, [999])
        self.assertFalse(self.destination.exists())

    def test_failed_cancelled_skipped_prepare_is_zero_admission(self):
        for conclusion in ("failure", "cancelled", "skipped", "neutral", None):
            with self.subTest(conclusion=conclusion):
                self.api.downloads.clear()
                self.api.job["conclusion"] = conclusion
                with self.assertRaisesRegex(ArtifactError, "PREPARE_NOT_SUCCESSFUL"):
                    self.load()
                self.assertEqual(self.api.downloads, [999])
                self.assertFalse(self.destination.exists())

    def test_wrong_prepare_identity_or_attempt_is_rejected(self):
        original = copy.deepcopy(self.api.job)
        for key, value in (("run_id", 101), ("run_attempt", 2), ("head_sha", "d" * 40), ("name", "untrusted")):
            self.api.job = {**original, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ArtifactError, "PREPARE_JOB_IDENTITY"):
                self.load()

    def test_native_rerun_wrong_control_and_completed_run_are_rejected(self):
        original = copy.deepcopy(self.api.run)
        for patch in ({"run_attempt": 2}, {"head_sha": "e" * 40}, {"status": "completed"},
                      {"repository": {"id": 1, "full_name": self.api.repo}},
                      {"actor": {"login": "untrusted"}}):
            self.api.run = {**original, **patch}
            with self.subTest(patch=patch), self.assertRaises(ArtifactError):
                self.load()
        self.assertEqual(self.api.downloads, [])

    def test_expiry_cross_repo_wrong_run_and_missing_digest_reject(self):
        original = copy.deepcopy(self.api.metadata[202])
        mutations = (
            lambda m: m.update(expired=True),
            lambda m: m["workflow_run"].update(repository_id=1),
            lambda m: m["workflow_run"].update(id=101),
            lambda m: m.update(digest=None),
            lambda m: m.update(size_in_bytes=2**40),
        )
        for mutate in mutations:
            self.api.metadata[202] = copy.deepcopy(original)
            mutate(self.api.metadata[202])
            with self.assertRaises(ArtifactError):
                self.load()
            self.assertFalse(self.destination.exists())

    def test_bad_download_and_inner_digest_reject(self):
        self.api.zips[202] += b"corrupted"
        with self.assertRaisesRegex(ArtifactError, "ZIP_DIGEST_OR_SIZE"):
            self.load()
        self.api = ArtifactAPI()
        self.api.rewrite_plan(lambda p: p["artifacts"]["source"].update(content_sha256="0" * 64))
        with self.assertRaisesRegex(ArtifactError, "CONTENT_DIGEST"):
            self.load()

    def test_nonce_and_current_base_are_independently_checked(self):
        self.api.run["display_title"] = "lite-gitcode:" + "x" * 24
        with self.assertRaisesRegex(ArtifactError, "NONCE"):
            self.load()
        self.api = ArtifactAPI()
        self.api.personal.open_pull_snapshot.return_value = {
            "identity": {**self.api.plan["pr"], "base_sha": "0" * 40},
            "state": "open", "authorized": True,
        }
        with self.assertRaisesRegex(ArtifactError, "VERSION_STALE"):
            self.load()

    def test_early_intent_is_read_before_prepare_and_has_no_source_download(self):
        self.api.job.update(status="in_progress", conclusion=None)
        result = self.api.reader.early_intent(100, now=1100)
        self.assertEqual(result[0], self.api.intent)
        self.assertEqual(self.api.downloads, [201])
        self.assertFalse(self.destination.exists())

    def test_final_run_recheck_closes_cancellation_window(self):
        original = self.api.download_artifact

        def download(artifact_id, *, limit):
            payload = original(artifact_id, limit=limit)
            if artifact_id != 999:
                self.api.run["status"] = "completed"
            return payload

        self.api.download_artifact = download
        with self.assertRaisesRegex(ArtifactError, "NOT_AUTHORIZED_OR_ACTIVE"):
            self.load()
        self.assertFalse(self.destination.exists())

    def test_wrong_check_app_fails_before_any_extraction(self):
        next(iter(self.api.checks.values()))["app"] = {"id": 1000}
        with self.assertRaisesRegex(ValueError, "CHECK_SLOT_IDENTITY"):
            self.load()
        self.assertFalse(self.destination.exists())

    def test_source_and_trusted_runner_archives_both_reject_links(self):
        for which in ("source", "runner"):
            self.api = ArtifactAPI(**{which: tar_bytes(link=True)})
            with self.subTest(which=which), self.assertRaisesRegex(SourceError, "UNSAFE_ARCHIVE_MEMBER"):
                self.load()
            self.assertFalse(self.destination.exists())

    def test_materialized_bundle_tampering_is_detected(self):
        loaded = self.load()
        loaded.runner_path.write_bytes(b"changed")
        with self.assertRaisesRegex(ArtifactError, "RUNNER_DIGEST"):
            loaded.verify_files()

    def test_existing_workspace_is_never_replaced(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep"
        sentinel.write_text("existing")
        with self.assertRaisesRegex(ArtifactError, "WORKSPACE_MUST_BE_NEW"):
            self.load()
        self.assertEqual(sentinel.read_text(), "existing")

    def test_zip_names_duplicates_symlinks_and_oversize(self):
        with self.assertRaisesRegex(ArtifactError, "MEMBERS"):
            unzip_one(zip_bytes("../ci-request.json", b"{}"), "ci-request.json", limit=100)
        with self.assertRaisesRegex(ArtifactError, "OVERSIZED"):
            unzip_one(zip_bytes("ci-request.json", b"a" * 1000), "ci-request.json", limit=100)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("ci-request.json")
            info.create_system, info.external_attr = 3, (0o120777 << 16)
            archive.writestr(info, "/tmp/outside")
        with self.assertRaisesRegex(ArtifactError, "UNSAFE"):
            unzip_one(stream.getvalue(), "ci-request.json", limit=100)


if __name__ == "__main__":
    unittest.main()
