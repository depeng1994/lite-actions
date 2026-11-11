"""RFC #19 AU-01..03: fixed SHA, no-change and failure safety."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update import check_source
from auto_update.update import Settings, _run


def make_settings(root: Path) -> Settings:
    return Settings(
        root=root,
        source="https://github.com/depeng1994/lite-actions.git",
        ref="refs/heads/main",
        unit="example-worker.service",
        state_dir=root / "runtime",
        ready_file=root / "ready.json",
        log_file=root / "management.jsonl",
        lock_file=root / "update.lock",
        ready_timeout=120,
    )


class SourceTests(unittest.TestCase):
    def test_au01_same_sha_does_not_stop_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = "a" * 40
            with (
                patch("auto_update.update.current_release", return_value=(root, a)),
                patch("auto_update.update.check_source", return_value=a),
                patch("auto_update.update.stop_worker") as stop,
                patch("auto_update.update._audit") as log,
            ):
                self.assertEqual(_run(make_settings(root)), 0)
                stop.assert_not_called()
                self.assertEqual(log.call_args.args[1], "NO_CHANGE")

    def test_au02_exact_fast_forward_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "origin"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test")
            git("checkout", "-qb", "main")
            (repo / "version.txt").write_text("A\n")
            git("add", ".")
            git("commit", "-qm", "A")
            a = git("rev-parse", "HEAD")
            (repo / "version.txt").write_text("B\n")
            git("commit", "-qam", "B")
            b = git("rev-parse", "HEAD")
            with patch("auto_update.check_source.validate_source"):
                stage = check_source.stage_source(
                    root, str(repo), "refs/heads/main", b, a
                )
            self.assertEqual(check_source.checkout_sha(stage), b)
            self.assertEqual((stage / "version.txt").read_text(), "B\n")
            self.assertFalse((root / "current").exists())

    def test_au03_unavailable_or_untrusted_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = "a" * 40
            with (
                patch("auto_update.update.current_release", return_value=(root, a)),
                patch(
                    "auto_update.update.check_source",
                    side_effect=check_source.SourceError("source_unavailable"),
                ),
                patch("auto_update.update.stop_worker") as stop,
            ):
                self.assertEqual(_run(make_settings(root)), 2)
                stop.assert_not_called()
                events = (root / "management.jsonl").read_text()
                self.assertIn('"FAILED_CHECK"', events)
            for url in ("file:///etc/passwd", "https://user:secret@github.com/x/y.git"):
                with self.subTest(url=url), self.assertRaises(check_source.SourceError):
                    check_source.validate_source(url, "refs/heads/main")


if __name__ == "__main__":
    unittest.main()
