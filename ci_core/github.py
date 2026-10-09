"""Small, counted GitHub REST client for outbound-only CI orchestration."""
from __future__ import annotations
import json
import os
import subprocess
import urllib.parse
import urllib.request

REPO = "depeng1994/torchtitan-npu"
API = f"https://api.github.com/repos/{REPO}"


def authorization() -> str:
    token = os.environ.get("A3_CI_GITHUB_TOKEN")
    if token:
        return token
    proc = subprocess.run(["gh", "auth", "token"], capture_output=True,
                          text=True, timeout=15)
    if proc.returncode or not proc.stdout.strip():
        raise RuntimeError("gh auth token unavailable")
    return proc.stdout.strip()


class GitHubClient:
    def __init__(self, token: str):
        self.token = token
        self.sent_bytes = 0
        self.requests = 0

    def request(self, method: str, path: str, data: dict | None = None):
        if not path.startswith("/") or "://" in path:
            raise ValueError("relative GitHub API path required")
        url = API + path
        payload = (json.dumps(data, separators=(",", ":"), ensure_ascii=False)
                   .encode("utf-8") if data is not None else None)
        if payload is not None and len(payload) > 7000:
            raise ValueError("POST payload exceeds 7000 bytes")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "lite-actions/2",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url_parts = urllib.parse.urlsplit(url)
        head = f"{method} {url_parts.path}" + (
            f"?{url_parts.query}" if url_parts.query else ""
        ) + " HTTP/1.1\r\n"
        head += f"Host: {url_parts.netloc}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k,v in headers.items())
        if payload is not None:
            head += f"Content-Length: {len(payload)}\r\n"
        head += "\r\n"
        uploaded = len(head.encode()) + len(payload or b"")
        self.sent_bytes += uploaded
        self.requests += 1
        request = urllib.request.Request(url, data=payload, method=method,
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    def running_workflows(self) -> list[dict]:
        data = self.request("GET",
            "/actions/runs?event=workflow_dispatch&status=in_progress&per_page=100")
        return data.get("workflow_runs", [])

    def commit_comments(self, sha: str) -> list[dict]:
        data = self.request("GET", f"/commits/{sha}/comments?per_page=100")
        return data or []

    def send_result(self, sha: str, body: str):
        return self.request("POST", f"/commits/{sha}/comments", {"body": body})
