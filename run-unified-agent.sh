#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state logs/system
# The owner explicitly approved bounded reclamation of A3 auto-occupy/vllm.
# Operators may disable it with A3_CI_RECLAIM_EXTERNAL_PROCESSES=0.
export A3_CI_RECLAIM_EXTERNAL_PROCESSES="${A3_CI_RECLAIM_EXTERNAL_PROCESSES:-1}"
exec >>logs/system/unified-agent.stdout.log 2>&1
exec flock -n state/unified-agent.lock python3 -u unified_agent.py
