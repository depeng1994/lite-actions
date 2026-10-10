#!/usr/bin/env python3
"""Compare/update non-authoritative Lite Actions fixtures from model repo.

The MODEL repository .github/ is the sole deployable source of truth; only
that repository's workflows run on GitHub. `--check` fails if any copy drifts.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parents[1]
FILES = (
    ('.github/scripts/lite_actions/wait_result.py','github/ci_wait_result.py'),
    ('.github/workflows/lite-actions-dispatcher-smoke.yml','github/workflows/lite-actions-dispatcher-smoke.yml'),
    ('.github/workflows/a3-8p-lite-actions.yml','github/workflows/a3-8p-lite-actions.yml'),
    ('.github/workflows/a3-16p-lite-actions.yml','github/workflows/a3-16p-lite-actions.yml'),
    ('.github/workflows/a5-64p-lite-actions.yml','github/workflows/a5-64p-lite-actions.yml'),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-repo', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = args.model_repo.resolve()
    for relative, mirror in FILES:
        actual = source / relative
        expected = HERE / mirror
        if not actual.is_file():
            raise FileNotFoundError(actual)
        if args.check:
            if not expected.is_file() or actual.read_bytes() != expected.read_bytes():
                print('CI MIRROR DRIFT:',mirror,file=sys.stderr)
                return 1
        else:
            expected.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(actual,expected)
    print(f'CI mirror {"matches" if args.check else "updated from"} model source: {source}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
