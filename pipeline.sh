#!/usr/bin/env bash
set -euo pipefail
echo "Dispatcher-Smoke: operating-system information probe (no NPU required)"
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
printf "Host: "; hostname -f
printf "Architecture: "; uname -m
printf "Kernel: "; uname -sr
echo "OS release (/etc/os-release):"
if [[ -r /etc/os-release ]]; then
  cat /etc/os-release
else
  echo "/etc/os-release is unavailable" >&2
  exit 1
fi
