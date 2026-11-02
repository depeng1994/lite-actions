"""CPU tests use synthetic credentials only; not real App capability evidence."""

import base64
import copy
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from ci_core import gitcode_app as app

REPO_ID = 1210038933
REPO = "cann-framework/torchtitan-npu"


class AppTokenTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.installation = {
            "id": 91,
            "app_id": 42,
            "suspended_at": None,
            "permissions": {"checks": "write"},
        }
        self.reply = {
            "token": "synthetic-installation-token",
            "expires_at": dt.datetime.fromtimestamp(
                self.now + 3600, dt.UTC
            ).isoformat(),
            "permissions": {"checks": "write", "metadata": "read"},
            "repositories": [{"id": REPO_ID, "full_name": REPO}],
        }
        self.calls = []
        self.signer = Mock(return_value=b"synthetic-signature")
        self.tokens = app.InstallationTokens(
            app_id=42,
            installation_id=91,
            repo_id=REPO_ID,
            signer=self.signer,
            transport=self.transport,
            clock=lambda: self.now,
        )

    def transport(self, method, path, bearer, payload=None):
        self.calls.append((method, path, bearer, payload))
        return copy.deepcopy(self.installation if method == "GET" else self.reply)

    def test_scoped_jwt_and_token_request(self):
        self.assertEqual(self.tokens(), self.reply["token"])
        self.assertEqual(len(self.calls), 2)
        jwt = self.calls[0][2]
        header, claims, _ = jwt.split(".")

        def decode(s):
            return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))

        self.assertEqual(decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode(claims), {"iss": "42", "iat": self.now - 60, "exp": self.now + 540}
        )
        self.assertEqual(
            self.calls[1][3],
            {"repository_ids": [REPO_ID], "permissions": {"checks": "write"}},
        )
        self.assertEqual(self.calls[1][1], "/app/installations/91/access_tokens")
        self.assertNotIn(self.reply["token"], repr(self.tokens))
        self.assertNotIn(jwt, repr(self.tokens))

    def test_shared_threads_mint_only_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.tokens(), range(16)))
        self.assertEqual(values, [self.reply["token"]] * 16)
        self.assertEqual(len(self.calls), 2)

    def test_refresh_before_expiration(self):
        self.tokens()
        self.now += 3539
        self.tokens()
        self.assertEqual(len(self.calls), 2)
        self.now += 1
        self.reply["token"] = "synthetic-refreshed-token"
        self.reply["expires_at"] = dt.datetime.fromtimestamp(
            self.now + 3600, dt.UTC
        ).isoformat()
        self.assertEqual(self.tokens(), "synthetic-refreshed-token")
        self.assertEqual(len(self.calls), 4)

    def test_expired_credential_is_not_reused_after_refresh_failure(self):
        self.tokens()
        self.now += 3600
        self.tokens.transport = Mock(side_effect=app.AppError("unavailable"))
        with self.assertRaises(app.AppError):
            self.tokens()

    def test_invalid_app_or_installation_never_mints_token(self):
        for changes in (
            {"id": 92},
            {"app_id": 43},
            {"id": True},
            {"suspended_at": "2026-01-01"},
            {"permissions": {"checks": "read"}},
        ):
            with self.subTest(changes=changes):
                self.tokens.invalidate()
                self.calls.clear()
                self.installation = {
                    "id": 91,
                    "app_id": 42,
                    "suspended_at": None,
                    "permissions": {"checks": "write"},
                    **changes,
                }
                with self.assertRaisesRegex(app.AppError, "INSTALLATION"):
                    self.tokens()
                self.assertEqual([c[0] for c in self.calls], ["GET"])

    def test_repositories_must_be_exact_single_stable_identity(self):
        for value in (
            None,
            [],
            [{"id": REPO_ID}],
            [{"id": True, "full_name": REPO}],
            [{"id": REPO_ID, "full_name": "intruder/model"}],
            [{"id": REPO_ID, "full_name": REPO}] * 2,
        ):
            with self.subTest(value=value):
                self.tokens.invalidate()
                self.reply["repositories"] = value
                with self.assertRaisesRegex(app.AppError, "REPOSITORY_SCOPE"):
                    self.tokens()

    def test_overprivileged_or_readonly_token_is_rejected(self):
        for value in (
            {"checks": "read"},
            {"checks": "write", "contents": "write"},
            {"checks": "write", "actions": "read"},
            {"checks": "write", "metadata": "write"},
            None,
        ):
            with self.subTest(value=value):
                self.tokens.invalidate()
                self.reply["permissions"] = value
                with self.assertRaisesRegex(app.AppError, "PERMISSION_SCOPE"):
                    self.tokens()

    def test_expiry_must_be_bounded_timezone_aware_and_fresh(self):
        for value in (
            None,
            "invalid",
            "2027-01-01T00:00:00",
            True,
            dt.datetime.fromtimestamp(self.now + 50, dt.UTC).isoformat(),
            dt.datetime.fromtimestamp(self.now + 7200, dt.UTC).isoformat(),
        ):
            with self.subTest(value=value):
                self.tokens.invalidate()
                self.reply["expires_at"] = value
                with self.assertRaisesRegex(app.AppError, "EXPIRY"):
                    self.tokens()

    def test_no_fixed_40_character_token_assumption(self):
        self.reply["token"] = "ghs_APPID_" + "x" * 3000
        self.assertEqual(self.tokens(), self.reply["token"])

    def test_malformed_token_cannot_become_an_http_header(self):
        for value in ("", "abc\nInjected: value", "x" * 16385, None, True):
            with self.subTest(value=value):
                self.tokens.invalidate()
                self.reply["token"] = value
                with self.assertRaisesRegex(app.AppError, "INVALID_APP_TOKEN"):
                    self.tokens()

    def test_unknown_post_outcome_is_never_automatically_retried(self):
        self.tokens.transport = Mock(side_effect=[self.installation, OSError("lost")])
        with self.assertRaises(OSError):
            self.tokens()
        self.assertEqual(self.tokens.transport.call_count, 2)

    def test_app_endpoints_cannot_be_redirected_to_other_services(self):
        for method, path in (
            ("GET", "https://intruder.invalid"),
            ("POST", "/repos/a/b"),
            ("DELETE", "/app/installations/91"),
        ):
            with (
                self.subTest(path=path),
                patch.object(app.urllib.request, "build_opener") as opener,
            ):
                with self.assertRaisesRegex(app.AppError, "ENDPOINT"):
                    app.app_request(method, path, "synthetic")
                opener.assert_not_called()

    def test_checks_client_only_performs_exact_check_operations(self):
        client = app.ChecksAppClient(self.tokens)
        with patch.object(app, "GitHubClient") as factory:
            factory.return_value.request.return_value = {"id": 123}
            for method, path in (
                ("POST", "/check-runs"),
                ("PATCH", "/check-runs/123"),
                ("GET", "/check-runs/123"),
                ("GET", "/commits/" + "a" * 40 + "/check-runs?filter=all"),
            ):
                self.assertEqual(client.request(method, path), {"id": 123})
            for method, path in (
                ("POST", "/actions/workflows/1/dispatches"),
                ("DELETE", "/check-runs/123"),
                ("PATCH", "/git/refs/heads/master"),
                ("GET", "//intruder.invalid"),
                ("POST", "/check-runs?override=1"),
            ):
                with self.assertRaisesRegex(app.AppError, "ENDPOINT"):
                    client.request(method, path)
            self.assertEqual(factory.call_count, 4)
        self.assertNotIn(self.reply["token"], repr(client))

    def test_unauthorized_response_invalidates_without_replaying_write(self):
        client = app.ChecksAppClient(self.tokens)
        error = urllib.error.HTTPError(
            "https://api.github.com", 401, "secret-should-not-leak", {}, None
        )
        with patch.object(app, "GitHubClient") as factory:
            factory.return_value.request.side_effect = error
            with self.assertRaisesRegex(
                app.AppError, "CHECKS_HTTP_ERROR_401"
            ) as raised:
                client.request("POST", "/check-runs", {"name": "case"})
            self.assertEqual(factory.return_value.request.call_count, 1)
            self.assertIsNone(self.tokens._cached)
            self.assertNotIn("secret-should-not-leak", str(raised.exception))

    def test_unknown_checks_write_reaches_journal_recovery_without_retry(self):
        client = app.ChecksAppClient(self.tokens)
        with patch.object(app, "GitHubClient") as factory:
            factory.return_value.request.side_effect = TimeoutError("synthetic secret")
            with self.assertRaises(OSError) as raised:
                client.request("POST", "/check-runs", {"name": "case"})
            self.assertEqual(factory.return_value.request.call_count, 1)
            self.assertNotIn("synthetic secret", str(raised.exception))


