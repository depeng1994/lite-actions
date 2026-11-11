"""Safe installation helpers for the automatic updater.

This module deliberately keeps proxy values in memory and never prints them.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PROXY_ENV_ALLOWLIST = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
)
_DROPIN_CONTENT = (
    "[Service]\nEnvironmentFile=-/etc/lite-actions/auto-update-proxy.env\n"
)


class DeploySupportError(RuntimeError):
    """A safe, actionable deployment error without sensitive values."""


def validate_python_interpreter(path: str) -> str:
    """Require an absolute executable Python >=3.11 and return its version."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise DeploySupportError(
            "Python path must be absolute; pass --python /absolute/path"
        )
    if not candidate.is_file() or not os.access(str(candidate), os.X_OK):
        raise DeploySupportError(
            "Python executable not found or not executable: "
            + str(candidate)
            + "; install Python 3.11+ or pass --python /absolute/path/to/python3.12"
        )
    try:
        result = subprocess.run(
            [
                str(candidate),
                "-c",
                "import sys; print('%d.%d.%d' % sys.version_info[:3])",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploySupportError(
            "Cannot execute selected Python; pass --python /absolute/path/to/python3.12"
        ) from exc
    version = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DeploySupportError(
            "Selected Python did not return a valid version; pass --python /absolute/path/to/python3.12"
        )
    parts = tuple(int(part) for part in version.split("."))
    if parts < (3, 11, 0):
        raise DeploySupportError(
            "Python 3.11+ is required; selected interpreter is "
            + version
            + ". Ubuntu 20.04 may default to Python 3.8.10. Install/use the existing "
            "/usr/local/bin/python3.12 and run: sudo bash scripts/install_auto_update.sh "
            "--worker-unit NAME.service --python /usr/local/bin/python3.12 "
            "(sudo may reset PATH; use the absolute path)."
        )
    return version


def read_proxy_env_file(path: str | Path, *, expected_uid: int = 0) -> dict[str, str]:
    """Read a root-owned 0600 regular file without following symlinks."""
    source = Path(path)
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise DeploySupportError(
            "Proxy environment file cannot be inspected; check its path and ownership"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DeploySupportError(
            "Proxy environment file must be a regular non-symlink file"
        )
    if info.st_uid != expected_uid:
        raise DeploySupportError("Proxy environment file must be owned by root")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise DeploySupportError(
            "Proxy environment file must have mode 0600 (chmod 0600 FILE)"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(source), flags)
        with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise DeploySupportError(
                    "Proxy environment file changed while being opened"
                )
            values: dict[str, str] = {}
            for line_number, raw in enumerate(stream.read().split("\n"), 1):
                if not raw:
                    continue
                if "\r" in raw or "\x00" in raw:
                    raise DeploySupportError(
                        "Proxy environment file contains an invalid line; use KEY=VALUE only"
                    )
                key, separator, value = raw.partition("=")
                if not separator or key not in PROXY_ENV_ALLOWLIST:
                    raise DeploySupportError(
                        "Proxy environment file accepts only the nine documented proxy/CA KEY=VALUE names"
                    )
                if key in values:
                    raise DeploySupportError(
                        "Proxy environment file contains a duplicate key on line "
                        + str(line_number)
                    )
                if any(ord(char) < 32 or ord(char) == 127 for char in value):
                    raise DeploySupportError(
                        "Proxy environment values must be single-line printable values"
                    )
                values[key] = value
            if not values:
                raise DeploySupportError(
                    "Proxy environment file contains no allowed KEY=VALUE entries"
                )
            return values
    except DeploySupportError:
        raise
    except (OSError, UnicodeError) as exc:
        raise DeploySupportError(
            "Proxy environment file cannot be read safely"
        ) from exc


def render_systemd_environment(values: dict[str, str]) -> str:
    """Serialize validated values for systemd EnvironmentFile syntax."""
    rows = []
    for key in PROXY_ENV_ALLOWLIST:
        if key not in values:
            continue
        value = values[key]
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise DeploySupportError(
                "Proxy environment values must be single-line printable values"
            )
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        rows.append(key + '="' + escaped + '"')
    return "\n".join(rows) + "\n"


def _check_existing_file(
    path: Path, expected: bytes, mode: int, owner_uid: int
) -> bool:
    """Return True if exact content already exists; reject unsafe or changed files."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeploySupportError(
            "Cannot inspect existing managed proxy configuration"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DeploySupportError(
            "Existing managed proxy configuration must be a regular non-symlink file"
        )
    if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != mode:
        raise DeploySupportError(
            "Existing managed proxy configuration has unexpected owner or mode"
        )
    try:
        current = path.read_bytes()
    except OSError as exc:
        raise DeploySupportError(
            "Cannot read existing managed proxy configuration"
        ) from exc
    if current != expected:
        raise DeploySupportError(
            "Existing managed proxy configuration differs; review it, back it up, "
            "then remove the managed file(s) explicitly before reinstalling"
        )
    return True


def _create_exclusive(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def install_managed_proxy_files(
    source: str | Path,
    managed_file: str | Path = "/etc/lite-actions/auto-update-proxy.env",
    dropin_file: str | Path = (
        "/etc/systemd/system/lite-actions-auto-update.service.d/20-proxy-env.conf"
    ),
    *,
    expected_uid: int = 0,
) -> tuple[bool, bool]:
    """Install an immutable managed copy and systemd drop-in, idempotently."""
    values = read_proxy_env_file(source, expected_uid=expected_uid)
    managed = Path(managed_file)
    dropin = Path(dropin_file)
    managed_bytes = render_systemd_environment(values).encode("utf-8")
    dropin_bytes = _DROPIN_CONTENT.encode("utf-8")
    managed_exists = _check_existing_file(managed, managed_bytes, 0o600, expected_uid)
    dropin_exists = _check_existing_file(dropin, dropin_bytes, 0o644, expected_uid)
    if not managed_exists:
        _create_exclusive(managed, managed_bytes, 0o600)
    if not dropin_exists:
        _create_exclusive(dropin, dropin_bytes, 0o644)
    return (not managed_exists, not dropin_exists)


def safe_git_environment(global_config: str | Path) -> dict[str, str]:
    """Build a minimal Git environment with only a temporary trusted config."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(global_config),
    }
    for name in PROXY_ENV_ALLOWLIST:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def clone_seed(source_root: str | Path, seed: str | Path, sha: str) -> None:
    """Clone a clean user-owned worktree as root without relying on -c propagation.

    A private GIT_CONFIG_GLOBAL is inherited by clone's upload-pack subprocess,
    including Git 2.25 installations. It is removed after checkout.
    """
    source = Path(source_root).resolve(strict=True)
    target = Path(seed)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploySupportError("Invalid approved source SHA")
    if target.exists() or target.is_symlink():
        raise DeploySupportError("Seed already exists; refusing to overwrite it")
    if not source.is_dir() or not (source / ".git").exists():
        raise DeploySupportError("Source must be a Git working tree with .git metadata")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, config_name = tempfile.mkstemp(
        prefix="lite-actions-safe-git-", suffix=".config"
    )
    config = Path(config_name)
    try:
        os.fchmod(fd, 0o600)
        safe_entries = []
        for directory in (source, source / ".git", target, target / ".git"):
            escaped = str(directory).replace("\\", "\\\\").replace('"', '\\"')
            safe_entries.append('    directory = "' + escaped + '"')
        os.write(fd, ("[safe]\n" + "\n".join(safe_entries) + "\n").encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        env = safe_git_environment(config)
        result = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise DeploySupportError(
                "Seed clone failed; check source worktree ownership and Git"
            )
        result = subprocess.run(
            ["git", "-C", str(target), "checkout", "--quiet", "--detach", sha],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise DeploySupportError(
                "Seed checkout failed; verify the approved source SHA"
            )
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != sha:
            raise DeploySupportError("Seed SHA verification failed")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        config.unlink(missing_ok=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install-proxy")
    install.add_argument("--source", required=True)
    install.add_argument("--managed")
    install.add_argument("--dropin")
    run = sub.add_parser("run-with-proxy")
    run.add_argument("--source", required=True)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    seed = sub.add_parser("clone-seed")
    seed.add_argument("--source-root", required=True)
    seed.add_argument("--seed", required=True)
    seed.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        if args.command == "install-proxy":
            created = install_managed_proxy_files(
                args.source,
                args.managed or "/etc/lite-actions/auto-update-proxy.env",
                args.dropin
                or "/etc/systemd/system/lite-actions-auto-update.service.d/20-proxy-env.conf",
            )
            print(
                "Proxy integration "
                + ("installed" if any(created) else "already matches")
                + "; values were not displayed."
            )
            return 0
        if args.command == "run-with-proxy":
            argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            if not argv:
                raise DeploySupportError("run-with-proxy requires a command after --")
            values = read_proxy_env_file(args.source)
            env = os.environ.copy()
            env.update(values)
            os.execvpe(argv[0], argv, env)
        if args.command == "clone-seed":
            clone_seed(args.source_root, args.seed, args.sha)
            print("Seed clone and SHA verification succeeded.")
            return 0
    except DeploySupportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())