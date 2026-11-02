"""Dedicated schema-4 Checks App credentials, confined to the control process.

No PAT fallback, redirect following, credential logging or automatic POST retry.
The same reviewed App ID is required in Hosted and Blue; each mints its own
short-lived installation token restricted to one repository and Checks write.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ci_core.gitcode_contract import MODEL_REPOSITORIES, canonical, positive
from ci_core.github import GitHubClient

API_VERSION = "2026-03-10"
MAX_RESPONSE = 2 * 1024**2


class AppError(RuntimeError):
    pass


class AppUnavailable(OSError):
    """Sanitized retryable/unknown outcome; the owning journal decides recovery."""

    pass


def _b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _token(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16384
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise AppError("INVALID_APP_TOKEN")
    return value


class OpenSSLSigner:
    """Sign using an owner-only regular key file, never a shell or PR path."""

    def __init__(self, key_file: Path):
        self.path = Path(key_file)
        if not self.path.is_absolute():
            raise AppError("ABSOLUTE_APP_KEY_PATH_REQUIRED")

    def __repr__(self):
        return "OpenSSLSigner(<protected credential>)"

    def __call__(self, message: bytes) -> bytes:
        if self.path.resolve(strict=True) != self.path:
            raise AppError("APP_KEY_SYMLINK_FORBIDDEN")
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or not 1 <= info.st_size <= 65536
            ):
                raise AppError("APP_KEY_MUST_BE_OWNER_ONLY_REGULAR_FILE")
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/openssl",
                        "dgst",
                        "-sha256",
                        "-sign",
                        f"/proc/self/fd/{fd}",
                    ],
                    input=message,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=10,
                    pass_fds=(fd,),
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LC_ALL": "C",
                        "OPENSSL_CONF": "/dev/null",
                    },
                )
            except (OSError, subprocess.SubprocessError):
                raise AppError("APP_JWT_SIGNING_FAILED") from None
            if not 128 <= len(result.stdout) <= 1024:
                raise AppError("INVALID_RSA_SIGNATURE_SIZE")
            return result.stdout
        finally:
            os.close(fd)


def app_request(method, path, bearer, payload=None):
    """Only the App installation/token endpoints; bounded and non-redirecting."""
    if not (
        method == "GET"
        and re.fullmatch(r"/app/installations/[1-9][0-9]*", path)
        or method == "POST"
        and re.fullmatch(r"/app/installations/[1-9][0-9]*/access_tokens", path)
    ):
        raise AppError("APP_ENDPOINT_NOT_ALLOWED")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    headers = {
        "Authorization": "Bearer " + _token(bearer),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "lite-actions-checks-app",
    }
    data = canonical(payload) if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "https://api.github.com" + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=25
        ) as response:
            body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise AppError("APP_RESPONSE_LIMIT")
        result = json.loads(body)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise AppError(f"APP_HTTP_ERROR_{status}") from None
    except (OSError, TimeoutError, ValueError, UnicodeError):
        raise AppError("APP_REQUEST_FAILED") from None
    if not isinstance(result, dict):
        raise AppError("APP_RESPONSE_OBJECT_REQUIRED")
    return result


class InstallationTokens:
    """Thread-safe in-memory refresh; never stores a token in state or artifacts."""

    def __init__(
        self,
        *,
        app_id,
        installation_id,
        repo_id,
        signer,
        transport=app_request,
        clock=time.time,
    ):
        positive(app_id)
        positive(installation_id)
        if repo_id not in MODEL_REPOSITORIES or not callable(signer):
            raise AppError("INVALID_APP_CONFIGURATION")
        self.app_id, self.installation_id, self.repo_id = (
            app_id,
            installation_id,
            repo_id,
        )
        self.signer, self.transport, self.clock = signer, transport, clock
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0

    def __repr__(self):
        return f"InstallationTokens(app_id={self.app_id}, installation_id={self.installation_id}, repo_id={self.repo_id})"

    def invalidate(self):
        with self._lock:
            self._cached, self._expires = None, 0

    def __call__(self):
        with self._lock:
            now = int(self.clock())
            if self._cached is not None and now < self._expires - 60:
                return self._cached
            header = _b64(canonical({"alg": "RS256", "typ": "JWT"}))
            claims = _b64(
                canonical({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)})
            )
            message = header + b"." + claims
            jwt = (message + b"." + _b64(self.signer(message))).decode("ascii")
            path = f"/app/installations/{self.installation_id}"
            installation = self.transport("GET", path, jwt)
            if (
                type(installation.get("id")) is not int
                or installation["id"] != self.installation_id
                or type(installation.get("app_id")) is not int
                or installation["app_id"] != self.app_id
                or installation.get("suspended_at") is not None
                or installation.get("permissions", {}).get("checks") != "write"
            ):
                raise AppError("APP_INSTALLATION_IDENTITY_OR_PERMISSION_MISMATCH")
            result = self.transport(
                "POST",
                path + "/access_tokens",
                jwt,
                {
                    "repository_ids": [self.repo_id],
                    "permissions": {"checks": "write"},
                },
            )
            repositories = result.get("repositories")
            if (
                not isinstance(repositories, list)
                or len(repositories) != 1
                or type(repositories[0].get("id")) is not int
                or repositories[0]["id"] != self.repo_id
                or repositories[0].get("full_name")
                != MODEL_REPOSITORIES[self.repo_id][0]
            ):
                raise AppError("APP_TOKEN_REPOSITORY_SCOPE_MISMATCH")
            permissions = result.get("permissions")
            if (
                not isinstance(permissions, dict)
                or permissions.get("checks") != "write"
                or any(
                    key not in {"checks", "metadata"}
                    or key == "metadata"
                    and value != "read"
                    for key, value in permissions.items()
                )
            ):
                raise AppError("APP_TOKEN_PERMISSION_SCOPE_MISMATCH")
            try:
                expiry = dt.datetime.fromisoformat(
                    result["expires_at"].replace("Z", "+00:00")
                )
                if expiry.tzinfo is None:
                    raise ValueError("timezone required")
                expires = int(expiry.timestamp())
            except (KeyError, TypeError, AttributeError, ValueError):
                raise AppError("APP_TOKEN_EXPIRY_INVALID") from None
            if not now + 60 < expires <= now + 3660:
                raise AppError("APP_TOKEN_EXPIRY_INVALID")
            self._cached, self._expires = _token(result.get("token")), expires
            return self._cached


class ChecksAppClient:
    """A repository-bound Checks-only client; all lifecycle writes are explicit."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.repo_id = tokens.repo_id
        self.repo = MODEL_REPOSITORIES[self.repo_id][0]
        self.app_id = tokens.app_id

    def __repr__(self):
        return f"ChecksAppClient(repo={self.repo!r}, app_id={self.app_id})"

    def request(self, method, path, data=None):
        endpoint = path.partition("?")[0] if isinstance(path, str) else ""
        allowed = (
            method == "POST"
            and endpoint == "/check-runs"
            and "?" not in path
            or method in {"GET", "PATCH"}
            and re.fullmatch(r"/check-runs/[1-9][0-9]*", endpoint)
            or method == "GET"
            and re.fullmatch(r"/commits/[0-9a-f]{40}/check-runs", endpoint)
        )
        if not allowed:
            raise AppError("CHECKS_CLIENT_ENDPOINT_NOT_ALLOWED")
        # Exactly one request: CheckRunChannel owns unknown-write recovery.
        client = GitHubClient(self.tokens(), self.repo, api_version=API_VERSION)
        try:
            return client.request(method, path, data)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                self.tokens.invalidate()
            if status == 429 or status >= 500:
                raise AppUnavailable(f"CHECKS_HTTP_ERROR_{status}") from None
            raise AppError(f"CHECKS_HTTP_ERROR_{status}") from None
        except (OSError, TimeoutError, ValueError):
            raise AppUnavailable("CHECKS_REQUEST_OUTCOME_UNKNOWN") from None
