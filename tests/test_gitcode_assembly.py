"""Trusted wiring tests use synthetic env values and no live credentials/network."""

import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.executor import Execution
from ci_core.gitcode_api import GitCodeClient
from ci_core.gitcode_artifacts import ArtifactError
from ci_core.gitcode_assembly import (
    AssemblyError,
    assemble_blue,
    assemble_bridge,
    assemble_hosted,
    load_release,
)
from ci_core.gitcode_contract import canonical
from ci_core.gitcode_platform import PlatformError
from tests.test_gitcode_pipeline import HostingAPI


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = HostingAPI(self.root)
        self.fixture = fixture
        self.config = {
            "schema": 1,
            "sdk_sha": "a" * 40,
            "authority_mode": "observed_history_versioned_only",
            "policy": asdict(fixture.policy),
            "catalog": fixture.reader.catalog,
            "personal_repo": "depeng1994/torchtitan-npu",
            "upstream_repo": "cann/torchtitan-npu",
            "github_actor": fixture.policy.allowed_actors[0],
            "checks_installation_id": 123,
        }
        self.path = self.root / "release.json"
        self.write()
        self.environment = {
            "LITE_ACTIONS_HOSTED_PHASE": "prepare",
            "LITE_ACTIONS_GITHUB_TOKEN": "synthetic-read-dispatch",
            "LITE_ACTIONS_GITCODE_TOKEN": "synthetic-authority",
            "LITE_ACTIONS_CHECKS_KEY_FILE": str(self.root / "unused-test-key.pem"),
        }

    def write(self):
        self.path.write_bytes(canonical(self.config))
        self.path.chmod(0o600)

    def hosted(self):
        return assemble_hosted(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "state",
        )

    def test_dependency_profile_is_explicit_bound_and_wired(self):
        from ci_core.gitcode_dependencies import PROFILE, DependencyPolicyError

        self.config["source_dependency_profile"] = PROFILE
        self.write()
        with self.assertRaises(DependencyPolicyError):
            self.hosted()
        self.config["policy"]["policy_version"] += ":" + PROFILE
        self.write()
        runtime = self.hosted()
        self.assertEqual(runtime.reader.source_dependency_profile, PROFILE)
        self.assertEqual(runtime.producer.source_dependency_profile, PROFILE)
        self.assertEqual(runtime.producer.reader.source_dependency_profile, PROFILE)
        del self.config["source_dependency_profile"]
        self.write()
        with self.assertRaises(DependencyPolicyError):
            self.hosted()

    def test_concrete_hosted_clients_are_separate_and_no_authority_is_invented(self):
        runtime = self.hosted()
        self.assertIs(runtime.producer.github, runtime.reader.github)
        self.assertIsNot(runtime.reader.channel.client, runtime.reader.github)
        self.assertIsNone(runtime.authority)
        self.assertIsNone(runtime.reader.read_snapshot)
        self.assertTrue((self.root / "state").is_dir())
        self.assertFalse((self.root / "state" / "authority.sqlite3").exists())
        with self.assertRaisesRegex(AssemblyError, "HOSTED_CURRENT_AUTHORITY"):
            runtime.current({})
        with self.assertRaisesRegex(AssemblyError, "PUBLICATION_ADAPTER_REQUIRED"):
            runtime.publish_result({}, {})
        self.assertNotIn("synthetic-", repr(runtime))

    def test_bridge_and_blue_share_same_authority_ledger_and_store(self):
        bridge = assemble_bridge(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "state",
        )
        adapter = assemble_blue(
            [self.path],
            environment=self.environment,
            state_directory=self.root / "state",
        )
        broker = next(iter(adapter.brokers.values()))
        self.assertEqual(broker.read_authority.__self__.path, bridge.authority.path)
        self.assertEqual(broker.coordinator.path.name, "coordinator.sqlite3")
        self.assertEqual(broker.store.root.name, "requests")
        self.assertIs(
            broker.reader.read_snapshot.__self__, broker.read_authority.__self__
        )
        self.assertIs(bridge.reader.read_snapshot.__self__, bridge.authority)

    def test_real_assemble_blue_broker_counts_fresh_authority_and_persists_revocation(
        self,
    ):
        class GitCodeTransport:
            def __init__(transport_self):
                self_fixture = self.fixture
                identity = self_fixture.intent["pr"]
                transport_self.repo = self.config["personal_repo"]
                transport_self.metadata = {
                    "id": int(self.config["policy"]["gitcode_repo_id"]),
                    "full_name": transport_self.repo,
                    "default_branch": "master",
                }
                transport_self.pr = {
                    "number": identity["number"],
                    "state": "open",
                    "merged_at": None,
                    "user": {"login": self.config["policy"]["allowed_actors"][0]},
                    "head": {
                        "ref": identity["head_ref"],
                        "sha": identity["head_sha"],
                        "repo": {"full_name": transport_self.repo},
                    },
                    "base": {
                        "ref": identity["base_ref"],
                        "sha": identity["base_sha"],
                        "repo": {"full_name": transport_self.repo},
                    },
                }
                transport_self.branches = {
                    identity["head_ref"]: identity["head_sha"],
                    identity["base_ref"]: identity["base_sha"],
                }
                transport_self.calls = []

            def __call__(transport_self, path):
                from urllib.parse import unquote

                transport_self.calls.append(path)
                if path == "/user":
                    return {"login": self.config["github_actor"]}
                if path == "/repos/" + transport_self.repo:
                    return dict(transport_self.metadata)
                if (
                    path
                    == f"/repos/{transport_self.repo}/pulls/{self.fixture.intent['pr']['number']}"
                ):
                    return dict(transport_self.pr)
                if "/branches/" in path:
                    ref = unquote(path.split("/branches/", 1)[1])
                    return {"name": ref, "commit": {"id": transport_self.branches[ref]}}
                raise AssertionError("unexpected fake GitCode route")

        class FakeGitHub:
            def __init__(fake_self, token, repo, *, api_version, schema4_repo_id):
                fake_self.repo = repo
                fake_self.repo_id = schema4_repo_id
                fake_self.api = "https://api.github.com/repos/" + repo
                fake_self.calls = []
                fake_self.run_status = "in_progress"

            def request(fake_self, method, path, data=None):
                fake_self.calls.append((method, path))
                if method == "GET" and path == "/actions/runs/100":
                    return {
                        "id": 100,
                        "run_attempt": 1,
                        "head_sha": self.config["policy"]["control_sha"],
                        "head_branch": "master",
                        "workflow_id": self.config["policy"]["workflow_id"],
                        "event": "workflow_dispatch",
                        "repository": {
                            "id": self.config["policy"]["github_repo_id"],
                            "full_name": fake_self.repo,
                        },
                        "path": self.config["policy"]["workflow_path"],
                        "actor": {"login": self.config["github_actor"]},
                        "status": fake_self.run_status,
                    }
                raise AssertionError("unexpected fake GitHub route")

        transport = GitCodeTransport()
        real_client = GitCodeClient

        def gitcode_factory(repo, repo_id, **kwargs):
            return real_client(repo, repo_id, transport=transport, **kwargs)

        with (
            patch(
                "ci_core.gitcode_assembly.GitCodeClient", side_effect=gitcode_factory
            ),
            patch("ci_core.gitcode_assembly.GitHubClient", FakeGitHub),
        ):
            adapter = assemble_blue(
                [self.path],
                environment=self.environment,
                state_directory=self.root / "assembled-blue-state",
            )

        broker = next(iter(adapter.brokers.values()))
        provider = broker.verify_event.__self__
        provider.clock = lambda: 1100
        broker.clock = lambda: 1100
        intent = provider.create_intent(
            self.fixture.intent["pr"]["number"],
            [{"test_id": "dispatch_smoke_8p"}],
            pipeline="smoke",
        )
        # Use the real producer/artifact reader fixture to obtain a VerifiedRequest
        # for the exact intent enrolled by the assembled Blue authority provider.
        self.fixture.intent = intent
        self.fixture.run["display_title"] = (
            "lite-gitcode:" + intent["bridge"]["bridge_nonce"]
        )
        plan = self.fixture.produce()
        self.fixture.job.update(status="completed", conclusion="success")
        loaded = self.fixture.reader.load(
            100, self.root / "assembled-reader-load", now=1100
        )
        self.assertIsNotNone(loaded)
        run = dict(self.fixture.run)
        broker.reader.early_intent = lambda run_id, *, now: (intent, run)
        broker.reader.load = lambda run_id, destination, *, now: loaded

        transport.calls.clear()
        accepted = broker.admit(100)
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.verified.plan["plan_digest"], plan["plan_digest"])
        # Initial admission observation + post-materialization observation.
        self.assertEqual(len(transport.calls), 12)
        self.assertEqual(sum("/pulls/" in path for path in transport.calls), 4)
        self.assertEqual(sum("/branches/" in path for path in transport.calls), 4)

        for stage in ("preallocation", "before_launch", "finish_pass"):
            before = len(transport.calls)
            broker.startable_source(accepted)
            self.assertEqual(len(transport.calls) - before, 6, stage)
            broker.reader.actual_run(100)
        before = len(transport.calls)
        self.assertTrue(broker.assert_plan_current(plan))  # aggregate publication guard
        self.assertEqual(len(transport.calls) - before, 6, "aggregate_publish")
        broker.reader.actual_run(100)

        # Exercise the actual assembled RuntimeAdapter work -> before_launch ->
        # finish(PASS) -> flush/current callback path. Only the external result
        # publication transport is stubbed; every freshness/lease guard is real.
        class MemoryState:
            def __init__(state_self, record):
                state_self.records = {"assembled-case": record}

            def read(state_self):
                return state_self.records

            def update(state_self, key, **fields):
                state_self.records[key].update(fields)

        case = plan["cases"][0]
        record = {
            "protocol": 4,
            "gitcode": {
                "request_id": f"{plan['github']['repo_id']}/{plan['github']['run_id']}/{plan['github']['run_attempt']}",
                "execution_id": accepted.execution_id,
                "plan_digest": plan["plan_digest"],
            },
            "item": case,
            "phase": "QUEUED",
        }
        adapter.clock = lambda: 1100
        adapter.router = SimpleNamespace(
            worker="blue",
            state=MemoryState(record),
            pools={},
            root=self.root,
            workers={"blue": {}},
            active={},
        )

        def cpu_only_executor(**kwargs):
            cleanup = {
                "nodes": [
                    {
                        "host_id": "synthetic-host:22",
                        "container_id": "a" * 64,
                        "image_id": "sha256:" + "b" * 64,
                        "task_marker": "assembly-cpu-only",
                        "device_ids": list(range(8)),
                    }
                ]
            }
            kwargs["capture"]({"lease_id": "lease-assembly-cpu", "cleanup": cleanup})
            kwargs["before_launch"]()
            return Execution(
                0, ["CPU-only adapter regression"], self.root / "cpu.log", True
            )

        adapter.executor = cpu_only_executor
        adapter.delivery.flush = lambda: {"delivered": [], "pending": []}
        publish_current_calls = []

        def publish_complete(verified, receipt, results, *, current):
            publish_current_calls.append(current())
            return {"test_result": "PASS" if publish_current_calls[-1] else "STALE"}

        adapter.delivery.publish_complete = publish_complete
        adapter.work("assembled-case", case, threading.Event())
        self.assertEqual(
            adapter.router.state.read()["assembled-case"]["report"]["status"], "PASS"
        )
        before = len(transport.calls)
        adapter.flush()
        self.assertEqual(publish_current_calls, [True])
        self.assertEqual(len(transport.calls) - before, 6)
        self.assertEqual(
            adapter.router.state.read()["assembled-case"]["phase"], "REPORTED"
        )

        github = broker.reader.github
        github.run_status = "completed"
        with self.assertRaisesRegex(ArtifactError, "NOT_AUTHORIZED_OR_ACTIVE"):
            broker.reader.actual_run(100)

        # Base branch movement between guards is a new observation and a durable
        # revocation. It must not reuse the earlier positive guard result.
        transport.branches["master"] = "e" * 40
        with self.assertRaisesRegex(PlatformError, "REVOKED"):
            broker.assert_plan_current(plan)
        with broker.coordinator._connection() as db:
            revoked = db.execute(
                "SELECT revoked FROM source_intents WHERE id=?",
                (intent["bridge"]["dispatch_intent_id"],),
            ).fetchone()[0]
        self.assertEqual(revoked, 1)

    def test_manual_api_capture_is_explicit_and_wired_without_shared_hosted_db(self):
        self.config["manual_capture"] = {
            "enabled": True,
            "workflow_id": "manual-workflow",
            "workflow_path": ".gitcode/workflows/manual_capture.yml",
            "job_identifier": "manual_capture",
            "step_identifier": "1",
            "step_name": "Capture manual request",
            "script_sha256": "a" * 64,
            "shell": "bash",
            "working_directory": "",
            "request_authority": "github_dispatch_operator",
        }
        self.write()
        runtime = self.hosted()
        self.assertIsNone(runtime.authority)
        adapter = assemble_blue(
            [self.path],
            environment=self.environment,
            state_directory=self.root / "blue-state",
        )
        broker = next(iter(adapter.brokers.values()))
        provider = broker.read_authority.__self__
        self.assertIsNotNone(provider.manual_capture_policy)
        self.assertIsNotNone(broker.enroll_intent)
        self.assertIsNone(provider.attestation_policy)
        provider.enroll_manual = Mock(return_value=None)
        broker.reader.early_attestation = Mock(
            side_effect=AssertionError("manual needs no signed sidecar")
        )
        self.assertTrue(broker.enroll_intent({"opaque": "intent"}, {"id": 9}))
        provider.enroll_manual.assert_called_once_with(
            {"opaque": "intent"}, github_run={"id": 9}
        )

    def test_missing_token_has_no_gh_fallback_or_secret_error(self):
        del self.environment["LITE_ACTIONS_GITHUB_TOKEN"]
        with patch("ci_core.github.authorization") as fallback:
            with self.assertRaisesRegex(AssemblyError, "CREDENTIAL_UNAVAILABLE"):
                self.hosted()
            fallback.assert_not_called()

    def test_config_cannot_enable_other_repository_or_unproven_native_mode(self):
        for key, value in (("enabled", False), ("github_repo_id", 1438625957)):
            before = dict(self.config["policy"])
            self.config["policy"][key] = value
            self.write()
            with self.assertRaises(AssemblyError):
                load_release(self.path)
            self.config["policy"] = before
        self.config["authority_mode"] = "native"
        self.write()
        with self.assertRaisesRegex(AssemblyError, "UNREVIEWED_AUTHORITY_MODE"):
            load_release(self.path)

    def test_secret_fields_unknown_keys_and_unsafe_catalog_rejected(self):
        self.config["token"] = "must-never-be-accepted"
        self.write()
        with self.assertRaises(ValueError):
            load_release(self.path)
        del self.config["token"]
        self.config["catalog"]["arbitrary_shell"] = {}
        self.write()
        with self.assertRaisesRegex(AssemblyError, "CATALOG"):
            load_release(self.path)

    def test_config_and_state_symlinks_and_open_permissions_rejected(self):
        link = self.root / "link.json"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(AssemblyError, "CONFIG_PATH"):
            load_release(link)
        self.path.chmod(0o666)
        with self.assertRaisesRegex(AssemblyError, "OWNERSHIP_OR_MODE"):
            load_release(self.path)
        self.path.chmod(0o600)
        state = self.root / "state"
        state.mkdir(mode=0o755)
        with self.assertRaisesRegex(AssemblyError, "OWNER_ONLY_STATE"):
            self.hosted()

    def test_completed_canonical_resolution_checks_exact_pointer(self):
        runtime = self.hosted()
        fixture = HostingAPI(self.root)
        plan = fixture.plan
        pointer = {
            "request_id": f"{plan['github']['repo_id']}/{plan['github']['run_id']}/1",
            "plan_digest": plan["plan_digest"],
            "github": plan["github"],
            "result_slots": plan["result_slots"],
        }
        runtime.reader.load_results = Mock(return_value=Mock(plan=plan))
        self.assertEqual(runtime.resolve_canonical(pointer).plan, plan)
        pointer["plan_digest"] = "0" * 64
        with self.assertRaisesRegex(AssemblyError, "POINTER_MISMATCH"):
            runtime.resolve_canonical(pointer)

    def test_authority_directory_cannot_be_rebound_to_another_source(self):
        assemble_bridge(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "state",
        )
        self.config["personal_repo"] = "another/torchtitan-npu"
        self.write()
        with self.assertRaisesRegex(AssemblyError, "DIRECTORY_BINDING"):
            assemble_bridge(
                self.path,
                environment=self.environment,
                upload=Mock(),
                state_directory=self.root / "state",
            )

    def test_blue_both_coordinators_bind_same_trusted_decision_clock(self):
        now = [1102.25]
        adapter = assemble_blue(
            [self.path],
            environment=self.environment,
            state_directory=self.root / "clock-state",
            clock=lambda: now[0],
        )
        self.assertEqual(adapter.coordinator.decision_clock(), 1102)
        for broker in adapter.brokers.values():
            provider = broker.verify_plan.__self__
            self.assertEqual(provider.coordinator.decision_clock(), 1102)
            now[0] = 1103.25
            self.assertEqual(provider.coordinator.decision_clock(), 1103)
            self.assertEqual(broker.coordinator.decision_clock(), 1103)
            self.assertEqual(broker._now(), 1103)

    def test_invalid_release_never_opens_coordinator_storage(self):
        self.config["policy"]["enabled"] = False
        self.write()
        with patch("ci_core.gitcode_assembly.Coordinator") as coordinator:
            with self.assertRaises(AssemblyError):
                assemble_blue(
                    [self.path],
                    environment=self.environment,
                    state_directory=self.root / "state",
                )
            coordinator.assert_not_called()

    def test_pull_authority_capability_is_explicit_and_wired_both_sides(self):
        self.config["authority_protocol"] = "artifact-check-v1"
        self.write()
        with self.assertRaisesRegex(AssemblyError, "HOSTED_AUTHORITY_RUN_ID"):
            self.hosted()
        self.environment["GITHUB_RUN_ID"] = "100"
        runtime = self.hosted()
        self.assertIsNotNone(runtime.hosted_current)
        self.assertEqual(runtime.authority_protocol, "artifact-check-v1")
        adapter = assemble_blue(
            [self.path],
            environment=self.environment,
            state_directory=self.root / "blue-state",
        )
        self.assertEqual(set(adapter.responders), set(adapter.brokers))
        self.assertTrue(
            all(b.verify_plan is not None for b in adapter.brokers.values())
        )

    def test_enabled_publication_requires_separate_credential(self):
        self.config["publication"] = {
            "enabled": True,
            "actor": self.config["github_actor"],
        }
        self.write()
        with self.assertRaisesRegex(AssemblyError, "CREDENTIAL_UNAVAILABLE"):
            assemble_bridge(
                self.path,
                environment=self.environment,
                upload=Mock(),
                state_directory=self.root / "state",
            )
        self.environment["LITE_ACTIONS_GITCODE_PUBLICATION_TOKEN"] = (
            "synthetic-separate-publication"
        )
        runtime = assemble_bridge(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "state",
        )
        self.assertIsNotNone(runtime.publisher)
        self.assertIsNot(runtime.publisher.client, runtime.reader.personal)

    def test_wait_and_bridge_have_no_private_key_and_checks_writes_are_impossible(self):
        del self.environment["LITE_ACTIONS_CHECKS_KEY_FILE"]
        self.environment["LITE_ACTIONS_HOSTED_PHASE"] = "wait"
        waiter = self.hosted()
        bridge = assemble_bridge(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "bridge-state",
        )
        for runtime in (waiter, bridge):
            self.assertIsNone(runtime.producer)
            self.assertIsNone(runtime.reader.channel.journal)
            with self.assertRaisesRegex(RuntimeError, "READ_ONLY_CHECKS"):
                runtime.reader.channel.client.request("POST", "/check-runs", {})
            with self.assertRaisesRegex(RuntimeError, "READ_ONLY_CHECKS"):
                runtime.reader.channel.client.request("PATCH", "/check-runs/1", {})
        self.environment["LITE_ACTIONS_HOSTED_PHASE"] = "prepare"
        with self.assertRaisesRegex(AssemblyError, "KEY_PATH_REQUIRED"):
            self.hosted()

    def test_attestation_is_disabled_by_default_and_enabled_policy_has_no_private_key(
        self,
    ):
        self.assertIsNone(load_release(self.path)["attestation_policy"])
        self.config["attestation"] = {"enabled": False}
        self.write()
        self.assertIsNone(load_release(self.path)["attestation_policy"])
        self.config["attestation"] = {
            "enabled": True,
            "key_id": "candidate-v1",
            "public_key_file": str(self.root / "not-provisioned-public.pem"),
            "public_key_sha256": "d" * 64,
            "issuer_repo": "depeng1994/torchtitan-npu",
            "issuer_repo_id": "42",
            "head_repo_id": self.config["policy"]["gitcode_repo_id"],
            "workflow_path": ".gitcode/workflows/ci-github-bridge.yml",
            "workflow_source_sha": "e" * 40,
            "workflow_id": "b" * 32,
            "allowed_event_kinds": ["pull_request"],
            "allowed_platform_events": ["PULL_REQUEST"],
        }
        self.write()
        parsed = load_release(self.path)["attestation_policy"]
        self.assertEqual(parsed.workflow_id, "b" * 32)
        self.assertEqual(parsed.allowed_run_statuses, ("RUNNING",))
        runtime = assemble_bridge(
            self.path,
            environment=self.environment,
            upload=Mock(),
            state_directory=self.root / "state",
        )
        self.assertEqual(runtime.authority.attestation_policy, parsed)
        self.config["attestation"]["issuer_repo_id"] = "77"
        self.write()
        with self.assertRaisesRegex(ValueError, "TRUSTED_TARGET_ISSUER"):
            load_release(self.path)
        self.config["attestation"]["allow_external_issuer"] = True
        self.write()
        self.assertTrue(
            load_release(self.path)["attestation_policy"].allow_external_issuer
        )

    def test_cli_rejects_yellow_before_any_runtime_assembly(self):
        import unified_router

        with patch(
            "sys.argv",
            [
                "router",
                "--worker-id",
                "yellow",
                "--gitcode-config",
                str(self.path),
                "--gitcode-state-directory",
                str(self.root / "state"),
            ],
        ):
            with self.assertRaises(SystemExit) as raised:
                unified_router.main()
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
