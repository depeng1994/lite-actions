"""Trusted remote launch/cancel interlock; standard library only.

Copied from the reviewed Worker, not from a proposed source bundle. A cancelled
marker permanently denies a delayed launch. The lock spans intent persistence,
process creation and PID recording so cleanup cannot race a late Popen.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

MARKER = re.compile(r"g4_[0-9a-f]{64}\Z")


def program():
    """Return this reviewed script for a credential-free remote Python -I call."""
    return Path(__file__).read_text()


def read_optional(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r") as stream:
        value = stream.read(1025)
    if len(value) > 1024:
        raise ValueError("oversized execution marker")
    return value


def durable_write(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(folder, marker):
    folder = Path(folder)
    if (
        not MARKER.fullmatch(marker)
        or not folder.is_absolute()
        or folder.is_symlink()
        or not folder.is_dir()
    ):
        raise ValueError("owned staged directory and marker required")
    fd = os.open(
        folder / ".launch.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = read_optional(folder / "job.intent")
        cancelled = read_optional(folder / "cancel.requested")
        if existing not in (None, marker) or cancelled not in (None, marker):
            raise ValueError("foreign execution marker")
        yield folder, existing, cancelled
    finally:
        os.close(fd)


def cancel_intent(folder, marker):
    with locked(folder, marker) as (folder, existing, cancelled):
        if cancelled is None:
            durable_write(folder / "cancel.requested", marker)
        if existing is None:
            # Mutex proves Popen has never occurred for this directory. A late
            # launcher will observe this cancellation rather than starting work.
            durable_write(folder / "job.intent", marker)
            durable_write(folder / "exit_code.txt", "130")
    return 0


def launch(folder, marker, command):
    if (
        not isinstance(command, str)
        or not command
        or len(command) > 65536
        or "\0" in command
    ):
        raise ValueError("invalid trusted launch command")
    with locked(folder, marker) as (folder, existing, cancelled):
        if cancelled is not None:
            return 130
        if existing is not None or (folder / "exit_code.txt").exists():
            raise ValueError("execution directory is not reusable")
        durable_write(folder / "job.intent", marker)
        log_fd = os.open(
            folder / "run.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(log_fd, "wb") as stream:
            child = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c", command],
                stdout=stream,
                stderr=subprocess.STDOUT,
                env={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "HOME": "/root",
                    "LITE_CI_JOB_ID": marker,
                },
                start_new_session=True,
            )
            durable_write(folder / "job.pgid", str(child.pid))
    code = child.wait()
    durable_write(folder / "exit_code.txt", str(code))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("launch", "cancel-intent"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("marker")
    parser.add_argument("command", nargs="?")
    args = parser.parse_args(argv)
    if args.mode == "cancel-intent":
        if args.command is not None:
            raise ValueError("cancellation accepts no command")
        return cancel_intent(args.directory, args.marker)
    return launch(args.directory, args.marker, args.command)


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        # Do not echo the command or potentially sensitive environment values.
        print("trusted supervisor failure: " + type(exc).__name__, file=sys.stderr)
        result = 125
    raise SystemExit(result)
