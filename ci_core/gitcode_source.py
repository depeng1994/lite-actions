"""Construct immutable GitCode merge sources without importing PR Python.

This module deliberately has no schema-3 archive fallback. Fetch URLs, Git
version and dependency exceptions must come from reviewed deployment policy,
not workflow inputs. The builder owns a fresh bare repository and an empty HOME.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

SHA = re.compile(r"[0-9a-f]{40}\Z")
MERGE_POLICY = "git-ort-v1"


class SourceError(ValueError):
    """A fail-closed source preparation error with a stable diagnostic code."""


def require_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SourceError("INVALID_COMMIT_SHA")
    return value


def require_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value)
        or any(x in value for x in ("..", "//", "@{"))
        or any(
            p.startswith(".") or p.endswith((".", ".lock")) for p in value.split("/")
        )
        or value.endswith("/")
    ):
        raise SourceError("INVALID_BRANCH_REF")
    return value


def safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or any(ord(c) < 32 for c in value)
        or path.is_absolute()
        or any(p in ("", ".", "..", ".git") for p in value.split("/"))
        or len(value.encode("utf-8")) > 4096
    ):
        raise SourceError("UNSAFE_ARCHIVE_PATH")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceHandle:
    """One verified source identity shared by staging, execution and recovery."""

    provider: str
    head_sha: str
    base_sha: str
    tested_merge_sha: str
    tested_tree_sha: str
    bundle_sha256: str
    bundle_path: str
    merge_policy: str
    git_version: str
    dependencies: tuple[dict, ...] = ()

    def metadata(self) -> dict:
        value = asdict(self)
        value.pop("bundle_path")
        value["dependencies"] = list(self.dependencies)
        return value

    def verify(self) -> Path:
        if self.provider != "gitcode" or self.merge_policy != MERGE_POLICY:
            raise SourceError("SOURCE_HANDLE_PROVIDER_MISMATCH")
        for sha in (
            self.head_sha,
            self.base_sha,
            self.tested_merge_sha,
            self.tested_tree_sha,
        ):
            require_sha(sha)
        path = Path(self.bundle_path)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", self.bundle_sha256)
            or path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != self.bundle_sha256
        ):
            raise SourceError("SOURCE_BUNDLE_DIGEST_MISMATCH")
        return path


class MergeSourceBuilder:
    """Build B+H using pinned Git, without checkout, hooks, drivers or credentials."""

    def __init__(
        self,
        root: Path,
        *,
        expected_git_version: str,
        allowed_urls: tuple[str, ...] = (),
        max_files: int = 100_000,
        max_unpacked_bytes: int = 2 * 1024**3,
        max_file_bytes: int = 64 * 1024**2,
        timeout_seconds: int = 600,
    ):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.repo = self.root / "objects.git"
        self.max_files = max_files
        self.max_unpacked_bytes = max_unpacked_bytes
        self.max_file_bytes = max_file_bytes
        self.timeout_seconds = timeout_seconds
        self.expires_at = time.monotonic() + timeout_seconds
        self.allowed_urls = frozenset(allowed_urls)
        self.git = shutil.which("git")
        if not self.git:
            raise SourceError("GIT_UNAVAILABLE")
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git_version = (
            self._git("--version").decode().strip().removeprefix("git version ")
        )
        if self.git_version != expected_git_version:
            raise SourceError("GIT_VERSION_NOT_PINNED")
        for url in self.allowed_urls:
            self._check_url(url)
        self._git("init", "--bare", "--template=", str(self.repo))

    @staticmethod
    def _check_url(url: str) -> None:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "gitcode.com"
            or parts.query
            or parts.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", parts.path)
        ):
            raise SourceError("UNTRUSTED_GIT_REMOTE")

    def _git(
        self,
        *args: str,
        data: bytes | None = None,
        extra_env: dict | None = None,
        allow_conflict: bool = False,
        max_output: int = 16 * 1024**2,
    ) -> bytes:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise SourceError("SOURCE_BUILD_DEADLINE_EXPIRED")
        command = [
            self.git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "http.followRedirects=false",
            "-c",
            "core.attributesFile=/dev/null",
        ]
        if self.repo.exists():
            command += ["--git-dir=" + str(self.repo)]
        command += list(args)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                env={**self.env, **(extra_env or {})},
                start_new_session=True,
            )
            try:
                process.communicate(data, timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise SourceError("GIT_OPERATION_TIMEOUT") from exc
            if process.returncode:
                code = (
                    "MERGE_CONFLICT"
                    if allow_conflict and process.returncode == 1
                    else "GIT_OPERATION_FAILED"
                )
                # Do not print command output: fetched commit metadata is untrusted.
                raise SourceError(code)
            if out.tell() > max_output:
                raise SourceError("GIT_OUTPUT_LIMIT")
            out.seek(0)
            return out.read(max_output + 1)

    def fetch_ref(self, *, url: str, ref: str, expected_sha: str, role: str) -> None:
        self._check_url(url)
        if url not in self.allowed_urls or role not in ("head", "base"):
            raise SourceError("UNTRUSTED_GIT_REMOTE")
        require_ref(ref)
        require_sha(expected_sha)
        target = "refs/heads/input-" + role
        self._git(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            url,
            "+refs/heads/" + ref + ":" + target,
        )
        if self._git("rev-parse", "--verify", target).decode().strip() != expected_sha:
            raise SourceError("FETCHED_REF_SHA_MISMATCH")

    def build(
        self,
        *,
        head_sha: str,
        base_sha: str,
        optional_gitlinks: dict[str, str] | None = None,
    ) -> SourceHandle:
        head_sha, base_sha = require_sha(head_sha), require_sha(base_sha)
        if (self.repo / "shallow").exists():
            raise SourceError("SHALLOW_HISTORY_UNSUPPORTED")
        for sha in (base_sha, head_sha):
            if self._git("cat-file", "-t", sha).strip() != b"commit":
                raise SourceError("SOURCE_NOT_COMMIT")
        # Requiring a merge base rejects unrelated and incomplete histories.
        self._git("merge-base", base_sha, head_sha)
        tree = (
            self._git(
                "merge-tree", "--write-tree", base_sha, head_sha, allow_conflict=True
            )
            .decode()
            .splitlines()[0]
        )
        require_sha(tree)
        timestamp = (
            max(
                int(self._git("show", "-s", "--format=%ct", sha))
                for sha in (base_sha, head_sha)
            )
            + 1
        )
        identity = {
            "GIT_AUTHOR_NAME": "Lite Actions Merge Builder",
            "GIT_AUTHOR_EMAIL": "ci@lite-actions.invalid",
            "GIT_COMMITTER_NAME": "Lite Actions Merge Builder",
            "GIT_COMMITTER_EMAIL": "ci@lite-actions.invalid",
            "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
        }
        parents = ["-p", base_sha] + (["-p", head_sha] if head_sha != base_sha else [])
        message = f"lite-actions merge-result\npolicy={MERGE_POLICY}\nbase={base_sha}\nhead={head_sha}\n"
        merge = (
            self._git(
                "commit-tree", tree, *parents, data=message.encode(), extra_env=identity
            )
            .decode()
            .strip()
        )
        require_sha(merge)
        bundle = self.root / "source.tar.gz"
        if bundle.exists():
            raise SourceError("SOURCE_ALREADY_BUILT")
        dependencies = self._archive_tree(tree, bundle, optional_gitlinks or {})
        return SourceHandle(
            "gitcode",
            head_sha,
            base_sha,
            merge,
            tree,
            sha256_file(bundle),
            str(bundle),
            MERGE_POLICY,
            self.git_version,
            tuple(dependencies),
        )

    def _archive_tree(
        self, tree: str, output: Path, optional_gitlinks: dict[str, str]
    ) -> list[dict]:
        """Archive literal blobs: git archive honors PR-controlled export-ignore/subst."""
        raw = self._git("ls-tree", "-rz", "--full-tree", tree)
        records = [entry for entry in raw.split(b"\0") if entry]
        if len(records) > self.max_files:
            raise SourceError("SOURCE_FILE_LIMIT")
        dependencies: list[dict] = []
        seen_dependencies = set()
        total = 0
        temporary = output.with_suffix(".partial")
        try:
            with (
                temporary.open("xb") as stream,
                gzip.GzipFile(
                    filename="", fileobj=stream, mode="wb", mtime=0
                ) as zipped,
            ):
                with tarfile.open(
                    fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT
                ) as archive:
                    for record in records:
                        meta, name = record.split(b"\t", 1)
                        mode, kind, oid = meta.decode().split()
                        name = name.decode("utf-8", errors="strict")
                        safe_path(name)
                        if kind == "commit" and mode == "160000":
                            if optional_gitlinks.get(name) != oid:
                                raise SourceError("UNMATERIALIZED_DEPENDENCY")
                            seen_dependencies.add(name)
                            dependencies.append(
                                {
                                    "path": name,
                                    "kind": "gitlink",
                                    "sha": oid,
                                    "materialized": False,
                                }
                            )
                            continue
                        if kind != "blob" or mode not in ("100644", "100755"):
                            # Conservative initial policy: no symlinks, devices or hardlinks.
                            raise SourceError("UNSUPPORTED_SOURCE_FILE_MODE")
                        size = int(self._git("cat-file", "-s", oid))
                        total += size
                        if (
                            size > self.max_file_bytes
                            or total > self.max_unpacked_bytes
                        ):
                            raise SourceError("SOURCE_SIZE_LIMIT")
                        content = self._git(
                            "cat-file", "blob", oid, max_output=self.max_file_bytes
                        )
                        if (
                            content.partition(b"\n")[0].rstrip(b"\r")
                            == b"version https://git-lfs.github.com/spec/v1"
                        ):
                            raise SourceError("UNMATERIALIZED_LFS_OBJECT")
                        info = tarfile.TarInfo(name)
                        info.size = size
                        info.mode = 0o755 if mode == "100755" else 0o644
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(content))
            if seen_dependencies != set(optional_gitlinks):
                raise SourceError("DEPENDENCY_DECLARATION_MISMATCH")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return dependencies


class BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized PAX/GNU metadata before tarfile allocates its body."""

    def _proc_pax(self, archive):
        if self.size > 64 * 1024:
            raise SourceError("SOURCE_ARCHIVE_METADATA_LIMIT")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 64 * 1024:
            raise SourceError("SOURCE_ARCHIVE_METADATA_LIMIT")
        return super()._proc_gnulong(archive)


