"""Source/runner execution boundaries; transport is simulated, never NPU PASS."""

import copy
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core import gitcode_execution as backend
from ci_core.executor import ExternalNpuOccupancy
from ci_core.gitcode_contract import VerifiedRequest, canonical
from ci_core.gitcode_source import SourceHandle
from ci_core.gitcode_store import StoredExecution
from tests.test_gitcode_artifacts import tar_bytes


class GitCodeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.item = {
            "case_id": "case_001_dispatch_smoke_8p",
            "order": 1,
            "test_id": "dispatch_smoke_8p",
            "label": "Trusted fixture",
            "kind": "npu_smoke",
            "worker": "blue",
            "target_pool": "a3_shared_blue",
            "docker": backend.DOCKER,
            "resources": {"nodes": 1, "devices_per_node": 8},
            "execution_timeout_seconds": 75,
        }
        self.profile = {"env_vars": {"ASCEND_SET_ENV_PATH": "/trusted/set_env.sh"}}
        self.catalog = {
            "schema": 1,
            "runner_protocol": "lite-gitcode-torchtitan-v1",
            "cases": {
                self.item["test_id"]: {
                    k: v for k, v in self.item.items() if k not in {"case_id", "order"}
                }
            },
            "profiles": {self.item["test_id"]: self.profile},
        }
        self.runner_files = {
            name: b"raise RuntimeError('control must never import this file')\n"
            for name in (
                "runner.py",
                "smoke_rank.py",
                "_lite_gitcode_train.py",
                "torchrun_lease.py",
            )
        }
        self.runner_files["catalog.json"] = canonical(self.catalog)
        source = self.root / "source.tar.gz"
        source.write_bytes(
            tar_bytes(
                {
                    ".github/scripts/lite_actions/gitcode/smoke_payload.py": b"raise RuntimeError('control must never import source')\nPAYLOAD_API_VERSION = 1\ndef all_reduce(tensor): pass\n"
                }
            )
        )
        self.handle = SourceHandle(
            provider="gitcode",
            head_sha="a" * 40,
            base_sha="b" * 40,
            tested_merge_sha="c" * 40,
            tested_tree_sha="d" * 40,
            bundle_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            bundle_path=str(source),
            merge_policy="git-ort-v1",
            git_version="2.43.0",
            dependencies=(),
        )
        self.runner_path = self.root / "runner.tar.gz"
        self.set_runner(self.runner_files)
        self.node = {
            "host_id": "192.0.2.1:22",
            "host_ssh": "192.0.2.1:22",
            "ssh": "192.0.2.1:22/" + "a" * 64,
            "container_name": backend.DOCKER,
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "node_ip": "192.0.2.1",
            "network_interface": "eth0",
            "pythonpath": "/trusted/dependencies",
            "position": 0,
            "device_ids": list(range(8)),
            "python": "/opt/python/bin/python3",
        }
        self.case = {
            "enabled": True,
            "pool": "a3_shared_blue",
            "docker": backend.DOCKER,
            "allocation": [
                {"host_id": self.node["host_id"], "devices": list(range(8))}
            ],
            "master_port": 16000,
            "hccl_if_base_port": 20000,
            "hccl_host_port_range": "24000-24063",
            "hccl_npu_port_range": "28000-28063",
        }
        self.pools = {"a3_shared_blue": {"process_indexing": "card-die"}}
        self.captured = None
        self.events = []
        self.cancel = threading.Event()

    def set_runner(self, files):
        self.runner_path.write_bytes(tar_bytes(files))
        self.plan = {
            "cases": [self.item],
            "deadline": int(time.time()) + 300,
            "control": {
                "runner_sha256": hashlib.sha256(
                    self.runner_path.read_bytes()
                ).hexdigest()
            },
        }
        self.stored = StoredExecution(
            VerifiedRequest(canonical(self.plan)), self.handle, self.runner_path
        )

    def capture(self, value):
        self.events.append("capture")
        self.captured = {**value, "fencing_token": 42}
        return self.captured

    def stage(self, node, stored, runtime):
        self.assertIsNotNone(self.captured)
        self.events.append("stage")
        return node["destination"]

    def guard(self):
        self.assertIn("stage", self.events)
        self.events.append("guard")

    def launch(self, node, runtime, item):
        self.assertIn("guard", self.events)
        self.events.append("launch")
        return 0

    def execute(self, *, launch=None, stage=None, verify=None, clean=True, guard=None):
        with (
            patch.object(
                backend, "capture_nodes", return_value=[copy.deepcopy(self.node)]
            ),
            patch.object(backend, "scan_idle"),
            patch.object(backend, "stage_node", side_effect=stage or self.stage),
            patch.object(
                backend, "launch_node", side_effect=launch or self.launch
            ) as launched,
            patch.object(
                backend,
                "verify_case_node",
                side_effect=verify or (lambda *args: {"scope": "CPU fixture"}),
            ),
            patch.object(backend, "cleanup_captured", return_value=clean) as cleanup,
        ):
            result = backend.execute(
                stored=self.stored,
                item=self.item,
                case=self.case,
                execution_id="execution-1",
                pools=self.pools,
                root=self.root,
                cancel=self.cancel,
                capture=self.capture,
                before_launch=guard or self.guard,
                phase=lambda value: self.events.append(value),
            )
        return result, launched, cleanup

    def test_static_bundle_validation_never_executes_runner_or_source(self):
        directory = self.root / "validation"
        directory.mkdir()
        self.assertEqual(
            backend.bundle_profile(self.stored, self.item, directory), self.profile
        )

    def test_mutated_source_or_runner_refuses_before_any_remote_access(self):
        for kind in ("source", "runner"):
            path = (
                Path(self.handle.bundle_path) if kind == "source" else self.runner_path
            )
            old = path.read_bytes()
            path.write_bytes(old + b"mutation")
            with patch.object(backend, "transport_run") as network:
                directory = self.root / kind
                directory.mkdir()
                with self.assertRaises(ValueError):
                    backend.bundle_profile(self.stored, self.item, directory)
                network.assert_not_called()
            path.write_bytes(old)

    def test_runner_catalog_mismatch_and_extra_files_refuse(self):
        cases = [
            {**self.runner_files, "extra.py": b"pass\n"},
            {
                **self.runner_files,
                "catalog.json": canonical(
                    {
                        **self.catalog,
                        "cases": {
                            self.item["test_id"]: {
                                **self.catalog["cases"][self.item["test_id"]],
                                "docker": "other",
                            }
                        },
                    }
                ),
            },
        ]
        for index, files in enumerate(cases):
            self.set_runner(files)
            directory = self.root / str(index)
            directory.mkdir()
            with self.assertRaises(ValueError):
                backend.bundle_profile(self.stored, self.item, directory)

    def test_capture_precedes_staging_guard_precedes_launch_and_clean_before_success(
        self,
    ):
        outcome, launched, cleanup = self.execute()
        self.assertEqual(outcome.exit_code, 0)
        self.assertTrue(outcome.release_verified)
        self.assertLess(self.events.index("capture"), self.events.index("stage"))
        self.assertLess(self.events.index("guard"), self.events.index("launch"))
        launched.assert_called_once()
        self.assertFalse(cleanup.call_args.kwargs["stop"])
        self.assertEqual(self.captured["source_sha"], self.handle.tested_merge_sha)
        self.assertEqual(self.captured["tree_sha"], self.handle.tested_tree_sha)

    def test_staging_failure_cannot_launch_or_call_a_source_fallback(self):
        outcome, launched, cleanup = self.execute(
            stage=Mock(side_effect=RuntimeError("network failed"))
        )
        self.assertNotEqual(outcome.exit_code, 0)
        launched.assert_not_called()
        cleanup.assert_not_called()
        self.assertTrue(outcome.release_verified)  # no process was started

    def test_revocation_after_all_node_staging_prevents_launch(self):
        outcome, launched, _ = self.execute(
            guard=Mock(side_effect=InterruptedError("revoked"))
        )
        self.assertEqual(outcome.exit_code, 130)
        launched.assert_not_called()

    def test_failed_payload_has_failure_exit_and_owned_cleanup(self):
        outcome, _, cleanup = self.execute(launch=lambda *args: 7)
        self.assertEqual(outcome.exit_code, 7)
        self.assertTrue(outcome.release_verified)
        self.assertTrue(cleanup.call_args.kwargs["stop"])

    def test_wrong_execution_evidence_cannot_pass(self):
        outcome, _, cleanup = self.execute(
            verify=Mock(side_effect=ValueError("wrong source"))
        )
        self.assertNotEqual(outcome.exit_code, 0)
        cleanup.assert_called()

    def test_unverified_cleanup_overrides_zero_process_exit(self):
        outcome, _, _ = self.execute(clean=False)
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.release_verified)

    def test_running_cancellation_stops_before_waiting_for_ssh(self):
        def launch(*args):
            self.cancel.set()
            time.sleep(0.03)
            return 0

        outcome, _, cleanup = self.execute(launch=launch)
        self.assertEqual(outcome.exit_code, 130)
        self.assertTrue(cleanup.call_args.kwargs["stop"])

    def test_idle_scan_failure_has_no_lease_capture_or_process(self):
        capture = Mock()
        with (
            patch.object(backend, "capture_nodes", return_value=[self.node]),
            patch.object(
                backend, "scan_idle", side_effect=ExternalNpuOccupancy("busy")
            ) as scan,
            patch.object(backend, "stage_node") as stage,
        ):
            with self.assertRaises(ExternalNpuOccupancy):
                backend.execute(
                    stored=self.stored,
                    item=self.item,
                    case=self.case,
                    execution_id="execution-1",
                    pools=self.pools,
                    root=self.root,
                    cancel=self.cancel,
                    capture=capture,
                    before_launch=self.guard,
                    phase=lambda value: None,
                )
            self.assertEqual(
                scan.call_args.kwargs["audit_identity"],
                ("execution-1", self.item["case_id"]),
            )
            capture.assert_not_called()
            stage.assert_not_called()

    def runtime(self):
        self.node.update(
            destination="/mnt/share/ci_tests/gitcode-owned/node0",
            task_marker="captured-marker",
        )
        return {
            "deadline": int(time.time()) + 300,
            "lease_id": "lease-fixture",
            "fencing_token": 42,
            "source_sha": "c" * 40,
            "tree_sha": "d" * 40,
            "ports": {field: self.case[field] for field in backend.PORT_FIELDS},
            "cleanup": {
                "nodes": [self.node],
                "ascend_set_env_path": "/trusted/set_env.sh",
                "process_indexing": "card-die",
            },
        }

    def test_runner_command_uses_pinned_interpreter_clean_env_and_separate_dirs(self):
        with patch.dict(
            "os.environ",
            GH_TOKEN="must-not-appear",
            GITCODE_API_TOKEN="also-must-not-appear",
        ):
            command = backend.runner_command(
                self.node, self.runtime(), self.item, "launch"
            )
        self.assertIn("env -i", command)
        self.assertIn("/opt/python/bin/python3 -I", command)
        self.assertIn("/runner/runner.py", command)
        self.assertIn("--source-root", command)
        self.assertIn("--lease-id", command)
        self.assertIn("--fencing-token 42", command)
        self.assertNotIn("must-not-appear", command)
        self.assertNotIn("github.com", command)
        self.assertNotIn("gitcode.com", command)

    def test_isolated_python_uses_only_captured_interpreter_library_directory(self):
        node = {**self.node, "python": "/opt/python/3.12/bin/python3"}
        prefix = backend.isolated_python_environment(node)
        self.assertIn("env -i", prefix)
        self.assertIn("LD_LIBRARY_PATH=/opt/python/3.12/lib", prefix)
        for value in ("python3", "/opt/../tmp/bin/python3", "/opt/bin/python3:/evil"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backend.isolated_python_environment({**node, "python": value})

    def test_cleanup_stage_timing_uses_only_safe_run_and_case_ids(self):
        runtime = self.runtime()
        with (
            patch.object(backend, "verify_node"),
            patch.object(backend, "request_stop"),
            patch.object(backend, "owned_processes_gone"),
            patch.object(backend, "scan_idle"),
            patch("ci_core.gitcode_timing.emit_stage") as emit,
        ):
            self.assertTrue(
                backend.cleanup_captured(
                    runtime,
                    stop=True,
                    stream=io.StringIO(),
                    run_id=100,
                    case_id=self.item["case_id"],
                )
            )
        emit.assert_called_once()
        args, kwargs = emit.call_args
        self.assertEqual(args[0], "cleanup")
        self.assertEqual(kwargs["run_id"], 100)
        self.assertEqual(kwargs["case_id"], self.item["case_id"])

    def test_rebuilt_container_is_never_cleaned_by_name(self):
        runtime = self.runtime()
        response = SimpleNamespace(
            stdout=json.dumps(
                {
                    "Id": "f" * 64,
                    "Name": "/" + backend.DOCKER,
                    "Image": self.node["image_id"],
                    "State": {"Running": True},
                }
            )
        )
        with (
            patch.object(backend, "transport_run", return_value=response) as transport,
            patch.object(backend, "stop_owned") as stop,
        ):
            self.assertFalse(
                backend.cleanup_captured(runtime, stop=True, stream=io.StringIO())
            )
            stop.assert_not_called()
            self.assertTrue(
                all(
                    self.node["container_id"] in call.args[1]
                    for call in transport.call_args_list
                )
            )

    def test_remote_evidence_checks_source_lease_rank_and_exit_types(self):
        runtime = self.runtime()
        evidence = {
            "schema": 1,
            "test_id": self.item["test_id"],
            "status": "PASS",
            "exit_code": 0,
            "source_sha": "c" * 40,
            "tree_sha": "d" * 40,
            "lease_id": runtime["lease_id"],
            "fencing_token": 42,
            "task_marker": "captured-marker",
            "node_rank": 0,
            "world_size": 8,
            "all_reduce_sum": 28,
        }
        for key, value in [
            ("source_sha", "e" * 40),
            ("lease_id", "other"),
            ("node_rank", 1),
            ("exit_code", False),
            ("fencing_token", 43),
            ("all_reduce_sum", 27),
        ]:
            response = SimpleNamespace(stdout=json.dumps({**evidence, key: value}))
            with (
                patch.object(backend, "verify_node"),
                patch.object(backend, "transport_run", return_value=response),
            ):
                with (
                    self.subTest(key=key),
                    self.assertRaisesRegex(ValueError, "MISMATCH"),
                ):
                    backend.verify_case_node(self.node, runtime, self.item)

    def test_remote_timeout_is_bounded_by_request_deadline_and_never_zero(self):
        runtime = self.runtime()
        runtime["deadline"] = 1005
        with patch.object(backend.time, "time", return_value=1000):
            self.assertIn(
                "--kill-after=30s 5s",
                backend.runner_command(self.node, runtime, self.item, "launch"),
            )
        with patch.object(backend.time, "time", return_value=1005):
            with self.assertRaises(TimeoutError):
                backend.runner_command(self.node, runtime, self.item, "launch")

    def test_backend_carries_deadline_through_actual_command_builder(self):
        commands = []

        def transport(host, command, **kwargs):
            commands.append(command)
            output = ""
            if "case-evidence.json" in command and "sys.stdout.write" in command:
                node = self.captured["cleanup"]["nodes"][0]
                output = json.dumps(
                    {
                        "schema": 1,
                        "test_id": self.item["test_id"],
                        "status": "PASS",
                        "exit_code": 0,
                        "source_sha": self.captured["source_sha"],
                        "tree_sha": self.captured["tree_sha"],
                        "lease_id": self.captured["lease_id"],
                        "fencing_token": 42,
                        "task_marker": node["task_marker"],
                        "node_rank": 0,
                        "world_size": 8,
                        "all_reduce_sum": 28,
                    }
                )
            return SimpleNamespace(stdout=output, returncode=0)

        with (
            patch.object(
                backend, "capture_nodes", return_value=[copy.deepcopy(self.node)]
            ),
            patch.object(backend, "scan_idle"),
            patch.object(backend, "verify_node"),
            patch.object(backend, "transport_run", side_effect=transport),
            patch.object(backend, "owned_processes_gone"),
        ):
            result = backend.execute(
                stored=self.stored,
                item=self.item,
                case=self.case,
                execution_id="execution-1",
                pools=self.pools,
                root=self.root,
                cancel=self.cancel,
                capture=self.capture,
                before_launch=lambda: None,
                phase=lambda value: None,
            )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.release_verified)
        self.assertEqual(self.captured["deadline"], self.plan["deadline"])
        supervisors = [command for command in commands if "subprocess.Popen" in command]
        self.assertEqual(len(supervisors), 1)
        self.assertTrue(supervisors[0].startswith("env -i "))
        self.assertIn("--fencing-token 42", supervisors[0])
        self.assertTrue(any("sha256sum" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