class KeyFileTests(unittest.TestCase):
    def test_key_path_and_file_permissions_are_checked_before_signing(self):
        with self.assertRaisesRegex(app.AppError, "ABSOLUTE"):
            app.OpenSSLSigner(Path("relative.pem"))
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key.pem"
            key.write_text("synthetic-not-a-real-key")
            key.chmod(0o644)
            with patch.object(app.subprocess, "run") as run:
                with self.assertRaisesRegex(app.AppError, "OWNER_ONLY"):
                    app.OpenSSLSigner(key)(b"message")
                run.assert_not_called()
            key.chmod(0o600)
            link = key.with_name("link.pem")
            link.symlink_to(key)
            with self.assertRaisesRegex(app.AppError, "SYMLINK"):
                app.OpenSSLSigner(link)(b"message")
            self.assertNotIn(str(key), repr(app.OpenSSLSigner(key)))

    @unittest.skipUnless(Path("/usr/bin/openssl").is_file(), "OpenSSL not installed")
    def test_synthetic_rsa_signature_is_verifiable_and_has_no_secret_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key, public, signature = (
                root / "key.pem",
                root / "public.pem",
                root / "sig.bin",
            )
            subprocess.run(
                [
                    "/usr/bin/openssl",
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    str(key),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            key.chmod(0o600)
            subprocess.run(
                [
                    "/usr/bin/openssl",
                    "pkey",
                    "-in",
                    str(key),
                    "-pubout",
                    "-out",
                    str(public),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            real_run = subprocess.run
            with (
                patch.dict(os.environ, SYNTHETIC_PLATFORM_SECRET="not-a-real-secret"),
                patch.object(app.subprocess, "run", wraps=real_run) as invoked,
            ):
                signature.write_bytes(app.OpenSSLSigner(key)(b"synthetic-jwt-payload"))
                self.assertNotIn(
                    "SYNTHETIC_PLATFORM_SECRET", invoked.call_args.kwargs["env"]
                )
                self.assertNotIn("shell", invoked.call_args.kwargs)
            verified = subprocess.run(
                [
                    "/usr/bin/openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public),
                    "-signature",
                    str(signature),
                ],
                input=b"synthetic-jwt-payload",
                capture_output=True,
            )
            self.assertEqual(verified.returncode, 0)


if __name__ == "__main__":
    unittest.main()
