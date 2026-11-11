#!/usr/bin/env bash
# Install the updater around one already-managed Worker. No zone-specific paths.
set -euo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/install_auto_update.sh --worker-unit NAME.service [options]
  --python PATH          Absolute Python >=3.11 (default: command -v python3)
  --proxy-env-file FILE  Root-owned 0600 file with only the nine proxy/CA keys
  --git-url URL          Trusted HTTPS Git source
  --git-ref REF          Trusted branch ref (default: refs/heads/main)
  --install-root PATH    Existing release root (default: /opt/lite-actions)
  --dry-run              Run complete read-only preflight; make no changes
  --enable-timer         Enable the timer after successful installation
EOF
}
fail() { echo "ERROR: $*" >&2; exit 2; }
need_value() { [[ $# -ge 2 && -n "$2" ]] || fail "Missing value for $1"; }

worker_unit=""
git_url="https://github.com/depeng1994/lite-actions.git"
git_ref="refs/heads/main"
install_root="/opt/lite-actions"
python_path=""
proxy_env_file=""
dry_run=0
enable_timer=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker-unit) need_value "$@"; worker_unit="$2"; shift 2 ;;
    --python) need_value "$@"; python_path="$2"; shift 2 ;;
    --proxy-env-file) need_value "$@"; proxy_env_file="$2"; shift 2 ;;
    --git-url) need_value "$@"; git_url="$2"; shift 2 ;;
    --git-ref) need_value "$@"; git_ref="$2"; shift 2 ;;
    --install-root) need_value "$@"; install_root="$2"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --enable-timer) enable_timer=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "Unknown argument: $1" ;;
  esac
done

if [[ ! "$worker_unit" =~ ^[A-Za-z0-9_.@:-]+\.service$ || "$worker_unit" == "lite-actions-auto-update.service" ]]; then
  fail "Specify the existing Worker unit, e.g. lite-actions-yellow.service"
fi
if [[ ! "$install_root" =~ ^/[A-Za-z0-9_./-]+$ || "$install_root" == *..* ]]; then
  fail "Install root must be a safe absolute path"
fi
if [[ "$dry_run" != 1 && "$EUID" != 0 ]]; then fail "Installation requires root; run with sudo"; fi
if [[ -n "$proxy_env_file" && "$EUID" != 0 ]]; then
  fail "--proxy-env-file requires root to verify root ownership and mode 0600; run with sudo"
fi

source_root="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -f "$source_root/auto_update/update.py" ]] || fail "Run from a complete reviewed Lite Actions Git checkout"

