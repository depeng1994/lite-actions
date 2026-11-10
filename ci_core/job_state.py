"""Durable run state shared by the poller and the dispatcher reporter."""

import fcntl
import json
import os
import threading
from pathlib import Path

_MUTEX = threading.RLock()
_META = "__lite_actions_metadata__"


class JobState:
    def __init__(self, root: Path):
        configured = os.environ.get("LITE_ACTIONS_STATE_DIR")
        directory = Path(configured).expanduser() if configured else root / "state"
        if configured and not directory.is_absolute():
            raise ValueError("LITE_ACTIONS_STATE_DIR must be absolute")
        self.path = directory / "unified-jobs.json"

    def _read_raw(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def read(self) -> dict:
        with _MUTEX:
            return {k: v for k, v in self._read_raw().items() if k != _META}

    def _write(self, current: dict) -> None:
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

    def update(self, key: str, **fields) -> None:
        if key == _META:
            raise ValueError("reserved state key")
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                item = current.get(key, {})
                item.update(fields)
                current[key] = item
                self._write(current)

    def admit(self, entries: list[tuple[str, dict]]) -> list[str]:
        """Persist a whole worker-owned plan and its queue order atomically."""
        if not entries:
            return []
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                had_meta = _META in current
                existing_sequences = [
                    r["admission_seq"]
                    for k, r in current.items()
                    if k != _META and "admission_seq" in r
                ]
                if any(type(n) is not int or n < 1 for n in existing_sequences):
                    raise ValueError("invalid persisted admission sequence")
                # An older binary can rewrite this state file during rollback and
                # omit metadata. Retain all previously assigned sequence numbers.
                meta = current.setdefault(
                    _META,
                    {
                        "phase": "REPORTED",
                        "next_sequence": max(existing_sequences, default=0) + 1,
                    },
                )
                seq = meta.get("next_sequence")
                if type(seq) is not int or seq < 1:
                    raise ValueError("invalid persisted admission sequence")
                if existing_sequences and seq <= max(existing_sequences):
                    raise ValueError("inconsistent persisted admission sequence")
                run = entries[0][1]["run"]
                identity_fields = (
                    "_ci_provider",
                    "_ci_repo",
                    "id",
                    "run_attempt",
                    "_ci_dispatch_id",
                )
                identity = tuple(run.get(k) for k in identity_fields)
                if len({key for key, _ in entries}) != len(entries) or any(
                    tuple(fields["run"].get(k) for k in identity_fields) != identity
                    or fields["run"].get("head_sha") != run.get("head_sha")
                    or fields["run"].get("path") != run.get("path")
                    or fields["run"].get("_ci_pr_number") != run.get("_ci_pr_number")
                    or fields["run"].get("_ci_source_repo")
                    != run.get("_ci_source_repo")
                    or fields.get("digest") != entries[0][1].get("digest")
                    for _, fields in entries
                ):
                    raise ValueError("inconsistent admission batch")
                for k, record in current.items():
                    previous = record.get("run", {})
                    if (
                        k != _META
                        and tuple(previous.get(k) for k in identity_fields) == identity
                    ):
                        if (
                            previous.get("head_sha") != run.get("head_sha")
                            or previous.get("path") != run.get("path")
                            or previous.get("_ci_pr_number") != run.get("_ci_pr_number")
                            or previous.get("_ci_source_repo")
                            != run.get("_ci_source_repo")
                            or record.get("digest") != entries[0][1]["digest"]
                        ):
                            raise ValueError("conflicting previously admitted run")
                legacy = sorted(
                    (
                        (k, r)
                        for k, r in current.items()
                        if k != _META
                        and r.get("phase") == "QUEUED"
                        and "admission_seq" not in r
                    ),
                    key=lambda kv: (
                        kv[1]["run"].get("created_at", ""),
                        str(kv[1]["run"]["id"]),
                        kv[1]["item"]["order"],
                    ),
                )
                for _, record in legacy:
                    record["admission_seq"] = seq
                    seq += 1
                inserted = []
                for key, fields in entries:
                    existing = current.get(key)
                    if existing is not None:
                        if any(
                            existing.get(k) != fields.get(k)
                            for k in ("digest", "item", "defaults")
                        ) or any(
                            existing.get("run", {}).get(k) != fields["run"].get(k)
                            for k in (
                                "_ci_provider",
                                "_ci_repo",
                                "id",
                                "run_attempt",
                                "_ci_dispatch_id",
                                "head_sha",
                                "path",
                                "_ci_pr_number",
                                "_ci_source_repo",
                            )
                        ):
                            raise ValueError("conflicting previously admitted case")
                        continue
                    current[key] = {**fields, "admission_seq": seq}
                    inserted.append(key)
                    seq += 1
                meta["next_sequence"] = seq
                if inserted or legacy or not had_meta:
                    self._write(current)
                return inserted

    def update_many(self, keys: list[str], **fields) -> None:
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                for key in keys:
                    if key not in current:
                        raise ValueError("missing admitted case")
                    current[key].update(fields)
                self._write(current)
