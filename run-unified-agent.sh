#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state logs/system
exec >>logs/system/unified-agent.stdout.log 2>&1
exec flock -n state/unified-agent.lock python3 -u unified_agent.py
