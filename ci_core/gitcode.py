"""GitCode Actions transport for the existing pull scheduler."""

import datetime as dt
import io
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

from ci_core.contract import (
    REPO,
    digest,
    normalize,
    parse_plan,
    trusted_gitcode_repositories,
)


class RunChanged(ValueError):
    """A run or its PR no longer points to the admitted source."""


def source_plan(rows, run, repo):
    """Normalize the fixed-SHA model's ordered smoke profile for both workers."""
    defaults = {"worker": "", "target_pool": "", "docker": ""}
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty source smoke profile")
    cases = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {
            "worker_probe",
            "npu_smoke",
        }:
            raise ValueError("invalid source smoke definition")
        if row["kind"] == "worker_probe" and (
            row.get("nnodes") != 0 or row.get("ngpu") != 0
        ):
            raise ValueError("worker probes must not request devices")
        cases.append(
            normalize(
                SimpleNamespace(
                    test_name=row["test_id"],
                    test_descr=row["label"],
                    ci_kind=row["kind"],
                    ci_overrides=row["ci_overrides"],
                    nnodes=row["nnodes"],
                    ngpu=row["ngpu"],
                    timeout=row["timeout"],
                ),
                defaults,
                len(cases),
            )
        )
    plan = dict(
        schema=3,
        repo=repo,
        workflow=run["path"].rsplit("/", 1)[-1],
        run_id=run["id"],
        attempt=run["run_attempt"],
        sha=run["head_sha"],
        defaults=defaults,
        cases=cases,
    )
    plan["plan_digest"] = digest(plan)
    parse_plan(plan, run, plan["workflow"], repo=repo)
    return plan


