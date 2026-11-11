"""Deployment hardening tests for Python selection, preflight and proxy setup."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_update import preflight
from auto_update.check_source import SourceError
from auto_update.deploy_support import (
    DeploySupportError,
    clone_seed,
    install_managed_proxy_files,
    read_proxy_env_file,
    render_systemd_environment,
    validate_python_interpreter,
)

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "auto_update" / "bootstrap.sh"


def fake_python(path: Path, version: str, *, accepted: bool, log: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in *"print("* ) echo "' + version + '"; exit 0;; esac\n'
        "  " + ("exit 0" if accepted else "exit 1") + "\n"
        "fi\n"
        'if [ "$1" = "-u" ]; then echo "$0" >> "' + str(log) + '"; exit 0; fi\n'
        "exit 3\n"
    )
    path.chmod(0o755)
    return path


class PythonSelectionTests(unittest.TestCase):
    def test_python_interpreter_accepts_311_and_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = fake_python(
                Path(tmp) / "python312",
                "3.12.2",
                accepted=True,
                log=Path(tmp) / "called",
            )
            self.assertEqual(validate_python_interpreter(str(candidate)), "3.12.2")

    def test_python_interpreter_rejects_relative_missing_and_38(self):
        with self.assertRaisesRegex(DeploySupportError, "absolute"):
            validate_python_interpreter("python3.12")
        with self.assertRaises(DeploySupportError):
            validate_python_interpreter("/definitely/missing/python3.12")
        with tempfile.TemporaryDirectory() as tmp:
            candidate = fake_python(
                Path(tmp) / "python38",
                "3.8.10",
                accepted=False,
                log=Path(tmp) / "called",
            )
            with self.assertRaisesRegex(DeploySupportError, "3.8.10"):
                validate_python_interpreter(str(candidate))

    def _bootstrap_tree(self, tmp: Path) -> tuple[Path, Path]:
        root = tmp / "install"
        code = root / "releases" / ("a" * 40)
        (code / "auto_update").mkdir(parents=True)
        (code / "auto_update" / "update.py").write_text("# updater fixture\n")
        (root / "bootstrap").mkdir()
        (root / "current").symlink_to(code)
        return root, code

    def test_bootstrap_uses_explicit_interpreter_and_rejects_38(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            root, _ = self._bootstrap_tree(temp)
            log = temp / "selected.log"
            good = fake_python(temp / "python312", "3.12.2", accepted=True, log=log)
            env = os.environ.copy()
            env.update(
                {
                    "LITE_ACTIONS_UPDATE_ROOT": str(root),
                    "LITE_ACTIONS_UPDATE_PYTHON": str(good),
                    "PATH": "/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                ["bash", str(BOOTSTRAP), "--check"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().strip(), str(good))

            bad = fake_python(temp / "python38", "3.8.10", accepted=False, log=log)
            env["LITE_ACTIONS_UPDATE_PYTHON"] = str(bad)
            result = subprocess.run(
                ["bash", str(BOOTSTRAP), "--check"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("3.11+", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_bootstrap_falls_back_from_python38_to_python311(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            root, _ = self._bootstrap_tree(temp)
            log = temp / "selected.log"
            bindir = temp / "bin"
            bad = fake_python(bindir / "python3", "3.8.10", accepted=False, log=log)
            good = fake_python(bindir / "python3.11", "3.11.9", accepted=True, log=log)
            env = os.environ.copy()
            env.pop("LITE_ACTIONS_UPDATE_PYTHON", None)
            env.update(
                {
                    "LITE_ACTIONS_UPDATE_ROOT": str(root),
                    "PATH": str(bindir) + ":/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                ["bash", str(BOOTSTRAP), "--check"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().strip(), str(good))
            self.assertNotEqual(str(bad), log.read_text().strip())


class ProxyEnvironmentTests(unittest.TestCase):
    def _source(self, path: Path, text: str, mode: int = 0o600) -> Path:
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_proxy_file_accepts_only_allowlist_and_serializes_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self._source(
                Path(tmp) / "proxy.env",
                "https_proxy=http://proxy.invalid:8080\n"
                "NO_PROXY=localhost,.internal\n"
                "GIT_SSL_CAINFO=/etc/ssl/certs/proxy-ca.pem\n",
            )
            values = read_proxy_env_file(source, expected_uid=os.geteuid())
            rendered = render_systemd_environment(values)
            self.assertIn('https_proxy="http://proxy.invalid:8080"', rendered)
            self.assertIn('GIT_SSL_CAINFO="/etc/ssl/certs/proxy-ca.pem"', rendered)
            self.assertNotIn("TOKEN", rendered)

    def test_proxy_file_rejects_bad_mode_symlink_unknown_duplicate_and_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_mode = self._source(
                root / "bad-mode.env", "https_proxy=http://proxy\n", 0o644
            )
            with self.assertRaisesRegex(DeploySupportError, "0600"):
                read_proxy_env_file(bad_mode, expected_uid=os.geteuid())

            target = self._source(root / "target.env", "https_proxy=http://proxy\n")
            link = root / "link.env"
            link.symlink_to(target)
            with self.assertRaisesRegex(DeploySupportError, "non-symlink"):
                read_proxy_env_file(link, expected_uid=os.geteuid())

            for name, text in (
                ("unknown", "LITE_ACTIONS_GITHUB_TOKEN=secret\n"),
                ("duplicate", "https_proxy=http://one\nhttps_proxy=http://two\n"),
                ("crlf", "https_proxy=http://proxy\r\n"),
            ):
                path = self._source(root / (name + ".env"), text)
                with self.subTest(name=name), self.assertRaises(DeploySupportError):
                    read_proxy_env_file(path, expected_uid=os.geteuid())

    def test_proxy_file_owner_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._source(Path(tmp) / "proxy.env", "https_proxy=http://proxy\n")
            with self.assertRaisesRegex(DeploySupportError, "owned by root"):
                read_proxy_env_file(path, expected_uid=os.geteuid() + 1)

    def test_proxy_install_is_idempotent_and_refuses_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root / "source.env", "https_proxy=http://proxy-a\n")
            managed = root / "etc" / "auto-update-proxy.env"
            dropin = root / "systemd" / "20-proxy-env.conf"
            self.assertEqual(
                install_managed_proxy_files(
                    source, managed, dropin, expected_uid=os.geteuid()
                ),
                (True, True),
            )
            self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(dropin.stat().st_mode), 0o644)
            self.assertEqual(
                install_managed_proxy_files(
                    source, managed, dropin, expected_uid=os.geteuid()
                ),
                (False, False),
            )
            source.write_text("https_proxy=http://proxy-b\n")
            source.chmod(0o600)
            with self.assertRaisesRegex(DeploySupportError, "differs"):
                install_managed_proxy_files(
                    source, managed, dropin, expected_uid=os.geteuid()
                )
            self.assertIn("proxy-a", managed.read_text())
            self.assertNotIn("proxy-b", managed.read_text())


class SeedCloneTests(unittest.TestCase):
    @unittest.skipUnless(
        os.geteuid() == 0, "requires root to reproduce a user-owned worktree"
    )
    def test_seed_clone_from_non_root_owned_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "user-worktree"
            source.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(source), *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (source / "tracked.txt").write_text("seed\n")
            git("add", "tracked.txt")
            git("commit", "-qm", "fixture")
            sha = git("rev-parse", "HEAD")
            nobody = shutil.which("chown")
            if nobody:
                subprocess.run(["chown", "-R", "65534:65534", str(source)], check=True)
            seed = root / "seed"
            clone_seed(source, seed, sha)
            actual = subprocess.check_output(
                ["git", "-C", str(seed), "rev-parse", "HEAD"], text=True
            ).strip()
            self.assertEqual(actual, sha)
            self.assertEqual((seed / "tracked.txt").read_text(), "seed\n")


class PreflightTests(unittest.TestCase):
    def _layout(self, root: Path):
        sha = "a" * 40
        releases = root / "releases"
        release = releases / sha
        (release / ".git").mkdir(parents=True)
        for name in ("config", "logs", "cache"):
            (release / name).mkdir()
        (root / "current").symlink_to(release)
        source = root / "source"
        (source / ".git").mkdir(parents=True)
        return sha, release, source

    def _run(
        self,
        root: Path,
        *,
        unit="loaded",
        binding=True,
        dirty="",
        remote_ok=True,
        valid_current=True,
        python_ok=True,
        git_available=True,
        systemd_ok=True,
        working_dir_ok=True,
        proxy_file=None,
        git_calls=None,
    ):
        sha, release, source = self._layout(root)
        if not binding:
            shutil.rmtree(release / "cache")
        if not valid_current:
            (root / "current").unlink()
            (root / "current").write_text("not a link")

        def fake_git(path, args, extra_safe=()):
            if git_calls is not None:
                git_calls.append((path, list(args)))
            if args[0] == "rev-parse":
                return sha
            if "status" in args:
                return dirty
            raise AssertionError(args)

        with (
            patch(
                "auto_update.preflight.validate_python_interpreter",
                return_value="3.12.2" if python_ok else None,
                side_effect=None if python_ok else DeploySupportError("python too old"),
            ),
            patch("auto_update.preflight.systemd_available", return_value=systemd_ok),
            patch(
                "auto_update.preflight.shutil.which",
                return_value="/usr/bin/git" if git_available else None,
            ),
            patch(
                "auto_update.preflight._systemctl_show",
                side_effect=lambda _unit, prop: (
                    unit
                    if prop == "LoadState"
                    else (
                        str(release) if working_dir_ok else "/wrong/working-directory"
                    )
                ),
            ),
            patch(
                "auto_update.preflight.read_proxy_env_file",
                side_effect=DeploySupportError("proxy file mode invalid")
                if proxy_file
                else None,
            ),
            patch("auto_update.preflight._run_git", side_effect=fake_git),
            patch(
                "auto_update.preflight.check_source",
                return_value=sha if remote_ok else None,
                side_effect=None if remote_ok else SourceError("source_unavailable"),
            ),
        ):
            checks = preflight.run_preflight(
                python_path="/usr/local/bin/python3.12",
                worker_unit="lite-actions-yellow.service",
                install_root=root,
                source_root=source,
                git_url="https://github.com/owner/repo.git",
                git_ref="refs/heads/main",
                proxy_env_file=proxy_file,
            )
        return checks

    def test_git_status_disables_optional_index_writes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), git_calls=calls)

        status_calls = [args for _, args in calls if "status" in args]
        self.assertEqual(
            status_calls,
            [["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"]],
        )

        installer = (ROOT / "scripts" / "install_auto_update.sh").read_text()
        self.assertIn(
            'git_in_source --no-optional-locks status --porcelain --untracked-files=all',
            installer,
        )

    def test_preflight_all_core_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp))
        self.assertTrue(all(check.ok for check in checks), checks)

    def test_preflight_reports_unit_binding_dirty_source_and_remote_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), unit="not-found")
        self.assertTrue(any(c.name == "Worker unit" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), binding=False)
        self.assertTrue(any(c.name == "cache binding" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), dirty=" M tracked.py")
        self.assertTrue(any(c.name == "source worktree" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), remote_ok=False)
        self.assertTrue(
            any(c.name == "GitHub HTTPS source" and not c.ok for c in checks)
        )
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), valid_current=False)
        self.assertTrue(any(c.name == "current release" and not c.ok for c in checks))

    def test_preflight_reports_python_git_systemd_workdir_and_proxy_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), python_ok=False)
        self.assertTrue(any(c.name == "Python" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), git_available=False)
        self.assertTrue(any(c.name == "Git" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), systemd_ok=False)
        self.assertTrue(any(c.name == "systemd" and not c.ok for c in checks))
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), working_dir_ok=False)
        self.assertTrue(
            any(c.name == "Worker WorkingDirectory" and not c.ok for c in checks)
        )
        with tempfile.TemporaryDirectory() as tmp:
            checks = self._run(Path(tmp), proxy_file="/tmp/fake-proxy.env")
        self.assertTrue(any(c.name == "proxy file" and not c.ok for c in checks))

    def test_no_proxy_option_does_not_install_proxy_files(self):
        installer = (ROOT / "scripts" / "install_auto_update.sh").read_text()
        block = installer.split("# Proxy support is opt-in.", 1)[1].split(
            "systemd-analyze verify", 1
        )[0]
        self.assertIn('if [[ -n "$proxy_env_file" ]]', block)
        self.assertIn("install-proxy", block)
        self.assertNotIn(
            "install-proxy", installer.split("# Proxy support is opt-in.", 1)[0]
        )

    def test_python_path_is_persisted_and_template_has_no_system_python_assumption(
        self,
    ):
        installer = (ROOT / "scripts" / "install_auto_update.sh").read_text()
        self.assertIn('LITE_ACTIONS_UPDATE_PYTHON="$python_path"', installer)
        self.assertIn(
            'git config --file "$safe_git_config" --add safe.directory "$old_path/.git"',
            installer,
        )
        self.assertIn('"$python_path" - "$ready_file"', installer)
        service = (ROOT / "deploy" / "lite-actions-blue.service").read_text()
        self.assertNotIn("/usr/bin/python3", service)
        self.assertIn("<PYTHON_3_11_OR_NEWER>", service)


if __name__ == "__main__":
    unittest.main()
