#!/usr/bin/env python3
"""Author-side tool. Run only in a disposable, credential-free model checkout, never on a Worker."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_core.registry import CATALOG, ROW_KEYS, SELECTOR, decode_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--suite",
        action="append",
        help="Repeat for every active suite. Default: dispatch_smoke",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    cases = {}
    suites = {}
    for suite in args.suite or ["dispatch_smoke"]:
        if not SELECTOR.fullmatch(suite) or suite in suites:
            parser.error("invalid/duplicate suite")
        out = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "tests.integration_tests.tools.lite_actions.entrypoint",
                "inspect",
                "--suite",
                suite,
            ],
            cwd=root,
            env={
                "PATH": os.defpath,
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            timeout=45,
        )
        rows = json.loads(out)
        ids = []
        for row in rows:
            # Model tooling may also export display-only fields (device, phases, etc.).
            # Do not enlarge the Worker wire schema or execute those fields.
            row = {k: row[k] for k in ROW_KEYS}
            if row["kind"] == "worker_probe":
                continue  # Infra probes are never model-defined.
            key = row["test_id"]
            if key in cases and cases[key] != row:
                parser.error("conflicting definitions")
            cases[key] = row
            ids.append(key)
        if ids:
            suites[suite] = ids
    data = (
        json.dumps(
            {"version": 1, "cases": cases, "suites": suites},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode()
    decode_catalog(data)
    path = root / CATALOG
    if args.check:
        if not path.is_file() or path.read_bytes() != data:
            parser.exit(1, "catalog drift\n")
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    main()
