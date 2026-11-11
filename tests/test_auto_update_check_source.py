"""RFC #19 AU-01..03: fixed SHA, no-change and failure safety."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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


    def test_git_passes_only_proxy_and_ca_allowlist(self):
        fake = {
            "http_proxy": "http://proxy.invalid:8080",
            "https_proxy": "http://proxy.invalid:8080",
            "HTTP_PROXY": "http://upper-proxy.invalid:8080",
            "HTTPS_PROXY": "http://upper-proxy.invalid:8080",
            "no_proxy": "localhost,.internal",
            "NO_PROXY": "localhost,.internal",
            "SSL_CERT_FILE": "/etc/ssl/private/proxy-ca.pem",
            "CURL_CA_BUNDLE": "/etc/ssl/private/proxy-ca.pem",
            "GIT_SSL_CAINFO": "/etc/ssl/private/proxy-ca.pem",
            "LITE_ACTIONS_GITHUB_TOKEN": "must-never-reach-git",
            "LITE_ACTIONS_GITCODE_TOKEN": "must-also-never-reach-git",
            "UNRELATED_SECRET": "must-not-reach-git",
        }
        with (
            patch.dict(os.environ, fake, clear=True),
            patch(
                "auto_update.check_source.subprocess.run",
                return_value=SimpleNamespace(stdout="ok\n"),
            ) as run,
        ):
            self.assertEqual(
                check_source.git(["ls-remote", "https://github.com/o/r.git"]),
                "ok",
            )

        child_env = run.call_args.kwargs["env"]
        for name in check_source.GIT_ENV_PASSTHROUGH:
            self.assertEqual(child_env[name], fake[name])
        self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(child_env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(child_env["GIT_CONFIG_GLOBAL"], os.devnull)
        for name in (
            "LITE_ACTIONS_GITHUB_TOKEN",
            "LITE_ACTIONS_GITCODE_TOKEN",
            "UNRELATED_SECRET",
        ):
            self.assertNotIn(name, child_env)
        self.assertEqual(
            set(child_env),
            {
                "PATH",
                "HOME",
                "GIT_TERMINAL_PROMPT",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                *check_source.GIT_ENV_PASSTHROUGH,
            },
        )

    def test_git_failure_does_not_expose_proxy_or_token_values(self):
        proxy_secret = "proxy-user:proxy-password@proxy.invalid:8080"
        token_secret = "ghp-never-log-this"
        with (
            patch.dict(
                os.environ,
                {
                    "https_proxy": "http://" + proxy_secret,
                    "GIT_SSL_CAINFO": "/private/path/proxy-ca.pem",
                    "LITE_ACTIONS_GITHUB_TOKEN": token_secret,
                },
                clear=True,
            ),
            patch(
                "auto_update.check_source.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git", "ls-remote"], 35),
            ),
        ):
            with self.assertRaises(check_source.SourceError) as raised:
                check_source.git(["ls-remote", "https://github.com/o/r.git"])

        self.assertEqual(str(raised.exception), "git_command_failed")
        self.assertNotIn(proxy_secret, str(raised.exception))
        self.assertNotIn(token_secret, str(raised.exception))
        self.assertNotIn("proxy-ca.pem", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
