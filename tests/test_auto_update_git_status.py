"""Guard against sudo git status refreshing user-owned checkout indexes."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update.preflight import _run_git

ROOT = Path(__file__).resolve().parents[1]


class GitStatusOwnershipTests(unittest.TestCase):
    def test_preflight_git_status_disables_optional_locks(self):
        with patch("auto_update.preflight.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr=""
            )
            _run_git(Path("/tmp/fixture-repo"), ["status", "--porcelain"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_installer_status_wrapper_disables_optional_locks(self):
        installer = (ROOT / "scripts" / "install_auto_update.sh").read_text()
        wrapper = installer.split("git_in_source() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("GIT_OPTIONAL_LOCKS=0", wrapper)
        self.assertIn(
            "git_in_source status --porcelain --untracked-files=all", installer
        )

    @unittest.skipUnless(
        os.geteuid() == 0 and shutil.which("chown"),
        "cross-owner index regression requires root and chown",
    )
    def test_preflight_status_preserves_foreign_owned_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "user-worktree"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            tracked = repo / "tracked.txt"
            tracked.write_text("unchanged\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "fixture")
            index = repo / ".git" / "index"
            os.chown(repo, 65534, 65534)
            for current, dirs, files in os.walk(repo):
                os.chown(current, 65534, 65534)
                for name in dirs:
                    os.chown(Path(current) / name, 65534, 65534)
                for name in files:
                    os.chown(Path(current) / name, 65534, 65534)
            os.utime(tracked, None)
            before = index.stat()
            self.assertEqual(before.st_uid, 65534)

            self.assertEqual(
                _run_git(repo, ["status", "--porcelain", "--untracked-files=all"]),
                "",
            )

            after = index.stat()
            self.assertEqual(after.st_uid, 65534)
            self.assertEqual(after.st_gid, 65534)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
