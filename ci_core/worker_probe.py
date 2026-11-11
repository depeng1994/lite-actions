"""Fixed, zero-device Linux Worker probe; never run on the Actions waiter."""

import os
import platform
import subprocess
import sys
import time

# Also used by the self-smoke waiter to display only approved receipt fields.
FIELDS = frozenset(
    {
        "WORKER_HOSTNAME",
        "WORKER_ARCH",
        "WORKER_KERNEL",
        "PRETTY_NAME",
        "WORKER_PYTHON",
        "WORKER_CPU_ONLINE",
        "WORKER_CPU_BUSY_PCT_1S",
        "WORKER_LOADAVG_1M_5M_15M",
    }
)


def safe_probe_line(line):
    if not isinstance(line, str) or len(line) > 180 or not line.isprintable():
        return False
    key, _, value = line.partition("=")
    return key in FIELDS and bool(value) and "::" not in line and "##[" not in line


def cpu_ticks():
    with open("/proc/stat", encoding="ascii") as f:
        label, *fields = f.readline().split()
    if label != "cpu" or len(fields) < 8:
        raise ValueError("missing aggregate Linux CPU counters")
    user, nice, system, idle, iowait, irq, softirq, steal = map(int, fields[:8])
    # guest/guest_nice are already accounted for in user/nice.
    busy = user + nice + system + irq + softirq
    return busy, busy + idle + iowait + steal


def snapshot():
    b0, t0 = cpu_ticks()
    time.sleep(1)
    b1, t1 = cpu_ticks()
    if t1 <= t0 or not 0 <= b1 - b0 <= t1 - t0:
        raise ValueError("invalid CPU counter sample")
    return {
        "WORKER_HOSTNAME": subprocess.check_output(
            ["hostname", "-f"], text=True
        ).strip(),
        "WORKER_ARCH": platform.machine(),
        "WORKER_KERNEL": f"{platform.system()} {platform.release()}",
        "PRETTY_NAME": platform.freedesktop_os_release()["PRETTY_NAME"],
        "WORKER_PYTHON": f"Python {platform.python_version()}",
        "WORKER_CPU_ONLINE": str(os.sysconf("SC_NPROCESSORS_ONLN")),
        "WORKER_CPU_BUSY_PCT_1S": f"{100 * (b1 - b0) / (t1 - t0):.1f}",
        "WORKER_LOADAVG_1M_5M_15M": " ".join(f"{n:.2f}" for n in os.getloadavg()),
    }


def probe():
    return subprocess.run(
        [sys.executable, "-I", __file__],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


if __name__ == "__main__":
    try:
        for key, value in snapshot().items():
            line = f"{key}={value}"
            if not safe_probe_line(line):
                raise ValueError("invalid probe field")
            print(line)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        sys.exit(f"Worker OS/CPU snapshot failed: {type(exc).__name__}")
