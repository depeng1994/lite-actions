#!/usr/bin/env python3
"""Publish a Control Center frontend archive received on stdin."""

import argparse
import fcntl
import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

MAX_RELEASE_BYTES = 64 * 1024 * 1024


def stage_release(stream, stage):
    payload = stream.read(MAX_RELEASE_BYTES + 1)
    if len(payload) > MAX_RELEASE_BYTES:
        raise ValueError("compressed frontend archive is too large")
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
        release = compressed.read(MAX_RELEASE_BYTES + 1)
    if len(release) > MAX_RELEASE_BYTES:
        raise ValueError("expanded frontend archive is too large")
    assets = stage / "assets"
    assets.mkdir()
    with tarfile.open(fileobj=io.BytesIO(release), mode="r:") as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if member.isdir() and member.name == "assets" and member.size == 0:
                continue
            is_asset = len(path.parts) == 2 and path.parts[0] == "assets"
            if (
                not member.isfile()
                or path.as_posix() != member.name
                or ".." in path.parts
                or (member.name != "index.html" and not is_asset)
                or member.size < 0
            ):
                raise ValueError(f"invalid frontend archive entry: {member.name!r}")
            target = stage.joinpath(*path.parts)
            with bundle.extractfile(member) as source, target.open("xb") as dest:
                shutil.copyfileobj(source, dest)
            target.chmod(0o644)
    if not (stage / "index.html").stat().st_size or not any(assets.iterdir()):
        raise ValueError("frontend archive must contain index.html and assets")


def publish_web(stream, dist):
    with (
        (dist.parent / ".web-publish.lock").open("a") as lock,
        tempfile.TemporaryDirectory(prefix=".web-publish-", dir=dist.parent) as tmp,
    ):
        stage = Path(tmp)
        stage_release(stream, stage)
        fcntl.flock(lock, fcntl.LOCK_EX)
        assets = dist / "assets"
        assets.mkdir(exist_ok=True)
        for file in (stage / "assets").iterdir():
            target = assets / file.name
            if target.exists():
                if target.read_bytes() != file.read_bytes():
                    raise ValueError(f"frontend asset name already in use: {file.name}")
            else:
                os.replace(file, target)
        index = dist / "index.html"
        candidate = stage / "index.html"
        if not index.exists() or index.read_bytes() != candidate.read_bytes():
            if index.exists():
                backup = stage / "previous-index.html"
                shutil.copyfile(index, backup)
                os.replace(backup, dist.parent / ".index-before-web-publish.html")
            os.replace(candidate, index)
        return hashlib.sha256(index.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(publish_web(sys.stdin.buffer, args.dist_dir))
    except (OSError, EOFError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f"Could not publish Control Center frontend: {exc}\n")


if __name__ == "__main__":
    main()
