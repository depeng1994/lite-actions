"""Durable run state shared by the poller and the dispatcher reporter."""

import json
import os
import threading
from pathlib import Path

_MUTEX = threading.RLock()


class JobState:
    def __init__(self, root: Path):
        self.path = root / "state" / "unified-jobs.json"

    def read(self) -> dict:
        with _MUTEX:
            return json.loads(self.path.read_text()) if self.path.exists() else {}

    def update(self, key: str, **fields) -> None:
        with _MUTEX:
            current = self.read()
            item = current.get(key, {})
            item.update(fields)
            current[key] = item
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w") as f:
                f.write(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
