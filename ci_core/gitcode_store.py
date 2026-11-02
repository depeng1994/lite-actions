"""Control-domain persistence for accepted schema-4 requests.

Result delivery only needs retained immutable metadata. Source staging separately
requires both pinned bundles to exist and pass their checksums. Neither operation
fetches an Artifact or imports proposed model Python. Cleanup uses the independent
Coordinator lease bindings, not this store or source materialization.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ci_core.gitcode_artifacts import BUNDLE_LIMIT, LoadedRequest
from ci_core.gitcode_contract import (
    VerifiedRequest,
    canonical,
    load_json,
    request_id,
    seal_plan,
    shape,
    text,
)
from ci_core.gitcode_source import SourceHandle, sha256_file


class StoreError(ValueError):
    pass


@dataclass(frozen=True)
class StoredExecution:
    verified: VerifiedRequest
    source: SourceHandle
    runner_path: Path

    def verify_files(self):
        self.source.verify()
        if (
            self.runner_path.is_symlink()
            or not self.runner_path.is_file()
            or not 0 < self.runner_path.stat().st_size <= BUNDLE_LIMIT
            or sha256_file(self.runner_path)
            != self.verified.plan["control"]["runner_sha256"]
        ):
            raise StoreError("STORED_RUNNER_CHANGED_OR_MISSING")


class GitCodeRequestStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise StoreError("REQUEST_STORE_ROOT_CANNOT_BE_SYMLINK")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise StoreError("REQUEST_STORE_DIRECTORY_REQUIRED")

    def _directory(self, rid):
        text(rid, limit=160)
        name = hashlib.sha256(rid.encode()).hexdigest()
        result = self.root / name
        if result.is_symlink():
            raise StoreError("REQUEST_STORE_ENTRY_CANNOT_BE_SYMLINK")
        return result

    @staticmethod
    def _write(path: Path, data: bytes):
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)

    @staticmethod
    def _sync(directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_source_identity(plan: dict, source: SourceHandle):
        if (
            source.provider != "gitcode"
            or source.head_sha != plan["pr"]["head_sha"]
            or source.base_sha != plan["pr"]["base_sha"]
            or source.tested_merge_sha != plan["merge"]["tested_merge_sha"]
            or source.tested_tree_sha != plan["merge"]["tested_tree_sha"]
            or source.bundle_sha256 != plan["artifacts"]["source"]["content_sha256"]
            or source.merge_policy != plan["merge"]["policy"]
            or "git-" + source.git_version != plan["merge"]["builder_version"]
        ):
            raise StoreError("STORED_SOURCE_PLAN_MISMATCH")

    def save(self, loaded: LoadedRequest) -> VerifiedRequest:
        if not isinstance(loaded, LoadedRequest):
            raise StoreError("LOADED_VERIFIED_REQUEST_REQUIRED")
        loaded.verify_files()
        plan = loaded.verified.plan
        if canonical(seal_plan(plan)) != canonical(plan):
            raise StoreError("REQUEST_STORE_PLAN_DIGEST_MISMATCH")
        self._validate_source_identity(plan, loaded.source)
        source = loaded.source.metadata()
        source.pop("bundle_path", None)
        document = {
            "schema": 1,
            "request_id": request_id(plan),
            "plan": plan,
            "source": source,
        }
        encoded = canonical(document)
        target = self._directory(document["request_id"])
        if target.exists():
            previous = self._document(document["request_id"], plan["plan_digest"])
            if canonical(previous) != encoded:
                raise StoreError("REQUEST_STORE_BINDING_IMMUTABLE")
            return VerifiedRequest(canonical(previous["plan"]))
        temporary = Path(tempfile.mkdtemp(prefix=".saving-", dir=self.root))
        try:
            self._write(temporary / "request.json", encoded)
            for name, origin, expected in (
                ("source.tar.gz", loaded.source.verify(), loaded.source.bundle_sha256),
                ("runner.tar.gz", loaded.runner_path, plan["control"]["runner_sha256"]),
            ):
                if origin.is_symlink() or not 0 < origin.stat().st_size <= BUNDLE_LIMIT:
                    raise StoreError("REQUEST_STORE_BUNDLE_SIZE_OR_TYPE_INVALID")
                with origin.open("rb") as src, (temporary / name).open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                (temporary / name).chmod(0o600)
                if sha256_file(temporary / name) != expected:
                    raise StoreError("BUNDLE_CHANGED_DURING_PERSISTENCE")
            self._sync(temporary)
            try:
                os.rename(temporary, target)
            except OSError:
                # A concurrent writer may have won. Only identical immutable
                # content is accepted; never replace an existing directory.
                previous = self._document(document["request_id"], plan["plan_digest"])
                if canonical(previous) != encoded:
                    raise StoreError("REQUEST_STORE_BINDING_IMMUTABLE") from None
            self._sync(self.root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return self.load_plan(document["request_id"], plan["plan_digest"])

    def _document(self, rid: str, expected_digest: str) -> dict:
        directory = self._directory(rid)
        path = directory / "request.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            raise StoreError("REQUEST_METADATA_MISSING_OR_UNSAFE")
        document = load_json(path.read_bytes())
        shape(document, {"schema", "request_id", "plan", "source"})
        plan = document["plan"]
        if (
            type(document["schema"]) is not int
            or document["schema"] != 1
            or document["request_id"] != rid
            or request_id(plan) != rid
            or plan["plan_digest"] != expected_digest
            or canonical(seal_plan(plan)) != canonical(plan)
        ):
            raise StoreError("PERSISTED_REQUEST_IDENTITY_MISMATCH")
        return document

    def load_plan(self, rid: str, expected_digest: str) -> VerifiedRequest:
        return VerifiedRequest(canonical(self._document(rid, expected_digest)["plan"]))

    def load_execution(self, rid: str, expected_digest: str) -> StoredExecution:
        document = self._document(rid, expected_digest)
        directory = self._directory(rid)
        source = SourceHandle(
            **{
                **document["source"],
                "dependencies": tuple(document["source"]["dependencies"]),
                "bundle_path": str(directory / "source.tar.gz"),
            }
        )
        plan = document["plan"]
        self._validate_source_identity(plan, source)
        result = StoredExecution(
            VerifiedRequest(canonical(plan)), source, directory / "runner.tar.gz"
        )
        result.verify_files()
        return result
