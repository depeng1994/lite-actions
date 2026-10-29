#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state logs/system
# External auto-occupy/vLLM reclamation requires per-pool approval.
# Operators can disable it globally with LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES=0.
export LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES="${LITE_ACTIONS_RECLAIM_EXTERNAL_PROCESSES:-1}"
exec >>logs/system/unified-agent.stdout.log 2>&1
exec flock -n state/unified-agent.lock python3 -u unified_agent.py
