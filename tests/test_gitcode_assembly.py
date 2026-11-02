"""Trusted wiring tests use synthetic env values and no live credentials/network."""

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from ci_core.gitcode_assembly import (
    AssemblyError,
    assemble_blue,
    assemble_bridge,
    assemble_hosted,
    load_release,
)
from ci_core.gitcode_contract import canonical
from tests.test_gitcode_pipeline import HostingAPI


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = HostingAPI(self.root)
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
