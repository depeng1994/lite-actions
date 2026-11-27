"""Regression coverage for diagnostic state hidden by private temp files."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ci_core.control_client import write_json
from ci_core.worker_ready import publish_ready


@unittest.skipUnless(
    os.name == "posix" and Path("/proc/self/stat").exists(),
    "Linux file permissions required",
)
class DiagnosticPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.writers = (
            ("worker-ready.json", self.write_ready),
            ("control-outbox.json", self.write_control),
        )

    def write_ready(self, path):
        return publish_ready(self.root, path, release_sha="a" * 40)

    @staticmethod
    def write_control(path):
        value = {"events": [{"phase": "QUEUED"}], "generation": 1}
        write_json(path, value)
        return value

    def test_new_diagnostic_state_is_readable(self):
        for filename, writer in self.writers:
            with self.subTest(filename=filename):
                path = self.root / filename.removesuffix(".json") / filename
                expected = writer(path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o755)
                self.assertEqual(json.loads(path.read_text()), expected)

    def test_private_umask_and_replacement_do_not_hide_state(self):
        os.umask(0o077)
        for filename, writer in self.writers:
            for existing in (False, True):
                with self.subTest(filename=filename, existing=existing):
                    path = self.root / f"{filename}.{existing}"
                    if existing:
                        path.write_text("{}")
                        os.chmod(path, 0o600)
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    expected = writer(path)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                    self.assertEqual(json.loads(path.read_text()), expected)
