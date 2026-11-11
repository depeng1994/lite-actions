"""Resolve a trusted remote ref and stage an exact fast-forward Git commit."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

SHA = re.compile(r"[0-9a-f]{40}\Z")
REF = re.compile(r"refs/heads/[A-Za-z0-9_./-]+\Z")


class SourceError(RuntimeError):
    """Do not include credentials or raw command stderr in this exception."""


def validate_source(url: str, ref: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "gitcode.com"}
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
    ):
        raise SourceError("untrusted_git_url")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise SourceError("untrusted_git_path")
    suffix = ref.removeprefix("refs/heads/")
    if (
        not REF.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or suffix.startswith(".")
        or suffix.endswith((".", "/", ".lock"))
    ):
        raise SourceError("invalid_git_ref")


def git(args: list[str], *, cwd: Path | None = None, timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError("git_command_failed") from exc
    return result.stdout.strip()


def check_source(url: str, ref: str) -> str:
    """Return the full target SHA; never evaluate remote content."""
    validate_source(url, ref)
    try:
        result = git(["ls-remote", "--exit-code", url, ref], timeout=35)
    except SourceError as exc:
        raise SourceError("source_unavailable") from exc
    rows = [line.split() for line in result.splitlines()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise SourceError("source_ref_mismatch")
    sha = rows[0][0]
    if not SHA.fullmatch(sha):
        raise SourceError("invalid_source_sha")
    return sha


def checkout_sha(path: Path) -> str:
    sha = git(["-C", str(path), "rev-parse", "--verify", "HEAD^{commit}"], timeout=15)
    if not SHA.fullmatch(sha):
        raise SourceError("invalid_checkout_sha")
    return sha


def stage_source(root: Path, url: str, ref: str, target: str, old: str) -> Path:
    """Clone into a private stage; verify both exact identity and ancestry."""
    validate_source(url, ref)
    if not SHA.fullmatch(target) or not SHA.fullmatch(old):
        raise SourceError("invalid_commit_sha")
    staging = root / "staging"
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=staging))
    branch = ref.removeprefix("refs/heads/")
    try:
        git(
            [
                "clone", "--quiet", "--no-checkout", "--single-branch",
                "--branch", branch, "--", url, str(stage),
            ],
            timeout=240,
        )
        git(["-C", str(stage), "checkout", "--quiet", "--detach", target], timeout=60)
        if checkout_sha(stage) != target:
            raise SourceError("download_sha_mismatch")
        try:
            git(["-C", str(stage), "cat-file", "-e", old + "^{commit}"], timeout=15)
        except SourceError:
            # Unshallow a clone or obtain the missing historical ancestor;
            # source URL stays the explicitly configured trusted endpoint.
            git(["-C", str(stage), "fetch", "--quiet", "origin", old], timeout=120)
        try:
            git(["-C", str(stage), "merge-base", "--is-ancestor", old, target], timeout=30)
        except SourceError as exc:
            raise SourceError("source_not_fast_forward") from exc
        if git(["-C", str(stage), "rev-parse", "--show-toplevel"], timeout=10) != str(stage):
            raise SourceError("unsafe_checkout")
        return stage
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
