"""Read-only installation doctor for the automatic Worker updater."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from auto_update.check_source import SourceError, check_source
from auto_update.deploy_support import (
    _DROPIN_CONTENT,
    DeploySupportError,
    _check_existing_file,
    read_proxy_env_file,
    render_systemd_environment,
    validate_python_interpreter,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    next_step: str


def _run_git(root: Path, args: list[str], extra_safe: tuple[Path, ...] = ()) -> str:
    safe = (root, root / ".git") + extra_safe
    fd, name = tempfile.mkstemp(prefix="lite-actions-preflight-git-")
    config = Path(name)
    try:
        os.fchmod(fd, 0o600)
        rows = []
        for path in safe:
            escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
            rows.append('    directory = "' + escaped + '"')
        os.write(fd, ("[safe]\n" + "\n".join(rows) + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(config),
        }
        for key in (
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "no_proxy",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
        ):
            if key in os.environ:
                env[key] = os.environ[key]
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode:
            raise RuntimeError("git_check_failed")
        return result.stdout.strip()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        config.unlink(missing_ok=True)


def systemd_available() -> bool:
    return bool(
        shutil.which("systemctl")
        and shutil.which("systemd-analyze")
        and Path("/run/systemd/system").exists()
    )


def _systemctl_show(unit: str, prop: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", "--property=" + prop, "--value", unit],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("systemctl_query_failed")
    return result.stdout.strip()


def run_preflight(
    *,
    python_path: str,
    worker_unit: str,
    install_root: str | Path,
    source_root: str | Path,
    git_url: str,
    git_ref: str,
    proxy_env_file: str | Path | None = None,
) -> list[Check]:
    checks: list[Check] = []
    root = Path(install_root)
    source = Path(source_root).resolve()
    proxy_values = {}
    if proxy_env_file:
        try:
            proxy_values = read_proxy_env_file(proxy_env_file)
            os.environ.update(proxy_values)
            checks.append(
                Check("proxy file", True, "root-owned 0600 allowlist accepted", "")
            )
        except DeploySupportError as exc:
            checks.append(
                Check(
                    "proxy file",
                    False,
                    str(exc),
                    "Create a root-owned regular file with mode 0600 and only the nine supported KEY=VALUE names.",
                )
            )

    try:
        version = validate_python_interpreter(python_path)
        checks.append(Check("Python", True, version, ""))
    except DeploySupportError as exc:
        checks.append(
            Check(
                "Python",
                False,
                str(exc),
                "Install Python 3.11+ and pass --python /absolute/path/to/python3.12.",
            )
        )

    git_path = shutil.which("git")
    if git_path:
        try:
            version = subprocess.run(
                [git_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
            checks.append(Check("Git", True, version, ""))
        except (OSError, subprocess.SubprocessError):
            checks.append(
                Check(
                    "Git",
                    False,
                    "Git version query failed",
                    "Install Git and ensure it is in PATH.",
                )
            )
    else:
        checks.append(
            Check(
                "Git",
                False,
                "git executable not found",
                "Install Git and ensure it is in PATH.",
            )
        )

    if systemd_available():
        checks.append(
            Check("systemd", True, "systemd manager and systemd-analyze available", "")
        )
        try:
            load_state = _systemctl_show(worker_unit, "LoadState")
            if load_state == "loaded":
                checks.append(
                    Check("Worker unit", True, worker_unit + " is loaded", "")
                )
            else:
                checks.append(
                    Check(
                        "Worker unit",
                        False,
                        "configured Worker unit is not loaded",
                        "Run systemctl list-unit-files --type=service and pass the existing Worker .service name.",
                    )
                )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            checks.append(
                Check(
                    "Worker unit",
                    False,
                    "systemctl could not inspect the configured Worker unit",
                    "Run systemctl status "
                    + worker_unit
                    + " and verify the unit name.",
                )
            )
    else:
        checks.append(
            Check(
                "systemd",
                False,
                "systemd manager, systemctl or systemd-analyze is unavailable",
                "Run this preflight on the systemd host (not inside a container without the host systemd bus).",
            )
        )
        checks.append(
            Check(
                "Worker unit",
                False,
                "not checked because systemd is unavailable",
                "Run systemctl status " + worker_unit + " on the Worker host.",
            )
        )

    try:
        current = root / "current"
        if not current.is_symlink():
            raise RuntimeError("current is not a symlink")
        release = current.resolve(strict=True)
        sha = release.name
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RuntimeError("current target is not named by a full commit SHA")
        if release.parent != (root / "releases").resolve(strict=True):
            raise RuntimeError("current target is outside releases/<sha>")
        if not (release / ".git").exists():
            raise RuntimeError("current release has no .git metadata")
        actual = _run_git(release, ["rev-parse", "--verify", "HEAD^{commit}"])
        if actual != sha:
            raise RuntimeError("release HEAD does not match its directory SHA")
        checks.append(
            Check(
                "current release",
                True,
                "current -> releases/<sha> Git checkout " + sha[:12],
                "",
            )
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        checks.append(
            Check(
                "current release",
                False,
                "current must be a symlink to releases/<40-character-SHA> with valid .git metadata",
                "Migrate the release layout separately; do not install over an archive/rsync snapshot.",
            )
        )
        release = None

    if release is not None:
        try:
            working_directory = _systemctl_show(worker_unit, "WorkingDirectory")
            if (
                working_directory
                and Path(working_directory).resolve() != release.resolve()
            ):
                raise RuntimeError(
                    "Worker WorkingDirectory does not match current release"
                )
            checks.append(
                Check(
                    "Worker WorkingDirectory",
                    True,
                    "matches current release"
                    if working_directory
                    else "not explicitly set; ExecStart remains host-managed",
                    "",
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            checks.append(
                Check(
                    "Worker WorkingDirectory",
                    False,
                    "Worker unit does not point to the current Git release",
                    "Review systemctl cat "
                    + worker_unit
                    + " and set WorkingDirectory to "
                    + str(release)
                    + ".",
                )
            )
        for name in ("config", "logs", "cache"):
            binding = release / name
            if binding.is_dir():
                checks.append(
                    Check(
                        name + " binding",
                        True,
                        "directory or resolved directory exists",
                        "",
                    )
                )
            else:
                checks.append(
                    Check(
                        name + " binding",
                        False,
                        "required directory/binding is missing or broken",
                        "Restore the existing "
                        + name
                        + " directory/absolute symlink before installation; do not replace business state.",
                    )
                )
    else:
        for name in ("config", "logs", "cache"):
            checks.append(
                Check(
                    name + " binding",
                    False,
                    "cannot verify without a valid current release",
                    "Restore the current Git release layout first.",
                )
            )

    try:
        head = _run_git(source, ["rev-parse", "--verify", "HEAD^{commit}"])
        dirty = _run_git(source, ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"])
        if dirty:
            raise RuntimeError("source worktree is dirty")
        checks.append(
            Check("source worktree", True, "clean Git checkout " + head[:12], "")
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        head = ""
        checks.append(
            Check(
                "source worktree",
                False,
                "source worktree is missing Git metadata or has tracked/untracked changes",
                "Use a clean, reviewed checkout: git status --short; git reset/clean only after reviewing local files.",
            )
        )

    try:
        remote_sha = check_source(git_url, git_ref)
        if head and remote_sha != head:
            raise SourceError("source checkout does not match configured remote ref")
        checks.append(
            Check(
                "GitHub HTTPS source",
                True,
                "configured ref is reachable and matches local HEAD",
                "",
            )
        )
    except (SourceError, OSError, subprocess.SubprocessError):
        checks.append(
            Check(
                "GitHub HTTPS source",
                False,
                "configured HTTPS ref could not be verified or does not match local HEAD",
                "Verify outbound HTTPS/proxy/CA settings, then run git ls-remote "
                + git_url
                + " "
                + git_ref
                + ".",
            )
        )

    if proxy_env_file:
        try:
            managed = Path("/etc/lite-actions/auto-update-proxy.env")
            dropin = Path(
                "/etc/systemd/system/lite-actions-auto-update.service.d/20-proxy-env.conf"
            )
            _check_existing_file(
                managed,
                render_systemd_environment(proxy_values).encode(),
                0o600,
                0,
            )
            _check_existing_file(dropin, _DROPIN_CONTENT.encode(), 0o644, 0)
            checks.append(
                Check(
                    "proxy integration",
                    True,
                    "managed file/drop-in are absent or identical",
                    "",
                )
            )
        except DeploySupportError as exc:
            checks.append(
                Check(
                    "proxy integration",
                    False,
                    str(exc),
                    "Back up and review the existing managed proxy files; do not overwrite changed files automatically.",
                )
            )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", dest="python_path", required=True)
    parser.add_argument("--worker-unit", required=True)
    parser.add_argument("--install-root", default="/opt/lite-actions")
    parser.add_argument("--source-root", default=str(Path.cwd()))
    parser.add_argument(
        "--git-url", default="https://github.com/depeng1994/lite-actions.git"
    )
    parser.add_argument("--git-ref", default="refs/heads/main")
    parser.add_argument("--proxy-env-file")
    args = parser.parse_args()
    checks = run_preflight(
        python_path=args.python_path,
        worker_unit=args.worker_unit,
        install_root=args.install_root,
        source_root=args.source_root,
        git_url=args.git_url,
        git_ref=args.git_ref,
        proxy_env_file=args.proxy_env_file,
    )
    for check in checks:
        print("[{}] {}".format("OK" if check.ok else "FAIL", check.name))
        print("  " + check.detail)
        if not check.ok:
            print("  Next: " + check.next_step)
    failures = [check for check in checks if not check.ok]
    print(
        "Preflight: {} ({} checks, {} failed)".format(
            "FAIL" if failures else "PASS", len(checks), len(failures)
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
