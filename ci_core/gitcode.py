"""GitCode Actions transport for the existing pull scheduler."""

import datetime as dt
import io
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from ci_core.contract import REPO, parse_plan


class GitCodeClient:
    def __init__(self, token, repo):
        if repo not in {"depeng1994/torchtitan-npu", "cann/torchtitan-npu"}:
            raise ValueError("untrusted GitCode repository")
        if not token:
            raise ValueError("GitCode token required")
        self.repo, self.token = repo, token
        self.v8 = f"https://api.gitcode.com/api/v8/repos/{repo}"
        self.v5 = f"https://api.gitcode.com/api/v5/repos/{repo}"

    def request(self, method, path, data=None, *, api=5):
        if not path.startswith("/") or "://" in path:
            raise ValueError("relative GitCode API path required")
        payload = (
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            if data is not None
            else None
        )
        if payload is not None and len(payload) > 7000:
            raise ValueError("request exceeds byte limit")
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "lite-actions",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            (self.v8 if api == 8 else self.v5) + path,
            data=payload,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(1024 * 1024)
        return json.loads(raw) if raw else None

    def workflow_status(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", run_id
        ):
            raise ValueError("invalid GitCode run ID")
        result = self.request("GET", f"/actions/runs/{run_id}", api=8)
        return {
            "status": "completed"
            if result.get("status") in {"COMPLETED", "FAILED", "CANCELED", "IGNORED"}
            else "in_progress",
            "run_attempt": result.get("attempt", 1),
        }

    def running_workflows(self):
        runs = []
        for page in range(1, 6):
            body = self.request(
                "GET",
                f"/actions/runs?event=Manual&status=RUNNING&per_page=100&page={page}",
                api=8,
            )
            rows = body.get("workflow_runs", [])
            for row in rows:
                if row.get("event") != "Manual" or row.get("status") != "RUNNING":
                    continue
                run_id = row.get("workflow_run_id")
                if not isinstance(run_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}", run_id
                ):
                    continue
                details = self.request("GET", f"/actions/runs/{run_id}", api=8)
                # This platform's runner does not expose the current attempt.
                # Accept only a fresh run until its identity is available there.
                if details.get("attempt") != 1:
                    continue
                if any(
                    details.get(k) != row.get(k)
                    for k in (
                        "workflow_run_id",
                        "event",
                        "status",
                        "head_sha",
                        "head_branch",
                        "file_path",
                    )
                ) or details.get("actor", {}).get("login") != row.get("actor", {}).get(
                    "login"
                ):
                    continue
                start = row.get("start_time")
                if not isinstance(start, (int, float)):
                    continue
                if start > 10**11:
                    start /= 1000
                runs.append(
                    {
                        "_ci_provider": "gitcode",
                        "_ci_repo": self.repo,
                        "id": run_id,
                        "run_attempt": details.get("attempt", 1),
                        "head_sha": row.get("head_sha"),
                        "head_branch": row.get("head_branch"),
                        "actor": row.get("actor") or {},
                        "path": row.get("file_path"),
                        "name": "Dispatcher-Smoke",
                        "run_number": row.get("run_number"),
                        "created_at": dt.datetime.fromtimestamp(
                            start, dt.timezone.utc
                        ).isoformat(),
                        "event": "workflow_dispatch",
                        "status": "in_progress",
                    }
                )
            if len(rows) < 100:
                break
        return runs

    def validate_current(self, run):
        details = self.request("GET", f"/actions/runs/{run['id']}", api=8)
        expected = {
            "workflow_run_id": run["id"],
            "attempt": run["run_attempt"],
            "head_sha": run["head_sha"],
            "head_branch": run["head_branch"],
            "file_path": run["path"],
            "event": "Manual",
            "status": "RUNNING",
        }
        if (
            any(details.get(k) != v for k, v in expected.items())
            or details.get("actor", {}).get("login") != run["actor"]["login"]
        ):
            raise ValueError("GitCode run changed or ended")

    def ci_request(self, run):
        run_id, attempt = run["id"], run["run_attempt"]
        name = f"lite-ci-request-{attempt}"
        found = self.request(
            "GET", f"/actions/runs/{run_id}/artifacts?per_page=100", api=8
        )
        matches = [
            a
            for a in found.get("artifacts", [])
            if a.get("name") == name and a.get("workflow_run_id") == run_id
        ]
        if not matches:
            return None
        if len(matches) != 1 or matches[0].get("size_bytes", 0) > 262144:
            raise ValueError("ambiguous or oversized request artifact")
        artifact_id = matches[0]["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(artifact_id)):
            raise ValueError("invalid artifact ID")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        req = urllib.request.Request(
            f"{self.v8}/actions/artifacts/{artifact_id}/zip",
            headers={"PRIVATE-TOKEN": self.token},
        )
        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=20)
            raise ValueError("artifact download did not redirect")
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
            url = exc.headers.get("Location", "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("invalid artifact redirect")
        with urllib.request.urlopen(
            urllib.request.Request(url), timeout=30
        ) as response:
            payload = response.read(262145)
        if len(payload) > 262144:
            raise ValueError("artifact exceeds limit")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if (
                len(names) != 1
                or names[0].split("/")[-1] != "ci-request.json"
                or ".." in names[0].split("/")
            ):
                raise ValueError("unexpected artifact contents")
            info = archive.getinfo(names[0])
            if info.file_size > 262144:
                raise ValueError("request exceeds limit")
            plan = json.loads(archive.read(info))
        parse_plan(plan, run, run["path"].rsplit("/", 1)[-1], repo=self.repo)
        if len(plan["cases"]) != 1:
            raise ValueError("GitCode fixed environment requires exactly one case")
        if (
            os.environ.get("LITE_ACTIONS_GITCODE_PROBE_ONLY") == "1"
            and plan["cases"][0]["kind"] != "worker_probe"
        ):
            raise ValueError("only zero-device probes are enabled")
        return plan

    def commit_comments(self, sha):
        comments = []
        for page in range(1, 21):
            rows = (
                self.request("GET", f"/commits/{sha}/comments?per_page=100&page={page}")
                or []
            )
            if not isinstance(rows, list):
                raise ValueError("invalid GitCode comments")
            comments.extend(rows)
            if len(rows) < 100:
                return comments
        raise RuntimeError("too many comments")

    def send_result(self, sha, body):
        return self.request("POST", f"/commits/{sha}/comments", {"body": body})


class MultiPlatformClient:
    def __init__(self, github, gitcode):
        self.github, self.gitcode = github, gitcode

    def for_run(self, run):
        if run.get("_ci_provider", "github") == "gitcode":
            return self.gitcode[run["_ci_repo"]]
        repo = run.get("_ci_repo", REPO)
        if self.github is None:
            raise ValueError("GitHub polling disabled")
        if not hasattr(self.github, "for_repo") and self.github.repo != repo:
            raise ValueError("run belongs to another GitHub repository")
        return (
            self.github.for_repo(repo)
            if hasattr(self.github, "for_repo")
            else self.github
        )

    def running_workflows(self):
        runs = []
        for client in [self.github, *self.gitcode.values()]:
            if client is None:
                continue
            try:
                runs.extend(client.running_workflows())
            except Exception:
                logging.exception("provider polling failed")
        return runs
