"""Durable run state shared by the poller and the dispatcher reporter."""

import fcntl
import json
import os
import threading
from pathlib import Path

_MUTEX = threading.RLock()


class JobState:
    def __init__(self, root: Path):
        configured = os.environ.get("LITE_ACTIONS_STATE_DIR")
        directory = Path(configured).expanduser() if configured else root / "state"
        if configured and not directory.is_absolute():
            raise ValueError("LITE_ACTIONS_STATE_DIR must be absolute")
        self.path = directory / "unified-jobs.json"

    def read(self) -> dict:
        with _MUTEX:
            return json.loads(self.path.read_text()) if self.path.exists() else {}

    def update(self, key: str, **fields) -> None:
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self.read()
                item = current.get(key, {})
                item.update(fields)
                current[key] = item
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