def extract_verified_source(
    handle: SourceHandle,
    destination: Path,
    *,
    max_files: int = 100_000,
    max_unpacked_bytes: int = 2 * 1024**3,
    max_bundle_bytes: int = 512 * 1024**2,
) -> Path:
    """Extract into a new private directory, atomically or not at all.

    Reject all links and special files. Cache hits must call handle.verify()
    again. This function never imports source code or chooses another provider.
    """
    return extract_verified_archive(
        handle.verify(),
        handle.bundle_sha256,
        destination,
        max_files=max_files,
        max_unpacked_bytes=max_unpacked_bytes,
        max_bundle_bytes=max_bundle_bytes,
    )


def extract_verified_archive(
    bundle: Path,
    expected_sha256: str,
    destination: Path,
    *,
    max_files: int = 100_000,
    max_unpacked_bytes: int = 2 * 1024**3,
    max_bundle_bytes: int = 512 * 1024**2,
) -> Path:
    """Shared safe extraction for source and independently pinned runner bundles."""
    bundle = Path(bundle)
    if (
        not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or bundle.is_symlink()
        or not bundle.is_file()
        or sha256_file(bundle) != expected_sha256
    ):
        raise SourceError("SOURCE_BUNDLE_DIGEST_MISMATCH")
    if bundle.stat().st_size > max_bundle_bytes:
        raise SourceError("SOURCE_BUNDLE_SIZE_LIMIT")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise SourceError("SOURCE_DESTINATION_EXISTS")
    scratch = Path(tempfile.mkdtemp(prefix=".source-", dir=destination.parent))
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(bundle, "r|gz", tarinfo=BoundedTarInfo) as archive:
            for member in archive:
                path = safe_path(member.name)
                if member.name in seen or len(seen) >= max_files:
                    raise SourceError("SOURCE_DUPLICATE_OR_FILE_LIMIT")
                seen.add(member.name)
                total += member.size
                if member.size < 0 or total > max_unpacked_bytes:
                    raise SourceError("SOURCE_SIZE_LIMIT")
                target = scratch.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                elif member.isfile() and not member.issparse():
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    incoming = archive.extractfile(member)
                    if incoming is None:
                        raise SourceError("SOURCE_FILE_UNREADABLE")
                    with incoming, target.open("xb") as out:
                        shutil.copyfileobj(incoming, out, length=1024 * 1024)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                else:
                    raise SourceError("UNSAFE_ARCHIVE_MEMBER")
        scratch.rename(destination)
        return destination
    except (tarfile.TarError, OSError, UnicodeError) as exc:
        raise SourceError("SOURCE_EXTRACTION_FAILED") from exc
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
