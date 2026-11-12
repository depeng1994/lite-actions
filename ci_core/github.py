"""Small, counted GitHub REST client for outbound-only CI orchestration."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from ci_core.contract import REPO, trusted_repositories

API = f"https://api.github.com/repos/{REPO}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorization() -> str:
    token = os.environ.get("LITE_ACTIONS_GITHUB_TOKEN")
    if token:
        return token
    proc = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
    )
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError("gh auth token unavailable")
    return proc.stdout.strip()


class GitHubClient:
    def __init__(self, token: str, repo: str = REPO, *, policy=None):
        if repo not in trusted_repositories():
            raise ValueError("untrusted GitHub repository")
        self.policy = policy
        self.repo = repo
        self.api = f"https://api.github.com/repos/{repo}"
        self.token = token
        self._counter_lock = threading.Lock()
        self.sent_bytes = 0
        self.requests = 0

    def request(self, method: str, path: str, data: dict | None = None):
        if (
            not isinstance(path, str)
            or (path and not path.startswith("/"))
            or path.startswith("//")
            or "://" in path
            or "\\" in path
            or ".." in path.split("/")
        ):
            raise ValueError("relative GitHub API path required")
        url = self.api + path
        payload = (
            json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if data is not None
            else None
        )
        budget = (
            64 * 1024
            if path.startswith("/check-runs")
            else (32 * 1024 if path.endswith("/dispatches") else 7000)
        )
        if payload is not None:
            ceiling = (
                65536
                if self.policy and getattr(self.policy, "worker", None) == "yellow"
                else budget
            )
            if len(payload) > ceiling:
                raise ValueError("API payload exceeds endpoint budget")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "lite-actions",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url_parts = urllib.parse.urlsplit(url)
        head = (
            f"{method} {url_parts.path}"
            + (f"?{url_parts.query}" if url_parts.query else "")
            + " HTTP/1.1\r\n"
        )
        head += f"Host: {url_parts.netloc}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        if payload is not None:
            head += f"Content-Length: {len(payload)}\r\n"
        head += "\r\n"
        uploaded = len(head.encode()) + len(payload or b"")
        with self._counter_lock:
            self.sent_bytes += uploaded
            self.requests += 1
        request = urllib.request.Request(
            url, data=payload, method=method, headers=headers
        )

        if self.policy:
            request = self.policy.before(request, repo=self.repo, provider="github")
        # Repository aliases/redirects must not silently change trusted identity.
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=30
        ) as response:
            data = response.read(8 * 1024**2 + 1)
            if len(data) > 8 * 1024**2:
                raise ValueError("GitHub response exceeds endpoint budget")
            return json.loads(data) if data else None

    def download_artifact(self, artifact_id: int, *, limit: int) -> bytes:
        """Download a bounded artifact without forwarding credentials to storage."""
        if (
            type(artifact_id) is not int
            or artifact_id < 1
            or type(limit) is not int
            or not 1 <= limit <= 262144
        ):
            raise ValueError("invalid artifact identifier or byte budget")

        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(
            self.api + f"/actions/artifacts/{artifact_id}/zip",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if self.policy:
            request = self.policy.before(request, repo=self.repo, provider="github")
        try:
            with opener.open(request, timeout=20):
                raise ValueError("artifact download did not redirect")
        except urllib.error.HTTPError as exc:
            code, location = exc.code, exc.headers.get("Location")
            exc.close()
            if code != 302:
                raise RuntimeError(f"artifact API unavailable (HTTP {code})") from None
        except (OSError, TimeoutError):
            raise RuntimeError("artifact API unavailable") from None
        parts = urllib.parse.urlsplit(location or "")
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
            or parts.fragment
            or not (
                host.endswith(".blob.core.windows.net")
                or host.endswith(".actions.githubusercontent.com")
            )
        ):
            raise ValueError("untrusted artifact download destination")
        download = urllib.request.Request(location)
        if self.policy:
            self.policy.before(
                download, repo=self.repo, provider="github", storage=True
            )
        try:
            with opener.open(download, timeout=60) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > limit:
                    raise ValueError("artifact download exceeds byte budget")
                payload = response.read(limit + 1)
        except (OSError, TimeoutError):
            raise RuntimeError("artifact storage unavailable") from None
        if len(payload) > limit:
            raise ValueError("artifact download exceeds byte budget")
        return payload

    def running_workflows(self) -> list[dict]:
        runs = {}
        workflow_names = {}
        for status in ("queued", "in_progress"):
            for page in range(1, 11):
                data = self.request(
                    "GET", f"/actions/runs?status={status}&per_page=100&page={page}"
                )
                rows = data.get("workflow_runs", [])
                for run in rows:
                    if run is None or run.get("status") not in {
                        "queued",
                        "in_progress",
                    }:
                        continue
                    name = run.get("name") or ""
                    workflow_id = run.get("workflow_id")
                    if (
                        not str(name).startswith("lite-ci-")
                        and type(workflow_id) is int
                        and workflow_id > 0
                    ):
                        if workflow_id not in workflow_names:
                            try:
                                workflow_names[workflow_id] = self.request(
                                    "GET", f"/actions/workflows/{workflow_id}"
                                ).get("name", "")
                            except (OSError, TimeoutError, ValueError):
                                logging.warning(
                                    "workflow metadata unavailable: %s", workflow_id
                                )
                                continue
                        name = workflow_names[workflow_id]
                    if not str(name).startswith("lite-ci-"):
                        continue
                    identity = (run["id"], run.get("run_attempt", 1))
                    if identity not in runs or run["status"] == "in_progress":
                        runs[identity] = {**run, "name": name, "_ci_repo": self.repo}
                if len(rows) < 100:
                    break
        return list(runs.values())

    def workflow_status(self, run_id: int) -> dict:
        if type(run_id) is not int or run_id < 1:
            raise ValueError("invalid GitHub workflow run identity")
        return self.request("GET", f"/actions/runs/{run_id}")

    def ci_request(self, run: dict) -> dict | None:
        """Retrieve a short structured artifact uploaded by this Actions run.

        Missing artifact means the GitHub preparation job hasn't finished yet;
        polling will retry. Never put test choices in the run title.
        """
        run_id, attempt = run["id"], run.get("run_attempt", 1)
        if (
            type(run_id) is not int
            or type(attempt) is not int
            or run_id < 1
            or attempt < 1
        ):
            raise ValueError("invalid run/attempt")
        name = f"lite-ci-request-{attempt}"
        query = urllib.parse.urlencode({"name": name, "per_page": 10})
        listing = self.request("GET", f"/actions/runs/{run_id}/artifacts?{query}")
        matching = [
            a
            for a in listing.get("artifacts", [])
            if a.get("name") == name and not a.get("expired")
        ]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError("ambiguous CI request artifacts")
        artifact = matching[0]
        if artifact.get("size_in_bytes", 0) > 262144:
            raise ValueError("CI request artifact oversized")
        if artifact.get("workflow_run", {}).get("id", run_id) != run_id:
            raise ValueError("CI artifact does not belong to the selected run")
        payload = self.download_artifact(artifact["id"], limit=262144)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != ["ci-request.json"]:
                raise ValueError("unexpected CI request archive contents")
            info = archive.getinfo("ci-request.json")
            if info.file_size > 262144:
                raise ValueError("CI request JSON too large")
            request_data = json.loads(archive.read(info))
        from ci_core.contract import parse_plan

        parse_plan(
            request_data, run, run.get("path", "").rsplit("/", 1)[-1], repo=self.repo
        )
        return request_data

    def commit_comments(self, sha: str) -> list[dict]:
        comments = []
        for page in range(1, 21):
            data = (
                self.request("GET", f"/commits/{sha}/comments?per_page=100&page={page}")
                or []
            )
            if not isinstance(data, list):
                raise ValueError("invalid commit comments response")
            comments.extend(data)
            if len(data) < 100:
                return comments
        raise RuntimeError(
            "too many commit comments; cannot safely correlate prior result"
        )

    def send_result(self, sha: str, body: str):
        return self.request("POST", f"/commits/{sha}/comments", {"body": body})


class MultiRepoGitHubClient:
    """One poller returns runs from all configured repositories."""

    def __init__(self, token: str, repos: tuple[str, ...], *, policy=None):
        self.clients = {
            repo: GitHubClient(token, repo, policy=policy) for repo in repos
        }

    def for_repo(self, repo: str) -> GitHubClient:
        return self.clients[repo]

    def running_workflows(self) -> list[dict]:
        runs = []
        for client in self.clients.values():
            try:
                runs.extend(client.running_workflows())
            except Exception as exc:
                logging.warning(
                    "repository poll unavailable: %s (%s)",
                    client.repo,
                    type(exc).__name__,
                )
        return sorted(runs, key=lambda r: (r.get("created_at", ""), r["id"]))


class ReleaseLogs:
    """Blue-only native GitHub Release assets. Never create releases, repositories or tokens."""

    def __init__(self, worker, cfg, *, token, opener=None, clock=None):
        import re
        import time
        from pathlib import Path

        from ci_core.contract import REPOSITORY

        dest = cfg.get("public_release", {})
        if (
            worker != "blue"
            or dest.get("public_diagnostics_approved") is not True
            or not token
        ):
            raise ValueError("PUBLIC_LOG_DESTINATION_NOT_APPROVED")
        self.repo = dest["repository"]
        self.release = dest["release_id"]
        self.tag = dest["tag"]
        self.uploader = dest["publisher_login"]
        if (
            not REPOSITORY.fullmatch(self.repo)
            or type(self.release) is not int
            or self.release < 1
            or not re.fullmatch(r"[0-9A-Za-z._-]{1,100}", self.tag)
            or not re.fullmatch(r"[A-Za-z0-9_\[\]-]{1,80}", self.uploader)
        ):
            raise ValueError("PUBLIC_LOG_DESTINATION_INVALID")
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock = clock or time.time
        self.receipts = Path(cfg["state_dir"]) / "release-receipts"
        self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.api = f"https://api.github.com/repos/{self.repo}"

    def api_call(self, method, path, *, data=None):
        if data is None:
            url = self.api + path
        else:
            url = f"https://uploads.github.com/repos/{self.repo}" + path
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "lite-actions",
        }
        if data is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
        req = urllib.request.Request(url, method=method, headers=headers, data=data)
        with self.opener.open(req, timeout=45) as r:
            raw = r.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise ValueError("oversized release response")
        return json.loads(raw) if raw else None

    def verify_release(self):
        repo = self.api_call("GET", "")
        release = self.api_call("GET", f"/releases/{self.release}")
        if (
            repo.get("full_name") != self.repo
            or repo.get("private") is not False
            or release.get("id") != self.release
            or release.get("draft") is not False
            or release.get("tag_name") != self.tag
        ):
            raise ValueError("PUBLIC_RELEASE_MISMATCH")

    def find(self, name):
        for page in range(1, 21):
            rows = self.api_call(
                "GET", f"/releases/{self.release}/assets?per_page=100&page={page}"
            )
            if not isinstance(rows, list):
                raise ValueError("invalid asset listing")
            matches = [row for row in rows if row.get("name") == name]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise ValueError("conflicting assets")
            if len(rows) < 100:
                return None
        raise ValueError("release asset listing exceeds safety limit")

    def verified(self, row, intent):
        link = f"https://github.com/{self.repo}/releases/download/{self.tag}/{intent['name']}"
        if (
            not isinstance(row, dict)
            or row.get("digest") != "sha256:" + intent["sha256"]
            or row.get("size") != intent["bytes"]
            or row.get("state") != "uploaded"
            or row.get("name") != intent["name"]
            or row.get("browser_download_url") != link
            or row.get("uploader", {}).get("login") != self.uploader
            or type(row.get("id")) is not int
        ):
            raise ValueError("PUBLIC_ASSET_MISMATCH")
        return dict(
            url=link,
            asset_id=row["id"],
            sha256=intent["sha256"],
            bytes=intent["bytes"],
            truncated=intent["truncated"],
        )

    def deliver(self, key, data, truncated):
        import hashlib

        from ci_core.logs import BLUE_BYTES

        if type(data) is not bytes or not 0 < len(data) <= BLUE_BYTES:
            raise ValueError("BLUE_SIZE")
        self.verify_release()
        name = "lite-ci-" + hashlib.sha256(key.encode()).hexdigest() + ".log"
        receipt = self.receipts / (name + ".json")
        if receipt.with_suffix(".expired").exists():
            raise ValueError("BLUE_ASSET_EXPIRED")
        expected = dict(
            name=name,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            truncated=truncated,
            repo=self.repo,
            release=self.release,
        )
        # O_EXCL and fsync reserve the ENTIRE case allowance before transmitting. A lost
        # response may only be resolved by GET, even after a process or machine restart.
        try:
            f = receipt.open("x", encoding="utf8")
        except FileExistsError:
            prior = json.loads(receipt.read_bytes())
            if any(prior.get(k) != v for k, v in expected.items()):
                raise ValueError("ASSET_INTENT_CONFLICT")
            return self.verified(self.find(name), expected)
        with f:
            json.dump(dict(expected, created=self.clock()), f)
            f.flush()
            os.fsync(f.fileno())
        d = os.open(self.receipts, os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
        try:
            row = self.api_call(
                "POST", f"/releases/{self.release}/assets?name={name}", data=data
            )
        except (OSError, TimeoutError):
            row = self.find(name)
        return self.verified(row, expected)

    def expire(self):
        """Delete only past-due assets matching our local intent and the exact publisher/digest."""
        for receipt in self.receipts.glob("lite-ci-*.log.json"):
            info = json.loads(receipt.read_bytes())
            if (
                self.clock() - info["created"] < 7 * 86400
                or info.get("repo") != self.repo
                or info.get("release") != self.release
                or receipt.name != info["name"] + ".json"
            ):
                continue
            asset = self.find(info["name"])
            if asset is not None:
                ref = self.verified(asset, info)
                self.api_call("DELETE", f"/releases/assets/{ref['asset_id']}")
            # Retain the small immutable tombstone: an old replay must never upload again.
            receipt.rename(receipt.with_suffix(".expired"))
