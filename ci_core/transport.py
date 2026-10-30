"""SSH transport: canonical aliases, optional trusted existing-container binding."""

import re
import shlex
import subprocess


def command(host, script):
    alias, sep, container = host.partition("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}", alias):
        raise ValueError("invalid SSH alias")
    if sep and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("invalid container identity")
    remote = (
        ("docker exec -i " + shlex.quote(container) + " " if sep else "")
        + "bash -lc "
        + shlex.quote(script)
    )
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=25",
        alias,
        remote,
    ]


def run(host, script, *, timeout=90, check=True, stdin=None):
    result = subprocess.run(
        command(host, script),
        stdin=stdin,
        capture_output=True,
        text=stdin is None,
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(f"SSH operation failed on {host}: exit={result.returncode}")
    return result
