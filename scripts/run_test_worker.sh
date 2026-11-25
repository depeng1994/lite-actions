#!/usr/bin/env bash
set -euo pipefail

ZONE=""
WORKER_ID=""
STATE_DIR=""
BROKER_SOCK=""
READY_FILE=""
POOLS_FILE=""
CODE_DIR=""
PYTHON_BIN="${PYTHON_BIN:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone)
      ZONE="${2:?missing --zone value}"
      shift 2
      ;;
    --worker-id)
      WORKER_ID="${2:?missing --worker-id value}"
      shift 2
      ;;
    --state-dir)
      STATE_DIR="${2:?missing --state-dir value}"
      shift 2
      ;;
    --broker-sock)
      BROKER_SOCK="${2:?missing --broker-sock value}"
      shift 2
      ;;
    --ready-file)
      READY_FILE="${2:?missing --ready-file value}"
      shift 2
      ;;
    --pools-file)
      POOLS_FILE="${2:?missing --pools-file value}"
      shift 2
      ;;
    --code-dir)
      CODE_DIR="${2:?missing --code-dir value}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:?missing --python value}"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if [[ -z "$ZONE" || -z "$WORKER_ID" || -z "$STATE_DIR" || -z "$BROKER_SOCK" || -z "$CODE_DIR" ]]; then
  echo "Usage: $0 --zone <blue|yellow> --worker-id <id> --state-dir <dir> --broker-sock <sock> --code-dir <dir> [--ready-file <file>] [--pools-file <file>] [--python <bin>]" >&2
  exit 2
fi

mkdir -p "$STATE_DIR"
export LITE_ACTIONS_STATE_DIR="$STATE_DIR"
export LITE_ACTIONS_BROKER_SOCK="$BROKER_SOCK"
export LITE_ACTIONS_DISABLE_IDLE_PROBE="1"
if [[ -n "$READY_FILE" ]]; then
  export LITE_ACTIONS_READY_FILE="$READY_FILE"
fi
if [[ -n "$POOLS_FILE" ]]; then
  export LITE_ACTIONS_ACTIVE_POOLS_PATH="$POOLS_FILE"
fi

cd "$CODE_DIR"
exec "$PYTHON_BIN" -u "$CODE_DIR/unified_router.py" --worker-id "$WORKER_ID" --broker-sock "$BROKER_SOCK" "$@"
