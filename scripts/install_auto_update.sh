#!/usr/bin/env bash
# Install the updater around one already-managed Worker. No zone-specific paths.
set -euo pipefail
umask 077

usage() {
  echo "Usage: sudo bash scripts/install_auto_update.sh --worker-unit NAME.service [--git-url URL] [--git-ref refs/heads/BRANCH] [--install-root ABS_PATH] [--dry-run] [--enable-timer]"
}

worker_unit=""
git_url="https://github.com/depeng1994/lite-actions.git"
git_ref="refs/heads/main"
install_root="/opt/lite-actions"
dry_run=0
enable_timer=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker-unit) worker_unit="$2"; shift 2 ;;
    --git-url) git_url="$2"; shift 2 ;;
    --git-ref) git_ref="$2"; shift 2 ;;
    --install-root) install_root="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --enable-timer) enable_timer=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ ! "$worker_unit" =~ ^[A-Za-z0-9_.@:-]+\.service$ || "$worker_unit" == "lite-actions-auto-update.service" ]]; then
  echo "Specify an existing, valid Worker systemd unit" >&2
  exit 2
fi
if [[ ! "$install_root" =~ ^/[A-Za-z0-9_./-]+$ || "$install_root" == *..* ]]; then
  echo "Invalid absolute install root" >&2
  exit 2
fi
if [[ "$dry_run" != 1 && "$EUID" != 0 ]]; then
  echo "Installation requires root; use --dry-run for inspection" >&2
  exit 2
fi

source_root="$(cd "$(dirname "$0")/.." && pwd -P)"
if [[ ! -f "$source_root/auto_update/update.py" ]]; then
  echo "Missing reviewed updater source" >&2
  exit 2
fi

