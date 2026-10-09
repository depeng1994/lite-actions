#!/usr/bin/env bash
set -euo pipefail
echo "A3-2p-CI fixed pipeline: npu-smi"
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
echo "Host: $(hostname)"
echo "Arch: $(uname -m)"
echo "Executing: npu-smi info"
npu-smi info
