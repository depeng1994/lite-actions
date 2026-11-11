#!/usr/bin/env bash
set -euo pipefail

# Resolve the updater before current changes; never import from a moving symlink.
ROOT="$(printenv LITE_ACTIONS_UPDATE_ROOT || true)"
if [[ -z "$ROOT" ]]; then ROOT="/opt/lite-actions"; fi
if [[ "$ROOT" != /* || ! -d "$ROOT" ]]; then
  echo "Invalid installation root" >&2
  exit 2
fi
candidate="$(readlink -f "$ROOT/current" || true)"
if [[ -n "$candidate" && -f "$candidate/auto_update/update.py" ]]; then
  code_dir="$candidate"
else
  code_dir="$ROOT/bootstrap/seed"
fi
if [[ ! -f "$code_dir/auto_update/update.py" ]]; then
  echo "No validated updater code available" >&2
  exit 2
fi
cd "$code_dir"
exec /usr/bin/python3 -u -m auto_update.update "$@"