git_in_source() {
  git -c safe.directory="$source_root" -C "$source_root" "$@"
}
approved_sha="$(git_in_source rev-parse HEAD)"
if [[ ! "$approved_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Source commit SHA invalid" >&2
  exit 2
fi
if [[ -n "$(git_in_source status --porcelain --untracked-files=all)" ]]; then
  echo "Source Git checkout must be clean" >&2
  exit 2
fi

# Validate input using the actual package before any privileged changes.
(
  cd "$source_root"
  python3 - "$git_url" "$git_ref" <<'PY'
import sys
from auto_update.check_source import validate_source
validate_source(sys.argv[1], sys.argv[2])
PY
)

if [[ "$(systemctl show --property=LoadState --value "$worker_unit")" != "loaded" ]]; then
  echo "Existing Worker service is not loaded" >&2
  exit 2
fi
if [[ ! -L "$install_root/current" ]]; then
  echo "Existing deployment must use a current symlink" >&2
  exit 2
fi
old_path="$(readlink -e "$install_root/current")"
old_sha="$(git -C "$old_path" rev-parse HEAD)"
if [[ "$old_path" != "$install_root/releases/$old_sha" ]]; then
  echo "Current Worker is not an immutable SHA release; refusing changes" >&2
  exit 2
fi
workdir="$(systemctl show --property=WorkingDirectory --value "$worker_unit")"
if [[ -n "$workdir" && "$(readlink -f "$workdir")" != "$old_path" ]]; then
  echo "Existing Worker service does not run from current release" >&2
  exit 2
fi

# In normal operation the approved source branch should name this exact commit.
remote_sha="$(git ls-remote --exit-code "$git_url" "$git_ref" | awk '{print $1}')"
if [[ "$remote_sha" != "$approved_sha" ]]; then
  echo "Reviewed local commit is not the configured remote ref tip" >&2
  exit 2
fi

printf 'Worker unit: %s\nOld version: %s\nApproved version: %s\n' \
  "$worker_unit" "$old_sha" "$approved_sha"
echo "WARNING: initial installation can stop a Worker and cancel active Actions."

if [[ "$dry_run" == 1 ]]; then
  echo "DRY_RUN_OK: no files, services or tasks changed."
  exit 0
fi

install -d -m 0700 "$install_root/bootstrap"
install -d -m 0700 /etc/lite-actions /var/lib/lite-actions/auto-update
install -d -m 0700 /var/log/lite-actions/management
install -d -m 0700 "$install_root/staging"

seed="$install_root/bootstrap/seed"
if [[ ! -e "$seed" ]]; then
  # A local Git clone of an already-reviewed and clean commit. No remote code
  # gets executed in this step; the seed is immutable for updater recovery.
  git -c safe.directory="$source_root" clone --quiet --no-hardlinks \
    "$source_root" "$seed"
  git -C "$seed" checkout --quiet --detach "$approved_sha"
else
  if [[ "$(git -C "$seed" rev-parse HEAD)" != "$approved_sha" ]]; then
    # Existing seed is a recovery anchor, not something to overwrite.
    echo "Existing trusted seed differs; preserving it." >&2
  fi
fi

install_unchanged_or_new() {
  local src="$1" dst="$2" mode="$3"
  if [[ -e "$dst" ]]; then
    if ! cmp -s "$src" "$dst"; then
      echo "Existing managed file differs: $dst" >&2
      return 1
    fi
  else
    install -m "$mode" "$src" "$dst"
  fi
}

install_unchanged_or_new \
  "$source_root/auto_update/bootstrap.sh" \
  "$install_root/bootstrap/update.sh" 0755

service_template="$(mktemp)"
trap 'rm -f "$service_template"' EXIT
# install_root was restricted to safe path characters above.
sed "s|/opt/lite-actions|$install_root|g" \
  "$source_root/deploy/lite-actions-auto-update.service" > "$service_template"
install_unchanged_or_new "$service_template" \
  /etc/systemd/system/lite-actions-auto-update.service 0644
install_unchanged_or_new \
  "$source_root/deploy/lite-actions-auto-update.timer" \
  /etc/systemd/system/lite-actions-auto-update.timer 0644

ready_file="/var/lib/lite-actions/worker-ready.json"
env_file="/etc/lite-actions/auto-update.env"
expected_env="$(mktemp)"
trap 'rm -f "$service_template" "$expected_env"' EXIT
cat > "$expected_env" <<EOF
LITE_ACTIONS_UPDATE_GIT_URL=$git_url
LITE_ACTIONS_UPDATE_GIT_REF=$git_ref
LITE_ACTIONS_UPDATE_WORKER_UNIT=$worker_unit
LITE_ACTIONS_UPDATE_ROOT=$install_root
LITE_ACTIONS_UPDATE_STATE_DIR=/var/lib/lite-actions/auto-update
LITE_ACTIONS_UPDATE_LOG_FILE=/var/log/lite-actions/management/updates.jsonl
LITE_ACTIONS_UPDATE_READY_FILE=$ready_file
LITE_ACTIONS_UPDATE_READY_TIMEOUT=120
EOF
install_unchanged_or_new "$expected_env" "$env_file" 0600

drop_dir="/etc/systemd/system/$worker_unit.d"
install -d -m 0755 "$drop_dir"
drop_file="$drop_dir/50-lite-actions-ready.conf"
expected_drop="$(mktemp)"
trap 'rm -f "$service_template" "$expected_env" "$expected_drop"' EXIT
printf '[Service]\nEnvironment="LITE_ACTIONS_READY_FILE=%s"\n' \
  "$ready_file" > "$expected_drop"
install_unchanged_or_new "$expected_drop" "$drop_file" 0644

systemd-analyze verify \
  /etc/systemd/system/lite-actions-auto-update.service \
  /etc/systemd/system/lite-actions-auto-update.timer
systemctl daemon-reload

# The standalone invocation inherits only the variables we generated.
set -a
source "$env_file"
set +a
"$install_root/bootstrap/update.sh" --check
systemctl start lite-actions-auto-update.service

# For a NO_CHANGE initial install, the old process has not consumed the new
# optional Ready drop-in. Restart through its existing supervised lifecycle.
if [[ ! -e "$ready_file" ]]; then
  if [[ -f "$install_root/current/ci_core/worker_ready.py" ]]; then
    systemctl restart "$worker_unit"
  else
    echo "Existing release cannot publish Ready; installation needs upgraded code" >&2
    exit 1
  fi
fi

if [[ "$(systemctl is-active "$worker_unit")" != "active" ]]; then
  echo "Worker not active after installation" >&2
  exit 1
fi

# Verify a current, identity-bound Ready rather than relying on is-active.
# No changes to business state, allocation or secrets.
if [[ -f "$install_root/current/ci_core/worker_ready.py" ]]; then
  if ! (
    cd "$install_root/current"
    python3 - "$ready_file" "$(git -C "$install_root/current" rev-parse HEAD)" "$worker_unit" <<'PY'
import subprocess
import sys
import time
from pathlib import Path
from ci_core.worker_ready import verify_ready

ready = Path(sys.argv[1])
sha, unit = sys.argv[2], sys.argv[3]
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    result = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", unit],
        capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode == 0 and result.stdout.strip().isdigit():
        pid = int(result.stdout.strip())
        if pid > 0 and verify_ready(
            ready, expected_sha=sha, expected_pid=pid, not_before=0
        ):
            sys.exit(0)
    time.sleep(0.5)
sys.exit(1)
PY
  ); then
    echo "Worker Ready identity did not verify" >&2
    exit 1
  fi
fi

printf 'INSTALL_OK: unit=%s sha=%s\n' "$worker_unit" \
  "$(git -C "$install_root/current" rev-parse HEAD)"
echo "Management log: /var/log/lite-actions/management/updates.jsonl"
echo "Timer remains disabled by default pending business E2E."

if [[ "$enable_timer" == 1 ]]; then
  systemctl enable --now lite-actions-auto-update.timer
  echo "Timer enabled by explicit request."
fi
