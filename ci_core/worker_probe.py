"""Local operating-system probe, independent of NPU capacity."""

import subprocess

PROBE = """set -eu
printf 'WORKER_HOSTNAME='; hostname -f
printf 'WORKER_ARCH='; uname -m
printf 'WORKER_KERNEL='; uname -sr
printf 'OS_RELEASE_BEGIN\\n'
cat /etc/os-release
printf 'OS_RELEASE_END\\n'
printf 'WORKER_PYTHON='; python3 --version
"""


def probe():
    return subprocess.run(
        ["bash", "-lc", PROBE], capture_output=True, text=True, timeout=90, check=False
    )
