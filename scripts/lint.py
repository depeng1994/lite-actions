#!/usr/bin/env python3
"""Run Ruff and enforce compact physical-device arrays in pool configuration."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args()
    failed = not device_layout(ROOT / "config/pools.json", fix=args.fix)
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
