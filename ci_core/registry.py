"""Resolve GitHub test selectors against trusted OverrideDefinitions at a fixed SHA.

Inputs arrive in a run/attempt/SHA-bound Artifact. Model code defines the
cases; deployment/runtime resource configuration stays with Lite Actions.
"""

from __future__ import annotations

import gzip
import json
import posixpath
import re
import tarfile
from pathlib import Path

SELECTOR = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


ASSET_ENV_KEYS = ("ASCEND_SET_ENV_PATH", "HF_ASSETS_PATH", "CKPT_INIT_LOAD_PATH")
RESERVED_RUNTIME_ENV = frozenset(
    (
        "NODE_IPS",
        "NGPU",
        "NNODES",
        "ASCEND_RT_VISIBLE_DEVICES",
        "LITE_CI_JOB_ID",
        "CKPT_SAVE_LOAD_PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "Network_Interface",
    )
)
CASE_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def validate_case_environment(env: object, *, kind: str = "train") -> dict[str, str]:
    """Validate trusted model-derived environment before SSH and preflight.

    An A5 case without configured assets remains discoverable with `inspect`,
    but cannot be executed until its real paths are defined in model source.
    """
    if not isinstance(env, dict):
        raise ValueError("model test env_vars must be a dictionary")
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not CASE_ENV_KEY.fullmatch(key)
            or key.startswith(("LD_", "DYLD_", "PYTHON", "GIT_", "BASH_FUNC_"))
            or key in {"BASH_ENV", "ENV", "SHELLOPTS", "PATH", "IFS"}
            or key in RESERVED_RUNTIME_ENV
            or not isinstance(value, str)
            or "\x00" in value
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError("invalid/forbidden test-owned environment variable")
    if kind not in {"train", "npu_smoke"}:
        raise ValueError("unsupported NPU execution kind")
    required = ASSET_ENV_KEYS if kind == "train" else ("ASCEND_SET_ENV_PATH",)
    for key in required:
        path = env.get(key)
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"model test must define an absolute {key} in env_vars")
    return env.copy()


CATALOG = ".ci/lite-actions.json"
MAX_CATALOG = 256 * 1024
MAX_SCAN = 512 * 1024 * 1024
MAX_MEMBERS = 65536
ROW_KEYS = {
    "test_id",
    "label",
    "nnodes",
    "ngpu",
    "timeout",
    "kind",
    "ci_overrides",
    "env_vars",
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CATALOG_INVALID: duplicate key")
        result[key] = value
    return result


def decode_catalog(data):
    """The catalog is data, not authority. Routing and environment approval follow."""
    if len(data) > MAX_CATALOG:
        raise ValueError("CATALOG_INVALID: size")
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
        if (
            type(doc) is not dict
            or set(doc) != {"version", "cases", "suites"}
            or type(doc["version"]) is not int
            or doc["version"] != 1
        ):
            raise ValueError
        cases, suites = doc["cases"], doc["suites"]
        if (
            not isinstance(cases, dict)
            or not 1 <= len(cases) <= 4096
            or not isinstance(suites, dict)
            or not 1 <= len(suites) <= 256
        ):
            raise ValueError
        for key, row in cases.items():
            if (
                not SELECTOR.fullmatch(key)
                or type(row) is not dict
                or set(row) != ROW_KEYS
                or row["test_id"] != key
            ):
                raise ValueError
            if (
                row["kind"] not in ("train", "npu_smoke")
                or not isinstance(row["label"], str)
                or not 1 <= len(row["label"]) <= 115
                or not row["label"].isprintable()
            ):
                raise ValueError
            for field, upper in [("ngpu", 256), ("nnodes", 16), ("timeout", 14400)]:
                if type(row[field]) is not int or not 1 <= row[field] <= upper:
                    raise ValueError
            overrides = row["ci_overrides"]
            if (
                not isinstance(overrides, dict)
                or set(overrides) - {"worker", "target_pool", "docker"}
                or not all(
                    isinstance(v, str) and 0 < len(v) <= 256 and v.isprintable()
                    for v in overrides.values()
                )
            ):
                raise ValueError
            validate_case_environment(row["env_vars"], kind=row["kind"])
        for suite, ids in suites.items():
            if (
                not SELECTOR.fullmatch(suite)
                or not isinstance(ids, list)
                or not 1 <= len(ids) <= 256
                or any(not isinstance(i, str) or i not in cases for i in ids)
            ):
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError("CATALOG_INVALID: schema") from exc
    return doc


class _BoundedInfo(tarfile.TarInfo):
    # tarfile consumes PAX/long-name payloads before exposing the next member.
    def _proc_member(self, tar):
        if self.size < 0 or self.size > MAX_SCAN or self.type == tarfile.GNUTYPE_SPARSE:
            raise ValueError("CATALOG_INVALID: tar metadata")
        if (
            self.type
            in (
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.GNUTYPE_LONGNAME,
                tarfile.GNUTYPE_LONGLINK,
                tarfile.SOLARIS_XHDTYPE,
            )
            and self.size > 65536
        ):
            raise ValueError("CATALOG_INVALID: extended header")
        return super()._proc_member(tar)


class _ScanReader:
    def __init__(self, source):
        self.source = source
        self.remaining = MAX_SCAN

    def read(self, size):
        chunk = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise ValueError("CATALOG_INVALID: scan budget")
        return chunk


def inspect_archive(archive: Path, selection: dict, *, repo=None) -> list[dict]:
    """Read one literal regular member; never extract or invoke model Python."""
    data = None
    try:
        with (
            gzip.open(archive, "rb") as raw,
            tarfile.open(
                fileobj=_ScanReader(raw), mode="r|", tarinfo=_BoundedInfo
            ) as tar,
        ):
            for count, member in enumerate(tar, 1):
                if count > MAX_MEMBERS or tar.offset > MAX_SCAN:
                    raise ValueError("CATALOG_INVALID: scan budget")
                path = posixpath.normpath("/" + member.name.replace("\\", "/")).lstrip(
                    "/"
                )
                if path == ".ci" and not member.isdir():
                    raise ValueError("CATALOG_INVALID: directory")
                if path != CATALOG:
                    continue
                if (
                    data is not None
                    or member.name != CATALOG
                    or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or member.sparse is not None
                    or not 0 < member.size <= MAX_CATALOG
                ):
                    raise ValueError("CATALOG_INVALID: member")
                with tar.extractfile(member) as stream:
                    data = stream.read(member.size)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError("CATALOG_INVALID: archive") from exc
    if data is None:
        raise ValueError("CATALOG_MISSING")
    doc = decode_catalog(data)
    if set(selection) == {"test_id"} and selection["test_id"] in doc["cases"]:
        return [doc["cases"][selection["test_id"]]]
    if set(selection) == {"suite"} and selection["suite"] in doc["suites"]:
        return [doc["cases"][i] for i in doc["suites"][selection["suite"]]]
    raise ValueError("CATALOG_INVALID: unknown selection")
