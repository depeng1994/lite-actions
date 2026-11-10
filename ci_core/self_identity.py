"""Bind an opt-in self-smoke Worker to its clean, actual Git checkout."""

import re
import subprocess
from pathlib import Path


def checkout_sha(root):
    root = Path(root).resolve()

    def git(*args, input=None):
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            input=input,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )

    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("self-smoke requires the actual Worker checkout")
    sha = git("rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid Worker checkout SHA")
    paths = (
        "ci_core",
        "unified_router.py",
        ".github/workflows/lite_actions_self_smoke.yml",
        "tests/__init__.py",
        "tests/integration_tests",
    )
    entries = git("ls-tree", "-rz", "--full-tree", "HEAD", "--", *paths)
    tracked = set()
    names, expected_blobs = [], []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, expected = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob" or b"\n" in raw_path:
            raise ValueError("unsafe Worker source tree")
        name = raw_path.decode()
        path = root / name
        if any(
            (root / part).is_symlink() for part in (Path(name), *Path(name).parents)
        ):
            raise ValueError("linked Worker source path")
        if not path.is_file():
            raise ValueError("missing Worker source file")
        names.append(raw_path)
        expected_blobs.append(expected)
        tracked.add(name)
    if "unified_router.py" not in tracked or not any(
        x.startswith("ci_core/") for x in tracked
    ):
        raise ValueError("incomplete Worker source tree")
    actual = git(
        "hash-object", "--no-filters", "--stdin-paths", input=b"\n".join(names) + b"\n"
    )
    if actual.splitlines() != expected_blobs:
        raise ValueError("Worker source differs from its commit")
    directories = (
        root / "ci_core",
        root / "tests/integration_tests",
    )
    for directory in directories:
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("linked Worker source path")
            if path.suffix == ".py" and str(path.relative_to(root)) not in tracked:
                raise ValueError("untracked Worker Python source")
    return sha
