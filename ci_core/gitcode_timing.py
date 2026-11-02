"""Sanitized monotonic stage timing for the trusted Blue execution path."""

from __future__ import annotations

import logging
import re
import sys
import time
from contextlib import contextmanager

_STAGES = frozenset(
    {
        "admit",
        "guard",
        "before_launch",
        "stage_nodes",
        "os_launch_and_wait",
        "os_probe",
        "os_execution",
        "cleanup",
        "finish_guard",
        "cleanup_ack",
        "aggregate_guard",
        "aggregate_publish",
        "gitcode_publish",
    }
)
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def _logger():
    logger = logging.getLogger("lite_actions.blue_stage_timing")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(
        getattr(handler, "_lite_actions_timing", False) for handler in logger.handlers
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._lite_actions_timing = True
        logger.addHandler(handler)
    return logger


def emit_stage(stage: str, elapsed_ms: int, *, run_id, case_id=None, outcome="ok"):
    if stage not in _STAGES or outcome not in {"ok", "error"}:
        return
    safe_run = run_id if type(run_id) is int and run_id > 0 else 0
    safe_case = (
        case_id if isinstance(case_id, str) and _SAFE_ID.fullmatch(case_id) else "none"
    )
    _logger().info(
        "blue_stage_timing stage=%s elapsed_ms=%d run_id=%d case_id=%s outcome=%s",
        stage,
        max(0, int(elapsed_ms)),
        safe_run,
        safe_case,
        outcome,
    )


@contextmanager
def timed_stage(stage: str, *, run_id, case_id=None):
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        emit_stage(
            stage,
            round((time.monotonic() - started) * 1000),
            run_id=run_id,
            case_id=case_id,
            outcome=outcome,
        )
