"""Exact personal staging profile; no real platform or credential operations."""

import copy
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

from ci_core.contract import SUPPORTED_REPOS
from ci_core.gitcode_artifacts import ArtifactError, GitCodeArtifactReader
from ci_core.gitcode_assembly import AssemblyError, assemble_blue, load_release
from ci_core.gitcode_checks import CheckRunChannel, CreationJournal
from ci_core.gitcode_contract import (
    MODEL_REPOSITORIES,
    ContractError,
    canonical,
    request_id,
)
from ci_core.gitcode_producer import GitCodeProducer
from ci_core.github import GitHubClient, MultiRepoGitHubClient
from tests.test_gitcode_artifacts import tar_bytes
from tests.test_gitcode_pipeline import HostingAPI

STAGE_ID = 1245430804
STAGE_REPO = "depeng1994/torchtitan-npu"


def staging_api(root):
    root.mkdir(parents=True)
    api = HostingAPI(root)
    api.policy = replace(
        api.policy,
        github_repo_id=STAGE_ID,
        gitcode_repo_id="9075802",
        upstream_repo_id="9075802",
        deployment_profile="personal-staging",
        app_id=9999,
    )
    api.repo, api.repo_id = STAGE_REPO, STAGE_ID
    api.api = "https://api.github.com/repos/" + STAGE_REPO
    api.intent["control_repo_id"] = STAGE_ID
    for key in ("repo_id", "head_repo_id", "base_repo_id"):
        api.intent["pr"][key] = "9075802"
    api.run.update(
        repository_id=STAGE_ID,
        repository={"id": STAGE_ID, "full_name": STAGE_REPO},
        _ci_repo=STAGE_REPO,
    )
    api.personal.repo_id = "9075802"
    api.personal.open_pull_snapshot.return_value = {
        "identity": copy.deepcopy(api.intent["pr"]),
        "state": "open",
        "authorized": True,
    }
    api.upstream.repo_id = "9075802"
    api.upstream.control_tip.return_value["repo_id"] = "9075802"
    catalog = api.reader.catalog
    api.channel = CheckRunChannel(
        api,
        repo_id=STAGE_ID,
        app_id=9999,
        journal=CreationJournal(root / "staging-checks.sqlite3"),
    )
    api.reader = GitCodeArtifactReader(
        policy=api.policy,
        github=api,
        personal=api.personal,
        channel=api.channel,
        catalog=catalog,
    )
    api.producer = GitCodeProducer(
        policy=api.policy,
        catalog=catalog,
        github=api,
        personal=api.personal,
        upstream=api.upstream,
        channel=api.channel,
        artifact_journal=api.upload_journal,
        upload=api.upload,
        clock=lambda: 1100,
        source_builder=api.build_source,
    )
    return api


class PersonalStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = staging_api(self.root / "stage")

    def test_exact_profile_is_opt_in_and_never_relabels_official_identity(self):
        for update in (
            {"deployment_profile": "production"},
            {"github_repo_id": 1210038933},
            {"github_repo_id": 123456789},
            {"gitcode_repo_id": "9028822"},
            {"upstream_repo_id": "9028822"},
            {"upstream_default_ref": "ci"},
            {"allowed_base_refs": ("ci",)},
            {"app_id": 5575236},
            {"allowed_head_repositories": (("other/repo", "1"),)},
        ):
            with self.subTest(update=update), self.assertRaises(ContractError):
                replace(self.stage.policy, **update)
        with self.assertRaises(TypeError):
            MODEL_REPOSITORIES[STAGE_ID] = ("other/repo", "master")
        self.assertNotIn(STAGE_REPO, SUPPORTED_REPOS)

    def test_clients_are_independent_and_schema3_staging_remains_forbidden(self):
        official = GitHubClient("synthetic", "cann-framework/torchtitan-npu")
        stage = GitHubClient("synthetic", STAGE_REPO, schema4_repo_id=STAGE_ID)
        self.assertEqual(official.repo_id, 1210038933)
        self.assertEqual(stage.repo_id, STAGE_ID)
        self.assertTrue(stage.schema4_only)
        with self.assertRaises(ValueError):
            GitHubClient("synthetic", STAGE_REPO)
        with self.assertRaises(ValueError):
            GitHubClient("synthetic", STAGE_REPO, schema4_repo_id=1210038933)
        poller = MultiRepoGitHubClient(
            "synthetic", (official.repo,), schema4_clients=(stage,)
        )
        self.assertIs(poller.for_repo(STAGE_REPO), stage)
        self.assertEqual(poller.for_repo(official.repo).repo_id, 1210038933)

    def test_staging_poll_cannot_ingest_native_or_other_repository_runs(self):
        client = GitHubClient("synthetic", STAGE_REPO, schema4_repo_id=STAGE_ID)
        valid = {**self.stage.run, "status": "in_progress"}
        native = {**valid, "id": 101, "display_title": "ordinary", "name": "ordinary"}
        foreign = {
            **valid,
            "id": 102,
            "repository": {
                "id": 1210038933,
                "full_name": "cann-framework/torchtitan-npu",
            },
        }
        client.request = Mock(return_value={"workflow_runs": [valid, native, foreign]})
        self.assertEqual([run["id"] for run in client.running_workflows()], [100])
        self.assertTrue(
            all(
                "event=workflow_dispatch" in c.args[1]
                for c in client.request.call_args_list
            )
        )

    def test_real_producer_reader_keep_staging_control_source_and_slots_bound(self):
        plan = self.stage.produce()
        self.assertEqual(request_id(plan), "1245430804/100/1")
        self.assertEqual(plan["control"]["upstream_repo_id"], "9075802")
        self.assertEqual(plan["github"]["repo"], STAGE_REPO)
        self.stage.job.update(status="completed", conclusion="success")
        loaded = self.stage.reader.load(100, self.root / "load", now=1100)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.verified.plan["pr"]["repo_id"], "9075802")
        loaded.source.verify()
        for slot in plan["result_slots"].values():
            self.assertEqual(slot["app_id"], 9999)
        self.stage.run["repository"] = {
            "id": 1210038933,
            "full_name": "cann-framework/torchtitan-npu",
        }
        with self.assertRaises(ArtifactError):
            self.stage.reader.actual_run(100)

    def test_staging_still_requires_exact_default_branch_u_equals_c(self):
        for field, value in (
            ("repo_id", "9028822"),
            ("sha", "f" * 40),
            ("default_ref", "ci"),
        ):
            before = self.stage.upstream.control_tip.return_value[field]
            self.stage.upstream.control_tip.return_value[field] = value
            with self.assertRaisesRegex(ValueError, "CONTROL_RELEASE_NOT_PUBLISHED"):
                self.stage.produce()
            self.assertEqual(self.stage.uploads, [])
            self.stage.upstream.control_tip.return_value[field] = before

    def test_staging_does_not_skip_torchtitan_source_interface_checks(self):
        self.stage.source_bytes = tar_bytes(
            {"unexpected.py": b"# missing required API"}
        )
        with self.assertRaisesRegex(ValueError, "SOURCE_INTERFACE_MISSING"):
            self.stage.produce()
        self.assertNotIn("lite-ci-request-1", self.stage.uploads)

    def test_staging_rejects_cross_repository_artifact_and_foreign_check_app(self):
        self.stage.produce()
        self.stage.job.update(status="completed", conclusion="success")
        artifact = next(iter(self.stage.metadata.values()))
        artifact["workflow_run"]["repository_id"] = 1210038933
        with self.assertRaises(ArtifactError):
            self.stage.reader.early_intent(100, now=1100)
        artifact["workflow_run"]["repository_id"] = STAGE_ID
        for check in self.stage.checks.values():
            check["app"]["id"] = 5575236
        with self.assertRaises(ValueError):
            self.stage.reader.load(100, self.root / "foreign-check", now=1100)

    def test_assembly_profiles_cannot_share_state_or_use_official_paths(self):
        config = {
            "schema": 1,
            "sdk_sha": "a" * 40,
            "authority_mode": "observed_history_versioned_only",
            "policy": asdict(self.stage.policy),
            "catalog": self.stage.reader.catalog,
            "personal_repo": STAGE_REPO,
            "upstream_repo": STAGE_REPO,
            "github_actor": "depeng1994",
            "checks_installation_id": 999,
        }
        path = self.root / "stage.json"
        path.write_bytes(canonical(config))
        path.chmod(0o600)
        env = {
            "LITE_ACTIONS_GITHUB_TOKEN": "synthetic",
            "LITE_ACTIONS_GITCODE_TOKEN": "synthetic",
            "LITE_ACTIONS_CHECKS_KEY_FILE": str(self.root / "never-read.pem"),
        }
        adapter = assemble_blue(
            [path], environment=env, state_directory=self.root / "ledger"
        )
        broker = next(iter(adapter.brokers.values()))
        self.assertEqual(broker.reader.github.repo_id, STAGE_ID)
        self.assertEqual(broker.reader.channel.app_id, 9999)
        bad = {**config, "upstream_repo": "cann/torchtitan-npu"}
        path.write_bytes(canonical(bad))
        with self.assertRaisesRegex(AssemblyError, "STAGING_REPOSITORY_PATH"):
            load_release(path)
        official_policy = replace(
            self.stage.policy,
            github_repo_id=1210038933,
            deployment_profile="production",
            app_id=5575236,
        )
        official = {**config, "policy": asdict(official_policy)}
        other = self.root / "official.json"
        other.write_bytes(canonical(official))
        other.chmod(0o600)
        path.write_bytes(canonical(config))
        with self.assertRaisesRegex(
            AssemblyError, "BLUE_AUTHORITY_REPOSITORY_MISMATCH"
        ):
            assemble_blue(
                [path, other], environment=env, state_directory=self.root / "mixed"
            )
        with self.assertRaisesRegex(
            AssemblyError, "AUTHORITY_DIRECTORY_BINDING_MISMATCH"
        ):
            assemble_blue(
                [other], environment=env, state_directory=self.root / "ledger"
            )