def source_dispatch(details):
    stages = details.get("stages")
    if not isinstance(stages, list) or any(
        not isinstance(stage, dict)
        or not isinstance(stage.get("jobs"), list)
        or any(not isinstance(job, dict) for job in stage["jobs"])
        for stage in stages
    ):
        raise RunChanged("invalid smoke execution jobs")
    jobs = [
        job
        for stage in stages
        for job in stage.get("jobs", [])
        if job.get("name") == "Dispatcher Smoke"
    ]
    if len(jobs) != 1 or jobs[0].get("status") != "RUNNING":
        raise RunChanged("smoke execution job unavailable or ambiguous")
    dispatch = jobs[0].get("last_dispatch_id")
    started = jobs[0].get("start_time")
    if not isinstance(dispatch, str) or type(started) is not int or started <= 0:
        raise RunChanged("missing smoke execution dispatch identity")
    identity = f"{dispatch}_{started}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise RunChanged("invalid smoke execution dispatch identity")
    return identity


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate catalog field")
        obj[key] = value
    return obj


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitCodeClient:
    def __init__(self, token, repo):
        if repo not in trusted_gitcode_repositories():
            raise ValueError("untrusted GitCode repository")
        if not token:
            raise ValueError("GitCode token required")
        self.repo, self.token = repo, token
        self.v8 = f"https://api.gitcode.com/api/v8/repos/{repo}"
        self.v5 = f"https://api.gitcode.com/api/v5/repos/{repo}"

    def request(self, method, path, data=None, *, api=5):
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "://" in path
            or "\\" in path
            or ".." in path.split("/")
        ):
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
        with urllib.request.build_opener(NoRedirect()).open(
            req, timeout=30
        ) as response:
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

    def pr_source(self, details):
        number = str(details.get("pull_request_id") or "")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", number):
            raise RunChanged("missing PR identity")
        pr = self.request("GET", f"/pulls/{number}")
        head, base = pr.get("head") or {}, pr.get("base") or {}
        source = (head.get("repo") or {}).get("full_name")
        if (
            pr.get("state") != "open"
            or str(pr.get("number")) != number
            or (base.get("repo") or {}).get("full_name") != self.repo
            or not isinstance(source, str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                source,
            )
            or head.get("sha") != details.get("head_sha")
            or head.get("ref") != details.get("head_branch")
        ):
            raise RunChanged("PR source no longer matches run")
        return int(number), source

    def running_workflows(self):
        runs = []
        for page in range(1, 6):
            body = self.request(
                "GET",
                f"/actions/runs?status=RUNNING&per_page=100&page={page}",
                api=8,
            )
            rows = body.get("workflow_runs", [])
            for row in rows:
                if row.get("status") != "RUNNING" or not str(
                    row.get("workflow_name", "")
                ).startswith("lite-ci-"):
                    continue
                run_id = row.get("workflow_run_id")
                if not isinstance(run_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,128}", run_id
                ):
                    continue
                try:
                    details = self.request("GET", f"/actions/runs/{run_id}", api=8)
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    ValueError,
                ) as exc:
                    # A transient failure for one run must not prevent another
                    # valid run in the same repository from being discovered.
                    logging.warning(
                        "GitCode run %s could not be read (%s); retrying next poll",
                        run_id,
                        type(exc).__name__,
                    )
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
                        "workflow_name",
                    )
                ):
                    continue
                start = row.get("start_time")
                if not isinstance(start, (int, float)):
                    continue
                if start > 10**11:
                    start /= 1000
                dispatch_fields = {}
                jobs = [
                    job
                    for stage in (details.get("stages") or [])
                    if isinstance(stage, dict)
                    for job in (stage.get("jobs") or [])
                    if isinstance(job, dict)
                ]
                if any(job.get("name") == "Dispatcher Smoke" for job in jobs):
                    try:
                        dispatch = source_dispatch(details)
                    except RunChanged:
                        continue
                    dispatch_fields = {"_ci_dispatch_id": dispatch}
                pr_fields = {}
                if details.get("pull_request_id"):
                    try:
                        number, source = self.pr_source(details)
                    except (
                        urllib.error.URLError,
                        TimeoutError,
                        OSError,
                        ValueError,
                    ) as exc:
                        logging.warning(
                            "GitCode PR source unavailable for %s (%s)",
                            run_id,
                            type(exc).__name__,
                        )
                        continue
                    pr_fields = {"_ci_pr_number": number, "_ci_source_repo": source}
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
                        "name": row.get("workflow_name"),
                        "run_number": row.get("run_number"),
                        "created_at": dt.datetime.fromtimestamp(
                            start, dt.timezone.utc
                        ).isoformat(),
                        "event": row.get("event"),
                        "status": "in_progress",
                        **pr_fields,
                        **dispatch_fields,
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
            "event": run["event"],
            "workflow_name": run["name"],
            "status": "RUNNING",
        }
        if any(
            details.get(k, 1 if k == "attempt" else None) != v
            for k, v in expected.items()
        ) or (
            "_ci_dispatch_id" in run
            and source_dispatch(details) != run["_ci_dispatch_id"]
        ):
            raise RunChanged("GitCode run changed or ended")
        if "_ci_pr_number" in run and self.pr_source(details) != (
            run.get("_ci_pr_number"),
            run.get("_ci_source_repo"),
        ):
            raise RunChanged("GitCode PR source changed")

    def ci_request(self, run):
        if "_ci_dispatch_id" in run:
            from ci_core.registry import inspect_archive
            from ci_core.ssh_runner import model_archive

            self.validate_current(run)
            with tempfile.TemporaryDirectory(prefix="lite-gitcode-source-") as tmp:
                source = model_archive(
                    run["head_sha"],
                    Path(tmp),
                    source_repo=self.repo,
                    provider="gitcode",
                    fallback_repo=run.get("_ci_source_repo"),
                )
                rows = inspect_archive(
                    source, {"suite": "dispatch_smoke"}, repo=self.repo
                )
            plan = source_plan(rows, run, self.repo)
            if os.environ.get("LITE_ACTIONS_GITCODE_PROBE_ONLY") == "1" and any(
                case["kind"] != "worker_probe" for case in plan["cases"]
            ):
                raise ValueError("only zero-device probes are enabled")
            return plan
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
            payload = archive.read(info)
        plan = json.loads(payload)
        parse_plan(plan, run, run["path"].rsplit("/", 1)[-1], repo=self.repo)
        if len(plan["cases"]) != 1:
            raise ValueError("GitCode fixed environment requires exactly one case")
        if os.environ.get("LITE_ACTIONS_GITCODE_PROBE_ONLY") == "1" and any(
            case["kind"] != "worker_probe" for case in plan["cases"]
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
            except Exception as exc:
                logging.warning("provider polling failed (%s)", type(exc).__name__)
        return runs
