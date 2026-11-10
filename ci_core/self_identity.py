"""Bind an opt-in self-smoke Worker to its clean, actual Git checkout."""

import hashlib
import re
import subprocess
from pathlib import Path


def checkout_sha(root):
    root = Path(root).resolve()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, timeout=15
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
        "tests/integration_tests/__init__.py",
        "tests/integration_tests/tools/__init__.py",
        "tests/integration_tests/tools/lite_actions",
        "tests/integration_tests/training_cases",
    )
    entries = git("ls-tree", "-rz", "--full-tree", "HEAD", "--", *paths)
    tracked = set()
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, expected = metadata.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError("unsafe Worker source tree")
        name = raw_path.decode()
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing Worker source file")
        content = path.read_bytes()
        blob = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content,
            usedforsecurity=False,
        ).hexdigest()
        if blob.encode() != expected:
            raise ValueError("Worker source differs from its commit")
        tracked.add(name)
    required = {
        "unified_router.py",
        ".github/workflows/lite_actions_self_smoke.yml",
        "tests/__init__.py",
        "tests/integration_tests/__init__.py",
        "tests/integration_tests/tools/__init__.py",
        "tests/integration_tests/tools/lite_actions/entrypoint.py",
        "tests/integration_tests/tools/lite_actions/dispatch_smoke_worker.py",
        "tests/integration_tests/tools/lite_actions/runner.py",
        "tests/integration_tests/training_cases/__init__.py",
        "tests/integration_tests/training_cases/dispatch_smoke.py",
    }
    if not required.issubset(tracked) or not any(
        x.startswith("ci_core/") for x in tracked
    ):
        raise ValueError("incomplete Worker source tree")
    directories = (
        root / "ci_core",
        root / "tests/integration_tests/tools/lite_actions",
        root / "tests/integration_tests/training_cases",
    )
    if any(
        str(path.relative_to(root)) not in tracked
        for directory in directories
        for path in directory.rglob("*.py")
    ):
        raise ValueError("untracked Worker Python source")
    return sha
