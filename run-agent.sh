#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs/system state
exec >> logs/system/agent.stdout.log 2>&1
exec flock -n state/agent.lock env A3_CI_POLL_SECONDS="${A3_CI_POLL_SECONDS:-60}" python3 -u agent.py
