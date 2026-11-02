"""Static interface checks do not execute source code or import model packages."""

import tempfile
import unittest
from pathlib import Path

from ci_core.gitcode_interfaces import (
    SMOKE_PAYLOAD,
    SourceInterfaceError,
    inspect_directory,
    required_paths,
)
from tests.test_gitcode_pipeline import HostingAPI


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def write(self, name, value):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def test_payload_is_ast_parsed_not_imported(self):
        self.write(
            SMOKE_PAYLOAD,
            "raise RuntimeError('control must not run proposed Python')\nPAYLOAD_API_VERSION = 1\ndef all_reduce(tensor): pass\n",
        )
        inspect_directory(self.source, ["dispatch_smoke_8p", "dispatch_smoke_16p"])

    def test_missing_wrong_duplicate_or_boolean_interface_version_is_rejected(self):
        for source in (
            "PAYLOAD_API_VERSION = True\ndef all_reduce(t): pass\n",
            "PAYLOAD_API_VERSION = 2\ndef all_reduce(t): pass\n",
            "PAYLOAD_API_VERSION = 1\n",
            "PAYLOAD_API_VERSION = 1\nPAYLOAD_API_VERSION = 1\ndef all_reduce(t): pass\n",
            "PAYLOAD_API_VERSION = 1\ndef all_reduce(t): pass\ndef all_reduce(t): pass\n",
            "PAYLOAD_API_VERSION = 1\nasync def all_reduce(t): pass\n",
        ):
            self.write(SMOKE_PAYLOAD, source)
            with self.subTest(source=source), self.assertRaises(SourceInterfaceError):
                inspect_directory(self.source, ["dispatch_smoke_8p"])

    def test_os_has_no_model_payload_requirement(self):
        inspect_directory(self.source, ["worker_os_blue"])
        self.assertEqual(required_paths(["worker_os_blue"]), set())

    def test_reserved_control_bootstrap_names_are_not_allowed(self):
        for name in ("sitecustomize.py", "usercustomize.py", "_lite_gitcode_train.py"):
            file = self.write(name, "raise RuntimeError('must not execute')")
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(SourceInterfaceError, "SHADOWS"),
            ):
                inspect_directory(self.source, ["worker_os_blue"])
            file.unlink()

    def test_training_paths_are_data_not_python_discovery(self):
        tests = [
            "dsv4_flash_a3_8p_example",
            "dsv4_flash_a3_8p_adamw",
            "dsv4_flash_a3_16p_example",
        ]
        paths = required_paths(tests)
        self.assertIn("scripts/run_train_multinodes.sh", paths)
        for path in paths:
            self.write(path, "this text is never executed or imported by preflight\n")
        inspect_directory(self.source, tests)
        (self.source / "torchtitan_npu/train.py").unlink()
        with self.assertRaisesRegex(SourceInterfaceError, "MISSING_OR_UNSAFE"):
            inspect_directory(self.source, tests)

    def test_foreign_case_and_traversal_are_not_accepted(self):
        for tests in (
            ["dispatch_smoke_a5_8p"],
            ["../../outside"],
            ["worker_os_blue", "worker_os_blue"],
            [],
        ):
            with self.subTest(tests=tests), self.assertRaises(SourceInterfaceError):
                required_paths(tests)

    def test_symlink_and_oversized_payload_fail_closed(self):
        payload = self.write(SMOKE_PAYLOAD, "# x" * 30000)
        with self.assertRaisesRegex(SourceInterfaceError, "SIZE_LIMIT"):
            inspect_directory(self.source, ["dispatch_smoke_8p"])
        outside = self.root / "outside.py"
        payload.rename(outside)
        payload.symlink_to(outside)
        with self.assertRaisesRegex(SourceInterfaceError, "MISSING_OR_UNSAFE"):
            inspect_directory(self.source, ["dispatch_smoke_8p"])

    def test_producer_missing_payload_never_uploads_executable_request(self):
        from tests.test_gitcode_artifacts import tar_bytes

        (self.root / "hosting").mkdir()
        api = HostingAPI(self.root / "hosting")
        api.source_bytes = tar_bytes({"README.md": b"no smoke payload"})
        with self.assertRaisesRegex(SourceInterfaceError, "MISSING_OR_UNSAFE"):
            api.produce()
        self.assertEqual(api.uploads, ["dispatch-intent-100-1"])


if __name__ == "__main__":
    unittest.main()
