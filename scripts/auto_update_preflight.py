#!/usr/bin/env python3
"""Read-only preflight for installing the Lite Actions automatic updater."""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
main = importlib.import_module("auto_update.preflight").main


if __name__ == "__main__":
    raise SystemExit(main())
