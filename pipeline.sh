#!/usr/bin/env bash
set -euo pipefail
echo "Dispatcher-Smoke: local npu-smi connectivity only (no 2P training)"
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
echo "Host: $(hostname)"
echo "Arch: $(uname -m)"
echo "Executing: npu-smi info"
npu-smi info
