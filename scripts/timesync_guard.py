#!/usr/bin/env python3
"""Guard worker wall-clock skew against the Control Center HTTPS Date header."""

from __future__ import annotations

import argparse
import email.utils
import logging
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Callable, Optional, Sequence

DEFAULT_URL = "https://lite-actions.ooguy.com/api/v1/public/overview"
DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_THRESHOLD_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 10.0

LOG = logging.getLogger("lite-actions-timesync")
_SYNC_LINE_RE = re.compile(
    r"^\s*System clock synchronized:\s*(yes|no)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single clock synchronization check."""

    url: str
    remote_epoch: float
    local_epoch: float
    offset_seconds: float
    rtt_seconds: float
    ntp_synchronized: Optional[bool]
    action: str
    applied_epoch: Optional[int] = None


def validate_url(url: str) -> str:
    """Validate that the target URL uses HTTP(S) and embeds no credentials."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Unsupported or invalid URL: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain embedded credentials")
    return url


def sanitize_url_for_log(url: str) -> str:
    """Return a log-safe URL with any userinfo stripped."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, "")
    )


def parse_http_date(raw: Optional[str]) -> float:
    """Parse an HTTP Date header into a UTC POSIX timestamp."""
    if not raw or not raw.strip():
        raise ValueError("Missing HTTP Date header")
    parsed = email.utils.parsedate_to_datetime(raw.strip())
    if parsed is None:
        raise ValueError(f"Invalid HTTP Date header: {raw!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def calculate_offset(remote_epoch: float, local_epoch: float) -> float:
    """Compute clock offset in seconds (positive means local clock is behind)."""
    return remote_epoch - local_epoch


def fetch_remote_time(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    method: str = "GET",
    opener: Callable[..., Any] = urllib.request.urlopen,
    time_fn: Callable[[], float] = time.time,
) -> tuple[float, float, float]:
    """Query the target URL and return (remote_epoch, local_midpoint, rtt_seconds)."""
    validated = validate_url(url)
    request = urllib.request.Request(
        validated,
        method=method,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "lite-actions-timesync-guard/1",
        },
    )
    started = time_fn()
    date_header: Optional[str] = None
    try:
        with opener(request, timeout=timeout) as response:
            date_header = response.headers.get("Date")
            finished = time_fn()
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 405 and exc.headers is not None:
                date_header = exc.headers.get("Date")
                finished = time_fn()
            else:
                raise
        finally:
            exc.close()
        if not date_header:
            raise
    remote_epoch = parse_http_date(date_header)
    local_midpoint = (started + finished) / 2.0
    rtt_seconds = max(0.0, finished - started)
    return remote_epoch, local_midpoint, rtt_seconds


