#!/usr/bin/env python3
"""Read-only preflight for installing the Lite Actions automatic updater."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auto_update.preflight import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