# sudo may reset PATH; persist and use an absolute interpreter path.
if [[ -z "$python_path" ]]; then python_path="$(command -v python3 || true)"; fi
[[ -n "$python_path" ]] || fail "python3 not found; install Python 3.11+ and pass --python /absolute/path/to/python3.12"
[[ "$python_path" == /* ]] || fail "--python must be absolute; e.g. --python /usr/local/bin/python3.12"
[[ -f "$python_path" && -x "$python_path" ]] || fail "Python executable missing/not executable: $python_path"
python_version="$("$python_path" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
[[ -n "$python_version" ]] || fail "Cannot execute $python_path; pass --python /absolute/path/to/python3.12"
if ! "$python_path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  fail "Python 3.11+ required; $python_path is $python_version. On Ubuntu 20.04 use /usr/local/bin/python3.12: add --python /usr/local/bin/python3.12. sudo may reset PATH, so use an absolute path."
fi
export LITE_ACTIONS_UPDATE_PYTHON="$python_path"
cd "$source_root"

echo "Running read-only Auto Update preflight..."
if [[ -n "$proxy_env_file" ]]; then
  "$python_path" -m auto_update.preflight \
    --python "$python_path" --worker-unit "$worker_unit" \
    --install-root "$install_root" --source-root "$source_root" \
    --git-url "$git_url" --git-ref "$git_ref" \
    --proxy-env-file "$proxy_env_file" \
    || fail "Preflight failed; no deployment files or services were changed"
else
  "$python_path" -m auto_update.preflight \
    --python "$python_path" --worker-unit "$worker_unit" \
    --install-root "$install_root" --source-root "$source_root" \
    --git-url "$git_url" --git-ref "$git_ref" \
    || fail "Preflight failed; no deployment files or services were changed"
fi
if [[ "$dry_run" == 1 ]]; then
  echo "DRY_RUN_OK: preflight passed; no files, services or tasks changed."
  exit 0
fi

# Git 2.25: use a private global safe.directory config inherited by clone's
# upload-pack child. Never edit /root/.gitconfig.
safe_git_config="$(mktemp)"
chmod 0600 "$safe_git_config"
git config --file "$safe_git_config" --add safe.directory "$source_root"
git config --file "$safe_git_config" --add safe.directory "$source_root/.git"
trap 'rm -f "$safe_git_config"' EXIT
git_in_source() {
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$safe_git_config" git -C "$source_root" "$@"
}
approved_sha="$(git_in_source rev-parse --verify 'HEAD^{commit}')"
[[ "$approved_sha" =~ ^[0-9a-f]{40}$ ]] || fail "Source commit SHA invalid"
[[ -z "$(git_in_source status --porcelain --untracked-files=all)" ]] || fail "Source worktree changed after preflight"

old_path="$(readlink -e "$install_root/current")"
git config --file "$safe_git_config" --add safe.directory "$old_path"
git config --file "$safe_git_config" --add safe.directory "$old_path/.git"
old_sha="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$safe_git_config" git -C "$old_path" rev-parse --verify 'HEAD^{commit}')"
[[ "$old_path" == "$install_root/releases/$old_sha" ]] || fail "Current release layout changed after preflight"

printf 'Worker unit: %s\nPython: %s (%s)\nOld version: %s\nApproved version: %s\n' \
  "$worker_unit" "$python_path" "$python_version" "$old_sha" "$approved_sha"
echo "WARNING: initial installation can stop a Worker and cancel active Actions."

install -d -m 0700 "$install_root/bootstrap"
install -d -m 0700 /etc/lite-actions /var/lib/lite-actions/auto-update
install -d -m 0700 /var/log/lite-actions/management
install -d -m 0700 "$install_root/staging"

seed="$install_root/bootstrap/seed"
if [[ ! -e "$seed" ]]; then
  "$python_path" -m auto_update.deploy_support clone-seed \
    --source-root "$source_root" --seed "$seed" --sha "$approved_sha"
else
  seed_sha="$(git -C "$seed" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)"
  if [[ "$seed_sha" != "$approved_sha" ]]; then
    echo "Existing trusted seed differs; preserving it as the recovery anchor."
  fi
fi

install_unchanged_or_new() {
  local src="$1" dst="$2" mode="$3"
  if [[ -e "$dst" || -L "$dst" ]]; then
    [[ -f "$dst" && ! -L "$dst" ]] || fail "Existing managed file is not regular: $dst"
    cmp -s "$src" "$dst" || fail "Existing managed file differs: $dst (back up and review before replacing)"
  else
    install -m "$mode" "$src" "$dst"
  fi
}
install_managed_bootstrap() {
  local src="$1" dst="$2"
  if [[ -e "$dst" || -L "$dst" ]]; then
    [[ -f "$dst" && ! -L "$dst" && "$(stat -c %u "$dst")" == "0" ]] \
      || fail "Existing bootstrap is not a root-owned regular file: $dst"
    if ! cmp -s "$src" "$dst"; then
      install -m 0755 "$src" "$dst.new"
      mv -f "$dst.new" "$dst"
    fi
  else
    install -m 0755 "$src" "$dst"
  fi
}
install_managed_bootstrap "$source_root/auto_update/bootstrap.sh" "$install_root/bootstrap/update.sh"

service_template="$(mktemp)"
expected_env="$(mktemp)"
legacy_env="$(mktemp)"
expected_drop="$(mktemp)"
trap 'rm -f "$safe_git_config" "$service_template" "$expected_env" "$legacy_env" "$expected_drop"' EXIT
sed "s|/opt/lite-actions|$install_root|g" \
  "$source_root/deploy/lite-actions-auto-update.service" > "$service_template"
install_unchanged_or_new "$service_template" /etc/systemd/system/lite-actions-auto-update.service 0644
install_unchanged_or_new "$source_root/deploy/lite-actions-auto-update.timer" \
  /etc/systemd/system/lite-actions-auto-update.timer 0644

ready_file="/var/lib/lite-actions/worker-ready.json"
env_file="/etc/lite-actions/auto-update.env"
cat > "$expected_env" <<EOF
LITE_ACTIONS_UPDATE_GIT_URL=$git_url
LITE_ACTIONS_UPDATE_GIT_REF=$git_ref
LITE_ACTIONS_UPDATE_WORKER_UNIT=$worker_unit
LITE_ACTIONS_UPDATE_ROOT=$install_root
LITE_ACTIONS_UPDATE_STATE_DIR=/var/lib/lite-actions/auto-update
LITE_ACTIONS_UPDATE_LOG_FILE=/var/log/lite-actions/management/updates.jsonl
LITE_ACTIONS_UPDATE_READY_FILE=$ready_file
LITE_ACTIONS_UPDATE_READY_TIMEOUT=120
LITE_ACTIONS_UPDATE_PYTHON="$python_path"
EOF
sed '/^LITE_ACTIONS_UPDATE_PYTHON=/d' "$expected_env" > "$legacy_env"
if [[ -e "$env_file" || -L "$env_file" ]]; then
  [[ -f "$env_file" && ! -L "$env_file" && "$(stat -c %u "$env_file")" == "0" && "$(stat -c %a "$env_file")" == "600" ]] \
    || fail "Existing auto-update.env must be root-owned regular mode 0600"
  if cmp -s "$env_file" "$expected_env"; then
    :
  elif cmp -s "$env_file" "$legacy_env"; then
    install -m 0600 "$expected_env" "$env_file.new"
    mv -f "$env_file.new" "$env_file"
  else
    fail "Existing auto-update.env differs; review settings and use the same --python path before retrying"
  fi
else
  install -m 0600 "$expected_env" "$env_file"
fi

drop_dir="/etc/systemd/system/$worker_unit.d"
install -d -m 0755 "$drop_dir"
drop_file="$drop_dir/50-lite-actions-ready.conf"
printf '[Service]\nEnvironment="LITE_ACTIONS_READY_FILE=%s"\n' "$ready_file" > "$expected_drop"
install_unchanged_or_new "$expected_drop" "$drop_file" 0644

# Proxy support is opt-in. Without --proxy-env-file no proxy files are created.
if [[ -n "$proxy_env_file" ]]; then
  "$python_path" -m auto_update.deploy_support install-proxy --source "$proxy_env_file"
fi

systemd-analyze verify \
  /etc/systemd/system/lite-actions-auto-update.service \
  /etc/systemd/system/lite-actions-auto-update.timer
systemctl daemon-reload

export LITE_ACTIONS_UPDATE_ROOT="$install_root"
export LITE_ACTIONS_UPDATE_GIT_URL="$git_url"
export LITE_ACTIONS_UPDATE_GIT_REF="$git_ref"
export LITE_ACTIONS_UPDATE_WORKER_UNIT="$worker_unit"
export LITE_ACTIONS_UPDATE_READY_FILE="$ready_file"
if [[ -n "$proxy_env_file" ]]; then
  "$python_path" -m auto_update.deploy_support run-with-proxy \
    --source "$proxy_env_file" -- "$install_root/bootstrap/update.sh" --check
else
  "$install_root/bootstrap/update.sh" --check
fi

systemctl start lite-actions-auto-update.service
if [[ ! -e "$ready_file" ]]; then
  if [[ -f "$install_root/current/ci_core/worker_ready.py" ]]; then
    systemctl restart "$worker_unit"
  else
    fail "Existing release cannot publish Ready; upgrade Worker code first"
  fi
fi
[[ "$(systemctl is-active "$worker_unit")" == "active" ]] || fail "Worker is not active after installation"

# Ready identity validation always uses the selected interpreter.
if [[ -f "$install_root/current/ci_core/worker_ready.py" ]]; then
  if ! (
    cd "$install_root/current"
    "$python_path" - "$ready_file" "$(git -C "$install_root/current" rev-parse HEAD)" "$worker_unit" <<'PY'
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
        if pid > 0 and verify_ready(ready, expected_sha=sha, expected_pid=pid, not_before=0):
            sys.exit(0)
    time.sleep(0.5)
sys.exit(1)
PY
  ); then
    fail "Worker Ready identity did not verify"
  fi
fi

printf 'INSTALL_OK: unit=%s python=%s sha=%s\n' \
  "$worker_unit" "$python_path" "$(git -C "$install_root/current" rev-parse HEAD)"
echo "Management log: /var/log/lite-actions/management/updates.jsonl"
echo "Timer remains disabled by default pending business E2E."
if [[ "$enable_timer" == 1 ]]; then
  systemctl enable --now lite-actions-auto-update.timer
  echo "Timer enabled by explicit request."
fi
