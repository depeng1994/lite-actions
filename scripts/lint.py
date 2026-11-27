#!/usr/bin/env python3
"""Run Ruff and enforce compact physical-device arrays in pool configuration."""

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ci_core.permissions import PRIVATE_UMASK

DEVICE_ARRAY = re.compile(r'"(?:device_ids|devices)"\s*:\s*(\[[^\]]*\])')


def device_layout(path: Path, *, fix=False) -> bool:
    text = path.read_text()
    json.loads(text)
    matches = [m for m in DEVICE_ARRAY.finditer(text) if "\n" in m.group(1)]
    if fix:
        for match in reversed(matches):
            compact = json.dumps(json.loads(match.group(1)))
            text = text[: match.start(1)] + compact + text[match.end(1) :]
        path.write_text(text)
        return True
    for match in matches:
        line = text.count("\n", 0, match.start(1)) + 1
        print(
            f"{path}:{line}: Device IDs must be on one line; run python3 scripts/lint.py --fix",
            file=sys.stderr,
        )
    return not matches


def shared_file_modes() -> bool:
    """Keep Worker/updater octal modes in their single policy module."""
    valid = True
    paths = [ROOT / "unified_router.py"]
    for package in ("ci_core", "auto_update"):
        paths.extend((ROOT / package).glob("*.py"))
    for path in paths:
        if path == ROOT / "ci_core/permissions.py":
            continue
        for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if token.type == tokenize.NUMBER and token.string.lower().startswith("0o"):
                print(
                    f"{path}:{token.start[0]}: use ci_core.permissions constants",
                    file=sys.stderr,
                )
                valid = False
    for path in (ROOT / "deploy").glob("*.service"):
        for line in path.read_text().splitlines():
            if line.startswith("UMask=") and line != f"UMask={PRIVATE_UMASK:04o}":
                print(
                    f"{path}: bootstrap UMask must match PRIVATE_UMASK", file=sys.stderr
                )
                valid = False
    return valid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    failed = not shared_file_modes()
    failed |= not device_layout(ROOT / "config/pools.json", fix=args.fix)
    for command in [
        [sys.executable, "-m", "ruff", "check", *(["--fix"] if args.fix else []), "."],
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            *([] if args.fix else ["--check"]),
            ".",
        ],
    ]:
        failed |= subprocess.run(command, cwd=ROOT, check=False).returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
