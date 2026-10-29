#!/usr/bin/env bash
set -euo pipefail
echo "Dispatcher-Smoke: operating-system information probe (no NPU required)"
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
printf 'WORKER_HOSTNAME='
hostname -f
printf 'WORKER_ARCH='
uname -m
printf 'WORKER_KERNEL='
uname -sr
printf 'OS_RELEASE_BEGIN\n'
if [[ -r /etc/os-release ]]; then
  cat /etc/os-release
else
  echo "/etc/os-release is unavailable" >&2
  exit 1
fi
printf 'OS_RELEASE_END\n'
printf 'WORKER_PYTHON='
python3 --version
