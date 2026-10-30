#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state logs/system
exec python3 -u unified_router.py "$@"
