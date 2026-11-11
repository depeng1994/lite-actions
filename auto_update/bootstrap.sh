#!/usr/bin/env bash
set -euo pipefail

# Resolve the updater before current changes; never import from a moving symlink.
ROOT="$(printenv LITE_ACTIONS_UPDATE_ROOT || true)"
if [[ -z "$ROOT" ]]; then ROOT="/opt/lite-actions"; fi
if [[ "$ROOT" != /* || ! -d "$ROOT" ]]; then
  echo "Auto Update: invalid installation root; check LITE_ACTIONS_UPDATE_ROOT." >&2
  exit 2
fi
candidate="$(readlink -f "$ROOT/current" || true)"
if [[ -n "$candidate" && -f "$candidate/auto_update/update.py" ]]; then
  code_dir="$candidate"
else
  code_dir="$ROOT/bootstrap/seed"
fi
if [[ ! -f "$code_dir/auto_update/update.py" ]]; then
  echo "Auto Update: updater code not found in current release or bootstrap seed." >&2
  exit 2
fi

check_python() {
  local candidate="$1"
  [[ "$candidate" == /* && -f "$candidate" && -x "$candidate" ]] || return 1
  local version
  version="$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || return 1
  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || {
    echo "Auto Update: Python 3.11+ required; $candidate is $version." >&2
    return 2
  }
  printf '%s\n' "$candidate"
  return 0
}

PYTHON=""
configured_python="$(printenv LITE_ACTIONS_UPDATE_PYTHON || true)"
if [[ -n "$configured_python" ]]; then
  if [[ "$configured_python" != /* ]]; then
    echo "Auto Update: LITE_ACTIONS_UPDATE_PYTHON must be an absolute path; configure /etc/lite-actions/auto-update.env." >&2
    exit 2
  fi
  if ! PYTHON="$(check_python "$configured_python")"; then
    echo "Auto Update: set LITE_ACTIONS_UPDATE_PYTHON to an executable Python 3.11+ path, e.g. /usr/local/bin/python3.12." >&2
    exit 2
  fi
else
  for name in python3 python3.11 python3.12; do
    candidate="$(command -v "$name" 2>/dev/null || true)"
    [[ -n "$candidate" ]] || continue
    if PYTHON="$(check_python "$candidate" 2>/dev/null)"; then
      break
    fi
  done
  if [[ -z "$PYTHON" ]]; then
    echo "Auto Update: no Python 3.11+ interpreter found. Install one or set LITE_ACTIONS_UPDATE_PYTHON to an absolute path such as /usr/local/bin/python3.12." >&2
    exit 2
  fi
fi

cd "$code_dir"
exec "$PYTHON" -u -m auto_update.update "$@"
