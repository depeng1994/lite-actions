"""Content-checked local model archives; an unverified cache hit is not source proof."""

import hashlib
import json
import os
from pathlib import Path


def checksum(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def metadata_path(archive: Path) -> Path:
    return archive.with_name(archive.name + ".json")


def verified(archive: Path, repository: str, sha: str) -> bool:
    meta = metadata_path(archive)
    try:
        if (
            archive.is_symlink()
            or meta.is_symlink()
            or not archive.is_file()
            or archive.stat().st_size <= 0
            or not meta.is_file()
            or meta.stat().st_size > 4096
        ):
            return False
        value = json.loads(meta.read_text())
        return value == {
            "schema": 1,
            "repository": repository,
            "sha": sha,
            "size": archive.stat().st_size,
            "sha256": checksum(archive),
        }
    except (OSError, ValueError, TypeError):
        return False


def seal(archive: Path, repository: str, sha: str):
    meta = metadata_path(archive)
    temporary = meta.with_name(meta.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "repository": repository,
                    "sha": sha,
                    "size": archive.stat().st_size,
                    "sha256": checksum(archive),
                },
                sort_keys=True,
            )
            + "\n"
        )
        os.replace(temporary, meta)
    finally:
        temporary.unlink(missing_ok=True)