def is_ntp_synchronized(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> Optional[bool]:
    """Check whether systemd reports the system clock as NTP-synchronized."""
    try:
        show_proc = runner(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("Failed to run timedatectl show: %s", type(exc).__name__)
        return None

    if show_proc.returncode == 0:
        value = (show_proc.stdout or "").strip().lower()
        if value == "yes":
            return True
        if value == "no":
            return False

    try:
        status_proc = runner(
            ["timedatectl"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("Failed to run timedatectl fallback: %s", type(exc).__name__)
        return None

    if status_proc.returncode == 0:
        match = _SYNC_LINE_RE.search(status_proc.stdout or "")
        if match:
            return match.group(1).lower() == "yes"
    return None


def apply_clock_epoch(
    target_epoch: float,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> int:
    """Set the UTC system clock via `date -u -s @<epoch>`."""
    epoch_int = int(round(target_epoch))
    runner(
        ["date", "-u", "-s", f"@{epoch_int}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return epoch_int


def evaluate_and_sync(
    url: str,
    *,
    threshold: float = DEFAULT_THRESHOLD_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    method: str = "GET",
    apply: bool = False,
    force_step: bool = False,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    time_fn: Callable[[], float] = time.time,
) -> CheckResult:
    """Measure clock offset against URL and adjust the clock only when safe."""
    remote_epoch, local_midpoint, rtt_seconds = fetch_remote_time(
        url,
        timeout=timeout,
        method=method,
        opener=opener,
        time_fn=time_fn,
    )
    offset = calculate_offset(remote_epoch, local_midpoint)
    ntp_synced = is_ntp_synchronized(runner=runner)

    applied_epoch: Optional[int] = None
    if abs(offset) <= threshold:
        action = "ok"
    elif force_step or (apply and ntp_synced is False):
        target_epoch = remote_epoch + max(0.0, time_fn() - local_midpoint)
        applied_epoch = apply_clock_epoch(target_epoch, runner=runner)
        action = "adjusted"
    elif apply and ntp_synced is True:
        action = "skipped_ntp_synced"
    elif apply and ntp_synced is None:
        action = "skipped_ntp_unknown"
    else:
        action = "dry_run"

    ntp_label = "unknown" if ntp_synced is None else ("yes" if ntp_synced else "no")
    safe_url = sanitize_url_for_log(url)
    applied_suffix = (
        f" applied_epoch={applied_epoch}" if applied_epoch is not None else ""
    )
    LOG.info(
        "timesync url=%s remote_epoch=%.3f local_epoch=%.3f offset=%+.3fs "
        "rtt=%.3fs threshold=%.3fs ntp_synced=%s action=%s%s",
        safe_url,
        remote_epoch,
        local_midpoint,
        offset,
        rtt_seconds,
        threshold,
        ntp_label,
        action,
        applied_suffix,
    )
    return CheckResult(
        url=safe_url,
        remote_epoch=remote_epoch,
        local_epoch=local_midpoint,
        offset_seconds=offset,
        rtt_seconds=rtt_seconds,
        ntp_synchronized=ntp_synced,
        action=action,
        applied_epoch=applied_epoch,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Compare local system time against Control Center HTTPS Date header "
            "and optionally adjust the clock when NTP is not synchronized."
        )
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Control Center HTTPS URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one clock check and exit instead of running periodically.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            "Check interval in seconds in daemon mode "
            f"(default: {DEFAULT_INTERVAL_SECONDS:.0f})."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD_SECONDS,
        help=(
            "Maximum tolerated absolute clock skew in seconds before acting "
            f"(default: {DEFAULT_THRESHOLD_SECONDS:.1f})."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:.1f})."
        ),
    )
    parser.add_argument(
        "--method",
        choices=("GET", "HEAD"),
        default="GET",
        help="HTTP method used to read the Date header (default: GET).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Adjust the system clock with `date -u -s @<epoch>` when "
            "|offset| > threshold and NTP is not synchronized."
        ),
    )
    parser.add_argument(
        "--force-step",
        action="store_true",
        help=(
            "Adjust the system clock when |offset| > threshold even if NTP "
            "reports synchronized or unknown."
        ),
    )
    return parser


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.threshold < 0:
        parser.error("--threshold must be non-negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        validate_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))

    _configure_logging()

    if args.once:
        try:
            evaluate_and_sync(
                args.url,
                threshold=args.threshold,
                timeout=args.timeout,
                method=args.method,
                apply=args.apply,
                force_step=args.force_step,
            )
        except Exception as exc:
            LOG.error("timesync check failed: %s: %s", type(exc).__name__, exc)
            return 1
        return 0

    stop_event = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _signum, _frame: stop_event.set())

    while not stop_event.is_set():
        try:
            evaluate_and_sync(
                args.url,
                threshold=args.threshold,
                timeout=args.timeout,
                method=args.method,
                apply=args.apply,
                force_step=args.force_step,
            )
        except Exception as exc:
            LOG.warning("timesync check failed: %s: %s", type(exc).__name__, exc)
        stop_event.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
