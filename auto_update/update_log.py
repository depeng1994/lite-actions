"""Local management-plane update events; never copy business logs."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


def record(path: Path, event: str, **fields: object) -> None:
    """Only callers' allowlisted values reach the journal or the JSONL file."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    text = json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("management log must be regular")
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (text + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(text, flush=True)
