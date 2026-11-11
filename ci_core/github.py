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
    def __init__(self, token: str, repo: str = REPO):
        if repo not in trusted_repositories():
            raise ValueError("untrusted GitHub repository")
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
        if payload is not None and len(payload) > budget:
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
        try:
            with opener.open(urllib.request.Request(location), timeout=60) as response:
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
        events = ("workflow_dispatch",)
        for event in events:
            for status in ("queued", "in_progress"):
                data = self.request(
                    "GET", f"/actions/runs?event={event}&status={status}&per_page=100"
                )
                for run in data.get("workflow_runs", []):
                    if run is None or run.get("status") not in {
                        "queued",
                        "in_progress",
                    }:
                        continue
                    identity = (run["id"], run.get("run_attempt", 1))
                    if identity not in runs or run["status"] == "in_progress":
                        runs[identity] = {**run, "_ci_repo": self.repo}
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

    def __init__(self, token: str, repos: tuple[str, ...]):
        self.clients = {repo: GitHubClient(token, repo) for repo in repos}

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
