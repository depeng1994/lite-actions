"""Small, counted GitHub REST client for outbound-only CI orchestration."""
from __future__ import annotations
import json
import urllib.error
import io
import zipfile
import os
from pathlib import Path
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
        if type(run_id) is not int or type(attempt) is not int or run_id < 1 or attempt < 1:
            raise ValueError("invalid run/attempt")
        name = f"lite-ci-request-{attempt}"
        query = urllib.parse.urlencode({"name":name,"per_page":10})
        listing = self.request("GET", f"/actions/runs/{run_id}/artifacts?{query}")
        matching = [a for a in listing.get("artifacts", [])
                    if a.get("name") == name and not a.get("expired")]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError("ambiguous CI request artifacts")
        artifact = matching[0]
        if artifact.get("size_in_bytes", 0) > 65536:
            raise ValueError("CI request artifact oversized")
        if artifact.get("workflow_run", {}).get("id", run_id) != run_id:
            raise ValueError("CI artifact does not belong to the selected run")
        artifact_id = artifact["id"]
        url = API + f"/actions/artifacts/{artifact_id}/zip"
        req = urllib.request.Request(url, headers={
            "Accept":"application/vnd.github+json",
            "Authorization":f"Bearer {self.token}",
            "User-Agent":"lite-actions-request/1",
        })
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=20)
            raise ValueError("artifact download did not redirect")
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
            signed_url = exc.headers.get("Location")
        parts = urllib.parse.urlsplit(signed_url or "")
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("invalid signed artifact download URL")
        # Signed URL is bearer access in itself. Never forward GitHub token.
        with urllib.request.urlopen(urllib.request.Request(signed_url), timeout=25) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("downloaded CI request oversized")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist() != ["ci-request.json"]:
                raise ValueError("unexpected CI request archive contents")
            info = archive.getinfo("ci-request.json")
            if info.file_size > 32768:
                raise ValueError("CI request JSON too large")
            request_data = json.loads(archive.read(info))
        if request_data.get("schema") == 2:
            from ci_core.protocol_v2 import parse_plan
            # Pipeline authorization is resolved from the trusted workflow path.
            from ci_core.config import load_config
            configs = load_config(Path(__file__).resolve().parents[1])
            matching = [spec for spec in configs.values()
                        if run.get("path") == ".github/workflows/" + spec["workflow"]]
            if len(matching) != 1:
                raise ValueError("unrecognized V2 workflow path")
            parse_plan(request_data, run, matching[0]["pipeline_id"])
        else:
            from ci_core.registry import parse_request
            parse_request(request_data, run)
        return request_data

    def commit_comments(self, sha: str) -> list[dict]:
        comments = []
        for page in range(1, 5):
            data = self.request(
                "GET", f"/commits/{sha}/comments?per_page=100&page={page}") or []
            if not isinstance(data, list):
                raise ValueError("invalid commit comments response")
            comments.extend(data)
            if len(data) < 100:
                return comments
        raise RuntimeError(
            "too many commit comments; cannot safely correlate prior result")

    def send_result(self, sha: str, body: str):
        return self.request("POST", f"/commits/{sha}/comments", {"body": body})
