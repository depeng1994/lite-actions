"""Read-only GitCode v5 authority adapter, verified against live response shapes.

PR head/base repository objects may omit numeric IDs. Resolve the configured
repository separately and reject external forks before following any PR URL.
A PR's cached base.sha is NOT the current target branch SHA. updated_at and
operation-log IDs are deliberately NOT presented as a proven version revision.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from ci_core.gitcode_contract import canonical, load_json, positive, validate_pr
from ci_core.gitcode_source import require_ref, require_sha

API_ROOT = "https://api.gitcode.com/api/v5"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class GitCodeAPIError(RuntimeError):
    def __init__(self, code: str, *, status: int = 0):
        self.code, self.status = code, status
        super().__init__(f"{code} (HTTP {status})" if status else code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a control credential to a redirect destination.
        return None


def repository_id(value) -> str:
    if type(value) is int:
        positive(value)
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        return value
    raise GitCodeAPIError("INVALID_GITCODE_REPOSITORY_ID")


class GitCodeClient:
    def __init__(
        self,
        repo: str,
        repo_id: str,
        *,
        token: str | None = None,
        transport: Callable[[str], dict | list] | None = None,
        timeout: int = 15,
        allowed_head_repositories: dict[str, str] | None = None,
    ):
        if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
            raise GitCodeAPIError("INVALID_GITCODE_REPOSITORY_PATH")
        self.repo, self.repo_id = repo, repository_id(repo_id)
        self.allowed_head_repositories = dict(allowed_head_repositories or {})
        ids = set()
        for path, ident in self.allowed_head_repositories.items():
            if not isinstance(path, str) or not REPOSITORY.fullmatch(path):
                raise GitCodeAPIError("INVALID_HEAD_REPOSITORY_BINDING")
            ident = repository_id(ident)
            if ident in ids or path == repo and ident != self.repo_id:
                raise GitCodeAPIError("CONFLICTING_HEAD_REPOSITORY_BINDING")
            self.allowed_head_repositories[path] = ident
            ids.add(ident)
        self._token = token
        self._transport = transport
        positive(timeout)
        self.timeout = min(timeout, 30)
        self._opener = urllib.request.build_opener(NoRedirect())
        if transport is None and (
            not isinstance(token, str) or not token or "\n" in token or "\r" in token
        ):
            raise GitCodeAPIError("GITCODE_READ_CREDENTIAL_REQUIRED")

    @classmethod
    def from_environment(
        cls, repo: str, repo_id: str, *, allowed_head_repositories=None
    ):
        token = os.environ.get("LITE_ACTIONS_GITCODE_TOKEN") or os.environ.get(
            "GITCODE_API_TOKEN"
        )
        return cls(
            repo,
            repo_id,
            token=token,
            allowed_head_repositories=allowed_head_repositories,
        )

    def __repr__(self):
        return f"GitCodeClient(repo={self.repo!r}, repo_id={self.repo_id!r})"

    def _get(
        self, path: str, *, expect_list: bool = False, api_version: int = 5
    ) -> dict | list:
        if type(api_version) is not int or api_version not in (5, 8):
            raise GitCodeAPIError("GITCODE_API_VERSION_NOT_ALLOWED")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "://" in path
            or ".." in path
            or "\\" in path
        ):
            raise GitCodeAPIError("UNSAFE_GITCODE_API_PATH")
        if self._transport is not None:
            result = self._transport(path if api_version == 5 else "/api/v8" + path)
            if not isinstance(result, list if expect_list else dict):
                raise GitCodeAPIError("INVALID_GITCODE_API_RESPONSE")
            return result
        for attempt in range(3):
            request = urllib.request.Request(
                f"https://api.gitcode.com/api/v{api_version}" + path,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "application/json",
                    "User-Agent": "lite-actions-gitcode-v4",
                },
                method="GET",
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    payload = response.read(4 * 1024**2 + 1)
                if len(payload) > 4 * 1024**2:
                    raise GitCodeAPIError("GITCODE_RESPONSE_SIZE_LIMIT")
                # Keep strict duplicate-key/non-finite checks for array responses.
                result = load_json(
                    b'{"response":' + payload + b"}", limit=4 * 1024**2 + 32
                )["response"]
                if not isinstance(result, list if expect_list else dict):
                    raise GitCodeAPIError("INVALID_GITCODE_API_RESPONSE")
                return result
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if status in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise GitCodeAPIError(
                    "GITCODE_AUTHORITY_HTTP_ERROR", status=status
                ) from None
            except (OSError, TimeoutError):
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                # Do not propagate a Request/exception with secret headers.
                raise GitCodeAPIError("GITCODE_AUTHORITY_UNREACHABLE") from None
        raise GitCodeAPIError("GITCODE_AUTHORITY_UNREACHABLE")

    def repository(self) -> dict:
        data = self._get("/repos/" + self.repo)
        if (
            repository_id(data.get("id")) != self.repo_id
            or data.get("full_name") != self.repo
        ):
            raise GitCodeAPIError("GITCODE_REPOSITORY_IDENTITY_CHANGED")
        default_ref = require_ref(data.get("default_branch"))
        return {
            "repo_id": self.repo_id,
            "repo": self.repo,
            "default_ref": default_ref,
            "clone_url": "https://gitcode.com/" + self.repo + ".git",
        }

    def branch(self, ref: str) -> str:
        require_ref(ref)
        data = self._get(
            "/repos/" + self.repo + "/branches/" + urllib.parse.quote(ref, safe="")
        )
        commit = data.get("commit")
        if data.get("name") != ref or not isinstance(commit, dict):
            raise GitCodeAPIError("GITCODE_BRANCH_IDENTITY_MISMATCH")
        sha = require_sha(commit.get("id"))
        if "sha" in commit and commit["sha"] != sha:
            raise GitCodeAPIError("GITCODE_COMMIT_IDENTITY_CONFLICT")
        return sha

    def pull(self, number: int) -> dict:
        positive(number)
        data = self._get(f"/repos/{self.repo}/pulls/{number}")
        if type(data.get("number")) is not int or data["number"] != number:
            raise GitCodeAPIError("GITCODE_PR_NUMBER_MISMATCH")
        return data

    def open_pull_numbers(self, *, max_pages: int = 10) -> list[int]:
        """Bounded discovery only; every PR still needs a fresh authority read.

        Pagination is not an atomic repository snapshot. A periodic caller must
        repeat scans; never interpret absence as closure or revoke on absence.
        """
        positive(max_pages)
        if max_pages > 100:
            raise GitCodeAPIError("GITCODE_DISCOVERY_BUDGET_EXCEEDED")
        metadata = self.repository()
        numbers = set()
        for page in range(1, max_pages + 1):
            rows = self._get(
                f"/repos/{self.repo}/pulls?state=open&sort=created"
                f"&direction=asc&per_page=100&page={page}",
                expect_list=True,
            )
            if len(rows) > 100:
                raise GitCodeAPIError("GITCODE_INVALID_DISCOVERY_PAGE")
            for row in rows:
                if not isinstance(row, dict):
                    raise GitCodeAPIError("GITCODE_INVALID_DISCOVERY_ROW")
                number = positive(row.get("number"))
                if row.get("state") != "open" or number in numbers:
                    raise GitCodeAPIError("GITCODE_DISCOVERY_CHANGED_DURING_SCAN")
                numbers.add(number)
            if len(rows) < 100:
                if self.repository() != metadata:
                    raise GitCodeAPIError("GITCODE_REPOSITORY_CHANGED_DURING_SCAN")
                return sorted(numbers)
        raise GitCodeAPIError("GITCODE_DISCOVERY_INCOMPLETE")

    def _head_client(self, path: str):
        if path == self.repo:
            return self
        ident = self.allowed_head_repositories.get(path)
        if ident is None:
            raise GitCodeAPIError("EXTERNAL_FORK_NOT_AUTHORIZED")
        return GitCodeClient(
            path,
            ident,
            token=self._token,
            transport=self._transport,
            timeout=self.timeout,
        )

    def repository_for_identity(self, identity: dict, role: str) -> dict:
        """Resolve a configured source URL by numeric identity, never PR URLs."""
        if role not in ("head", "base"):
            raise GitCodeAPIError("INVALID_SOURCE_ROLE")
        ident = identity[role + "_repo_id"]
        if (
            identity["repo_id"] != self.repo_id
            or identity["base_repo_id"] != self.repo_id
        ):
            raise GitCodeAPIError("TARGET_REPOSITORY_IDENTITY_MISMATCH")
        if ident == self.repo_id:
            return self.repository()
        if role != "head":
            raise GitCodeAPIError("TARGET_REPOSITORY_IDENTITY_MISMATCH")
        matches = [
            path
            for path, known in self.allowed_head_repositories.items()
            if known == ident
        ]
        if len(matches) != 1:
            raise GitCodeAPIError("EXTERNAL_FORK_NOT_AUTHORIZED")
        return self._head_client(matches[0]).repository()

    def _pull_identity(
        self,
        pr: dict,
        *,
        allowed_base_refs: tuple[str, ...],
        allowed_actors: tuple[str, ...],
    ):
        if pr.get("state") != "open" or pr.get("merged_at"):
            raise GitCodeAPIError("GITCODE_PR_NOT_OPEN")
        if (pr.get("user") or {}).get("login") not in allowed_actors:
            raise GitCodeAPIError("GITCODE_PR_ACTOR_NOT_AUTHORIZED")
        head, base = pr.get("head"), pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise GitCodeAPIError("GITCODE_PR_REFS_MISSING")
        base_repo, head_repo = base.get("repo"), head.get("repo")
        if not isinstance(base_repo, dict) or base_repo.get("full_name") != self.repo:
            raise GitCodeAPIError("TARGET_REPOSITORY_IDENTITY_MISMATCH")
        if not isinstance(head_repo, dict) or not isinstance(
            head_repo.get("full_name"), str
        ):
            raise GitCodeAPIError("EXTERNAL_FORK_NOT_AUTHORIZED")
        source = self._head_client(head_repo["full_name"])
        # Independent API identity resolution is mandatory when embedded IDs
        # are absent. Only configured repository paths can be requested.
        if source is not self:
            source.repository()
        for repo, expected in ((base_repo, self.repo_id), (head_repo, source.repo_id)):
            if "id" in repo and repository_id(repo["id"]) != expected:
                raise GitCodeAPIError("GITCODE_REPOSITORY_IDENTITY_CHANGED")
        require_ref(head.get("ref"))
        require_ref(base.get("ref"))
        require_sha(head.get("sha"))
        if not allowed_base_refs or base["ref"] not in allowed_base_refs:
            raise GitCodeAPIError("GITCODE_BASE_REF_NOT_AUTHORIZED")
        return {
            "head_ref": head["ref"],
            "head_sha": head["sha"],
            "head_repo_id": source.repo_id,
            "head_repo_path": source.repo,
            "base_ref": base["ref"],
            "actor": pr["user"]["login"],
        }

    def open_pull_snapshot(
        self,
        number: int,
        *,
        allowed_base_refs: tuple[str, ...],
        allowed_actors: tuple[str, ...],
    ) -> dict:
        """Read an open allowlisted-source PR and its CURRENT source/target heads.

        This brackets the branch reads with PR reads, but is not an atomic
        platform snapshot or a proof of unobserved ABA history. Callers must
        re-read at admission, launch and publication boundaries.
        """
        self.repository()
        before = self._pull_identity(
            self.pull(number),
            allowed_base_refs=allowed_base_refs,
            allowed_actors=allowed_actors,
        )
        head_sha = self._head_client(before["head_repo_path"]).branch(
            before["head_ref"]
        )
        base_sha = self.branch(before["base_ref"])
        after = self._pull_identity(
            self.pull(number),
            allowed_base_refs=allowed_base_refs,
            allowed_actors=allowed_actors,
        )
        self.repository()
        if canonical(before) != canonical(after) or head_sha != after["head_sha"]:
            raise GitCodeAPIError("GITCODE_PR_CHANGED_DURING_READ")
        identity = {
            "repo_id": self.repo_id,
            "number": number,
            "head_repo_id": after["head_repo_id"],
            "head_ref": after["head_ref"],
            "head_sha": head_sha,
            "base_repo_id": self.repo_id,
            "base_ref": after["base_ref"],
            "base_sha": base_sha,
        }
        validate_pr(
            identity,
            allowed_repo_id=self.repo_id,
            allowed_refs=allowed_base_refs,
            allowed_head_repo_ids=tuple(self.allowed_head_repositories.values()),
        )
        return {"identity": identity, "state": "open", "authorized": True}

    def action_run(self, run_id: str) -> dict:
        """Read actual v8 Action metadata; never fabricate missing PR/actor fields.

        Verified live response on 2026-11-02 uses uppercase event=PUSH and
        millisecond start_time, and omits actor, PR number and workflow source
        revision. This method is an observation, not event authorization.
        """
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", run_id
        ):
            raise GitCodeAPIError("INVALID_GITCODE_ACTION_RUN_ID")
        self.repository()
        run = self._get(f"/repos/{self.repo}/actions/runs/{run_id}", api_version=8)
        if run.get("workflow_run_id") != run_id:
            raise GitCodeAPIError("GITCODE_ACTION_RUN_IDENTITY_MISMATCH")
        positive(run.get("attempt"))
        positive(run.get("start_time"))
        require_sha(run.get("head_sha"))
        require_ref(run.get("head_branch"))
        for key in ("workflow_id", "file_path", "event", "status"):
            if not isinstance(run.get(key), str) or not 1 <= len(run[key]) <= 256:
                raise GitCodeAPIError("GITCODE_ACTION_METADATA_INVALID")
        if type(run.get("exist_in_default_branch")) is not bool:
            raise GitCodeAPIError("GITCODE_ACTION_DEFAULT_BRANCH_PROOF_MISSING")
        return {
            key: run[key]
            for key in (
                "workflow_run_id",
                "workflow_id",
                "file_path",
                "event",
                "status",
                "attempt",
                "head_sha",
                "head_branch",
                "start_time",
                "exist_in_default_branch",
            )
        }

    def authenticated_actor(self) -> str:
        data = self._get("/user")
        login = data.get("login")
        if not isinstance(login, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}", login
        ):
            raise GitCodeAPIError("GITCODE_AUTHENTICATED_ACTOR_REQUIRED")
        return login

    def authority_snapshot(self, number: int, *, allowed_base_refs, allowed_actors):
        """Observe closure/retarget as revocation, never as an executable snapshot."""
        self.repository()
        before = self.pull(number)
        state = "merged" if before.get("merged_at") else before.get("state")
        if state not in ("open", "closed", "merged"):
            raise GitCodeAPIError("GITCODE_UNKNOWN_PR_STATE")
        actor = (before.get("user") or {}).get("login")
        base_ref = (before.get("base") or {}).get("ref")
        # Validate repository/ref identity without granting actor/ref admission.
        normalized = {**before, "state": "open", "merged_at": None}
        identity = self._pull_identity(
            normalized, allowed_base_refs=(base_ref,), allowed_actors=(actor,)
        )
        head = (
            self._head_client(identity["head_repo_path"]).branch(identity["head_ref"])
            if state == "open"
            else identity["head_sha"]
        )
        base = self.branch(identity["base_ref"])
        after = self.pull(number)
        self.repository()

        # Ignore irrelevant comments/titles, but include all authorization fields.
        def fields(pr):
            return {
                key: pr.get(key)
                for key in ("state", "merged_at", "head", "base", "user")
            }

        if (
            canonical(fields(before)) != canonical(fields(after))
            or head != identity["head_sha"]
        ):
            raise GitCodeAPIError("GITCODE_PR_CHANGED_DURING_READ")
        pr = {
            "repo_id": self.repo_id,
            "number": number,
            "head_repo_id": identity["head_repo_id"],
            "head_ref": identity["head_ref"],
            "head_sha": head,
            "base_repo_id": self.repo_id,
            "base_ref": identity["base_ref"],
            "base_sha": base,
        }
        validate_pr(
            pr,
            allowed_repo_id=self.repo_id,
            allowed_head_repo_ids=tuple(self.allowed_head_repositories.values()),
        )
        return {
            "identity": pr,
            "state": state,
            "authorized": actor in allowed_actors and base_ref in allowed_base_refs,
        }

    def control_tip(self) -> dict:
        metadata = self.repository()
        sha = self.branch(metadata["default_ref"])
        if self.repository() != metadata:
            raise GitCodeAPIError("GITCODE_DEFAULT_BRANCH_CHANGED_DURING_READ")
        return {**metadata, "sha": sha}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only GitCode PR authority inspection; no dispatch or device access"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--base-ref", action="append", default=[])
    parser.add_argument("--actor", action="append", default=[])
    parser.add_argument(
        "--head-repository",
        action="append",
        default=[],
        metavar="PATH=ID",
        help="Explicit trusted source binding; never inferred from PR URLs",
    )
    args = parser.parse_args(argv)
    heads = {}
    for binding in args.head_repository:
        path, separator, ident = binding.partition("=")
        if not separator or path in heads:
            parser.error("--head-repository requires unique PATH=ID bindings")
        heads[path] = repository_id(ident)
    client = GitCodeClient.from_environment(
        args.repo, args.repo_id, allowed_head_repositories=heads
    )
    result = (
        client.control_tip()
        if args.pr is None
        else client.open_pull_snapshot(
            args.pr,
            allowed_base_refs=tuple(args.base_ref),
            allowed_actors=tuple(args.actor),
        )
    )
    print(
        json.dumps(
            {
                "observation": result,
                "event_revision_proof": "NOT_ESTABLISHED",
                "merge_gate": "NOT_PROVIDED",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
