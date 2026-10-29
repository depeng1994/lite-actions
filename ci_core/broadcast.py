"""Best-effort terminal broadcast to CI *execution* machines only.

Prefer execution hosts for human sessions; also cover their CI containers.
No dispatcher-local terminal notification.
"""
from __future__ import annotations

import datetime as dt
import logging
import subprocess
import time
import unicodedata


BEIJING = dt.timezone(dt.timedelta(hours=8))
BORDER_WIDTH = 88


def display_width(value: str) -> int:
    return sum(0 if unicodedata.combining(char)
               else 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
               for char in value)


def star_box(message: str, url: str, *, width: int = BORDER_WIDTH) -> str:
    """Fixed terminal column width, including full-width Chinese characters."""
    if min(width - display_width(message), width - display_width(url)) < 4:
        raise ValueError("notification does not fit the chosen width")
    def framed(text: str) -> str:
        return "* " + text + " " * (width - display_width(text) - 4) + " *"
    return "\n".join([
        "*" * width, framed(""), framed(message), framed(url),
        framed(""), "*" * width,
    ]) + "\n"


def action_url(run: dict) -> str:
    ident = int(run["id"])
    if ident <= 0:
        raise ValueError("invalid GitHub run id")
    # Derived from trusted numeric run_id, never accepts arbitrary remote URL.
    return f"https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/{ident}"


def notification(run: dict, phase: str, *, now: dt.datetime | None = None) -> str:
    at = (now or dt.datetime.now(BEIJING)).astimezone(BEIJING)
    timestamp = at.strftime("%Y-%m-%d %H:%M:%S")
    messages = {
        "begin": "本环境正在用于torchtitan CI验证，请勿使用！",
        "end": "本次torchtitan CI验证已结束，本任务所占NPU资源已释放！",
        "uncertain": "本次torchtitan CI验证已退出，但NPU仍可能被占用，请勿使用！",
    }
    if phase not in messages:
        raise ValueError(f"invalid broadcast phase {phase}")
    return star_box(f"[{timestamp}] {messages[phase]}", action_url(run))


def broadcast_job(case: dict, pools: dict, run: dict, phase: str,
                  *, executor=subprocess.run) -> list[str]:
    """Broadcast once per assigned host, to host user sessions and CI container.

    The execution host endpoint is mandatory. Container broadcast is best-effort
    because container /dev/pts typically has no interactive login sessions.
    """
    requests = case.get("allocation", [])
    if not requests:
        return []
    nodes = {item["host_id"]: item for item in pools[case["pool"]]["nodes"]}
    msg = notification(run, phase)
    failures = []
    for allocated in requests:
        node = nodes[allocated["host_id"]]
        destinations = node.get("broadcast_ssh", [])
        if not destinations:
            raise ValueError(f"no host broadcast SSH configured for {allocated['host_id']}")
        for n, target in enumerate(dict.fromkeys(destinations)):
            # Host wall on A3 may intermittently reject SSH at key exchange.
            # Retry ONLY pre-start host notice; exit notice is advisory and
            # should not change a verified training/test result.
            retries = 3 if n == 0 and phase == "begin" else 1
            delivered = False
            for attempt in range(retries):
                try:
                    finished = executor(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                         "-o", "ServerAliveInterval=10", target, "wall -n"],
                        input=msg, text=True, capture_output=True, timeout=22,
                        check=False,
                    )
                    if finished.returncode:
                        raise RuntimeError(f"wall exited {finished.returncode}")
                    delivered = True
                    logging.info("CI %s wall delivered run=%s target=%s",
                                 phase, run["id"], target)
                    break
                except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                    logging.warning("CI %s wall attempt=%s/%s failed run=%s target=%s: %s",
                                    phase, attempt + 1, retries, run["id"],
                                    target, type(exc).__name__)
                    if attempt + 1 < retries:
                        time.sleep(2)
            if not delivered and n == 0:
                failures.append(target)
    return failures
