"""Bounded standard-library control; generated inline, never imports PR source."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

GC_REPO = "depeng1994/torchtitan-npu"
GC_ID = "9075802"
GH_REPO = "cann-framework/torchtitan-npu"
GH_ID = 1210038933
GH_WORKFLOW_ID = 381287884
GH_WORKFLOW = ".github/workflows/lite_actions_dispatcher_smoke.yml"
GC_WORKFLOW = ".gitcode/workflows/torchtitan_forward_manual.yml"
CAPTURE_JOB = "manual_capture"
CAPTURE_STEP = "1"
CAPTURE_NAME = "Capture manual request"
PREFIX = "LITE_GITCODE_MANUAL_CAPTURE_V1 "
ALLOWED_ACTOR = "depeng1994"
ALLOWED_CASES = ("worker_os_blue", "dispatch_smoke_8p", "dispatch_smoke_16p")
CHECKS_APP_ID = 5575236


class ManualForwardError(RuntimeError):
    """Only fixed non-secret reason codes may be emitted."""


def require(value, code):
    if not value:
        raise ManualForwardError(code)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def sha(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "INVALID_SHA"
    )
    return value


def ident(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value),
        "INVALID_PLATFORM_ID",
    )
    return value


def ref(value):
    require(isinstance(value, str) and 0 < len(value) <= 200, "INVALID_REF")
    require(
        not any(ord(c) < 33 for c in value)
        and not any(
            x in value for x in ("..", "@{", "\\", "~", "^", ":", "?", "*", "[")
        ),
        "INVALID_REF",
    )
    require(
        not value.startswith(("/", "-")) and not value.endswith(("/", ".", ".lock")),
        "INVALID_REF",
    )
    return value


def api_route_label(service, path, *, dispatch=False):
    # Only fixed labels: never interpolate user-controlled URLs, IDs, headers or bodies.
    if service == "gitcode":
        if "/actions/runs/" in path:
            if path.endswith("/logs"):
                return "GITCODE_ACTION_STEP_LOGS"
            if "/jobs/" in path:
                return "GITCODE_ACTION_JOB"
            if path.endswith("/jobs"):
                return "GITCODE_ACTION_JOBS"
            return "GITCODE_ACTION_RUN"
        if "/pulls/" in path:
            return "GITCODE_PR"
        if "/branches/" in path:
            return "GITCODE_BRANCH"
        return "GITCODE_REPOSITORY"
    if dispatch:
        return "GITHUB_WORKFLOW_DISPATCH"
    if "/actions/runs/" in path:
        return "GITHUB_RUN_JOBS" if "/jobs?" in path else "GITHUB_RUN"
    if "/runs?" in path:
        return "GITHUB_NONCE_LOOKUP"
    if "/actions/workflows/" in path:
        return "GITHUB_WORKFLOW"
    if "/branches/" in path:
        return "GITHUB_BRANCH"
    return "GITHUB_REPOSITORY"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, environment):
        self.gitcode_token = environment.get("ATOMGIT_TOKEN", "")
        self.github_token = environment.get("LITE_ACTIONS_GITHUB_TOKEN", "")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(
        self, service, path, *, body=None, dispatch=False, authenticated_read=False
    ):
        require(
            path.startswith("/")
            and not path.startswith("//")
            and "://" not in path
            and "\\" not in path
            and ".." not in path,
            "UNSAFE_API_PATH",
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": "lite-gitcode-manual-control-v1",
        }
        if service == "gitcode":
            require(
                self.gitcode_token
                and "\n" not in self.gitcode_token
                and "\r" not in self.gitcode_token,
                "PLATFORM_READ_TOKEN_REQUIRED",
            )
            require(
                path.startswith(
                    ("/api/v5/repos/" + GC_REPO + "/", "/api/v8/repos/" + GC_REPO + "/")
                )
                or path == "/api/v5/repos/" + GC_REPO,
                "UNTRUSTED_GITCODE_TARGET",
            )
            if body is not None:
                require(
                    bool(
                        re.fullmatch(
                            r"/api/v8/repos/"
                            + re.escape(GC_REPO)
                            + r"/actions/runs/[A-Za-z0-9_-]+/jobs/[A-Za-z0-9_-]+/logs",
                            path,
                        )
                    ),
                    "ONLY_READ_ONLY_LOG_POST_ALLOWED",
                )
            origin = "https://api.gitcode.com"
            headers["Authorization"] = "Bearer " + self.gitcode_token
        else:
            require(
                service == "github"
                and (
                    path == "/repos/" + GH_REPO
                    or path.startswith("/repos/" + GH_REPO + "/")
                ),
                "UNTRUSTED_GITHUB_TARGET",
            )
            origin = "https://api.github.com"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            require(not (dispatch and authenticated_read), "GITHUB_AUTH_MODE_CONFLICT")
            if authenticated_read:
                require(body is None, "GITHUB_READ_CLIENT_CANNOT_WRITE")
                artifact_prefix = "/repos/" + GH_REPO + "/actions/runs/"
                artifact_suffix = "/artifacts?name=lite-ci-request-1&per_page=10&page=1"
                artifact_route = False
                if path.startswith(artifact_prefix) and path.endswith(artifact_suffix):
                    run_text = path[len(artifact_prefix) : -len(artifact_suffix)]
                    artifact_route = (
                        run_text.isascii() and run_text.isdigit() and int(run_text) > 0
                    )
                allowed = artifact_route or bool(
                    re.fullmatch(
                        r"/repos/"
                        + re.escape(GH_REPO)
                        + r"/(?:actions/artifacts/[0-9]+|check-runs/[0-9]+)",
                        path,
                    )
                )
                require(allowed, "GITHUB_AUTHENTICATED_READ_ROUTE_NOT_ALLOWED")
                require(
                    self.github_token
                    and not any(ord(c) < 32 for c in self.github_token),
                    "GITHUB_ACTIONS_READ_TOKEN_REQUIRED",
                )
                headers["Authorization"] = "Bearer " + self.github_token
            elif dispatch:
                require(
                    path
                    == "/repos/"
                    + GH_REPO
                    + f"/actions/workflows/{GH_WORKFLOW_ID}/dispatches"
                    and body is not None,
                    "ONLY_FIXED_DISPATCH_ALLOWED",
                )
                require(
                    self.github_token
                    and not any(ord(c) < 32 for c in self.github_token),
                    "GITHUB_DISPATCH_TOKEN_REQUIRED",
                )
                headers["Authorization"] = "Bearer " + self.github_token
            else:
                require(body is None, "GITHUB_READ_CLIENT_CANNOT_WRITE")
        data = None if body is None else canonical(body)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            origin + path,
            data=data,
            headers=headers,
            method="GET" if data is None else "POST",
        )
        # Never automatically retry a dispatch, follow redirects or print bodies.
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise ManualForwardError(
                "API_"
                + api_route_label(service, path, dispatch=dispatch)
                + "_HTTP_"
                + str(error.code)
            ) from None
        require(len(raw) <= 1024 * 1024, "API_RESPONSE_TOO_LARGE")
        if not raw:
            return None
        return json.loads(raw)

    def download_authenticated_artifact(self, artifact_id):
        require(type(artifact_id) is int and artifact_id > 0, "INVALID_ARTIFACT_ID")
        path = f"/repos/{GH_REPO}/actions/artifacts/{artifact_id}/zip"
        require(
            self.github_token and not any(ord(c) < 32 for c in self.github_token),
            "GITHUB_ACTIONS_READ_TOKEN_REQUIRED",
        )
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": "Bearer " + self.github_token,
                "User-Agent": "lite-gitcode-manual-control-v1",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                payload = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            if error.code != 302:
                raise ManualForwardError(
                    "API_GITHUB_PLAN_ARTIFACT_HTTP_" + str(error.code)
                ) from None
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlparse(location)
            host = parsed.hostname or ""
            trusted_host = host.endswith(".blob.core.windows.net") or host in {
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            require(
                parsed.scheme == "https"
                and trusted_host
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and bool(parsed.query),
                "GITHUB_PLAN_ARTIFACT_REDIRECT_UNTRUSTED",
            )
            # Signed redirect stays in memory and is never logged.
            blob_request = urllib.request.Request(
                location,
                headers={"User-Agent": "lite-gitcode-manual-control-v1"},
                method="GET",
            )
            try:
                with self.opener.open(blob_request, timeout=20) as response:
                    payload = response.read(1024 * 1024 + 1)
            except Exception:
                raise ManualForwardError(
                    "GITHUB_PLAN_ARTIFACT_BLOB_READ_FAILED"
                ) from None
        except Exception:
            raise ManualForwardError("GITHUB_PLAN_ARTIFACT_DOWNLOAD_FAILED") from None
        require(
            isinstance(payload, bytes) and 0 < len(payload) <= 1024 * 1024,
            "GITHUB_PLAN_ARTIFACT_SIZE_INVALID",
        )
        return payload

    def gc(self, suffix="", *, version=5, body=None):
        require(version in (5, 8), "UNSUPPORTED_API_VERSION")
        return self.request(
            "gitcode", f"/api/v{version}/repos/" + GC_REPO + suffix, body=body
        )

    def gh(self, suffix, *, body=None, dispatch=False, authenticated_read=False):
        return self.request(
            "github",
            "/repos/" + GH_REPO + suffix,
            body=body,
            dispatch=dispatch,
            authenticated_read=authenticated_read,
        )


def context(environment):
    require(
        environment.get("EVENT_NAME") == "Manual"
        and environment.get("ATOMGIT_EVENT_NAME", "Manual") == "Manual",
        "MANUAL_ONLY",
    )
    require(
        environment.get("RUN_ATTEMPT") == "1"
        and environment.get("ATOMGIT_RUN_ATTEMPT", "1") in ("", "1"),
        "NATIVE_RERUN_FORBIDDEN",
    )
    require(
        environment.get("REPOSITORY") == GC_REPO
        and environment.get("REPOSITORY_ID") == GC_ID,
        "PERSONAL_REPOSITORY_ONLY",
    )
    require(environment.get("REF") == "refs/heads/master", "DEFAULT_BRANCH_ONLY")
    require(environment.get("ACTOR") == ALLOWED_ACTOR, "MANUAL_OPERATOR_NOT_ALLOWED")
    return ident(environment.get("ATOMGIT_RUN_ID")), sha(environment.get("CONTROL_SHA"))


def branch(api, name):
    row = api.gc("/branches/" + urllib.parse.quote(ref(name), safe=""))
    require(row.get("name") == name, "BRANCH_IDENTITY_CHANGED")
    commit = row.get("commit") or {}
    value = sha(commit.get("id"))
    require("sha" not in commit or commit["sha"] == value, "BRANCH_SHA_CONFLICT")
    return value


def repository(api):
    row = api.gc()
    require(
        str(row.get("id")) == GC_ID
        and row.get("full_name") == GC_REPO
        and row.get("default_branch") == "master",
        "GITCODE_REPOSITORY_CHANGED",
    )


def release(api, control):
    github_repository = api.gh("")
    require(
        type(github_repository.get("id")) is int
        and github_repository["id"] == GH_ID
        and github_repository.get("full_name") == GH_REPO
        and github_repository.get("default_branch") == "master",
        "GITHUB_NUMERIC_REPOSITORY_CHANGED",
    )
    repository(api)
    require(branch(api, "master") == control, "GITCODE_CONTROL_CHANGED")
    row = api.gh("/branches/master")
    require(
        row.get("name") == "master" and (row.get("commit") or {}).get("sha") == control,
        "GITHUB_CONTROL_CHANGED",
    )
    workflow = api.gh(f"/actions/workflows/{GH_WORKFLOW_ID}")
    require(
        workflow.get("id") == GH_WORKFLOW_ID
        and workflow.get("path") == GH_WORKFLOW
        and workflow.get("state") == "active",
        "GITHUB_WORKFLOW_NOT_APPROVED",
    )


def pr_fields(row, number):
    require(
        row.get("number") == number
        and row.get("state") == "open"
        and not row.get("merged_at"),
        "PR_NOT_OPEN",
    )
    require(
        (row.get("user") or {}).get("login") == ALLOWED_ACTOR, "PR_AUTHOR_NOT_ALLOWED"
    )
    head, base = row.get("head") or {}, row.get("base") or {}
    for item in (head, base):
        repo = item.get("repo") or {}
        require(
            repo.get("full_name") == GC_REPO
            and ("id" not in repo or str(repo["id"]) == GC_ID),
            "PERSONAL_SOURCE_TARGET_ONLY",
        )
    require(base.get("ref") == "master", "PR_TARGET_NOT_MASTER")
    return {
        "head_ref": ref(head.get("ref")),
        "head_sha": sha(head.get("sha")),
        "base_ref": "master",
        "actor": ALLOWED_ACTOR,
    }


def snapshot(api, number):
    require(type(number) is int and 0 < number < 2**31, "INVALID_PR_NUMBER")
    repository(api)
    before = pr_fields(api.gc(f"/pulls/{number}"), number)
    head, base = branch(api, before["head_ref"]), branch(api, "master")
    after = pr_fields(api.gc(f"/pulls/{number}"), number)
    repository(api)
    require(before == after and head == after["head_sha"], "PR_CHANGED_DURING_CAPTURE")
    return {
        "repo_id": GC_ID,
        "number": number,
        "head_repo_id": GC_ID,
        "head_ref": after["head_ref"],
        "head_sha": head,
        "base_repo_id": GC_ID,
        "base_ref": "master",
        "base_sha": base,
    }


def run_identity(api, run_id, control):
    run = api.gc("/actions/runs/" + ident(run_id), version=8)
    expected = {
        "workflow_run_id": run_id,
        "file_path": GC_WORKFLOW,
        "head_sha": control,
        "head_branch": "master",
        "event": "Manual",
        "attempt": 1,
        "exist_in_default_branch": True,
    }
    require(
        all(type(run.get(k)) is type(v) and run[k] == v for k, v in expected.items()),
        "GITCODE_MANUAL_RUN_MISMATCH",
    )
    require(run.get("status") in ("RUNNING", "COMPLETED"), "GITCODE_RUN_NOT_LIVE")
    return run


def emit_failure(error):
    code = str(error) if isinstance(error, ManualForwardError) else type(error).__name__
    print("MANUAL_FORWARD_FAILED:" + code, file=sys.stderr)
    return 1
