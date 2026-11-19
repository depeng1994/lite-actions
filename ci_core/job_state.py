"""Durable run state shared by the poller and the dispatcher reporter."""

import fcntl
import json
import os
import threading
import time
from pathlib import Path

_MUTEX = threading.RLock()
_META = "__lite_actions_metadata__"


def _apply_fields(record, fields):
    phase = fields.get("phase")
    if phase and phase != record.get("phase") and phase != "REPORTED":
        stamp = "FINISHED" if phase == "PENDING_REPORT" else phase
        record.setdefault("phase_timestamps", {})[stamp] = int(time.time())
    record.update(fields)


def queue_identity(record):
    item = record.get("item", {})
    return item.get("target_pool"), item.get("kind")


def ordered_queued(records):
    queued = sorted(
        ((k, r) for k, r in records.items() if r.get("phase") == "QUEUED"),
        key=lambda kv: (
            kv[1]["run"].get("created_at", ""),
            str(kv[1]["run"]["id"]),
            kv[1]["item"]["order"],
        ),
    )
    # Keep the legacy ordering and its positions relative to Smoke while
    # ordering only the Smoke positions by durable admission sequence.
    smoke = iter(
        sorted(
            (pair for pair in queued if pair[1]["item"]["kind"] == "npu_smoke"),
            key=lambda pair: pair[1].get("admission_seq", 0),
        )
    )
    schedule = [
        next(smoke) if r["item"]["kind"] == "npu_smoke" else (key, r)
        for key, r in queued
    ]

    by_queue = {}
    for index, (_, record) in enumerate(schedule):
        by_queue.setdefault(queue_identity(record), []).append(index)
    for positions in by_queue.values():
        members = [schedule[i] for i in positions]
        if not any("queue_rank" in record for _, record in members):
            continue
        ranked = sorted(
            members, key=lambda member: member[1].get("queue_rank", float("inf"))
        )
        for pos, member in zip(positions, ranked):
            schedule[pos] = member
    return schedule


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
                _apply_fields(item, fields)
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
                run_fields = identity_fields + (
                    "head_sha",
                    "path",
                    "_ci_pr_number",
                    "_ci_source_repo",
                )

                def values(source, keys):
                    return tuple(source.get(k) for k in keys)

                def plan_identity(record):
                    return values(record["run"], run_fields), record.get("digest")

                identity = values(run, identity_fields)
                plan = plan_identity(entries[0][1])
                if len({key for key, _ in entries}) != len(entries) or any(
                    plan_identity(fields) != plan for _, fields in entries
                ):
                    raise ValueError("inconsistent admission batch")
                for k, record in current.items():
                    previous = record.get("run", {})
                    if (
                        k != _META
                        and values(previous, identity_fields) == identity
                        and (values(previous, run_fields), record.get("digest")) != plan
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
                ranks = {}
                for key, record in current.items():
                    if key != _META and "queue_rank" in record:
                        queue = queue_identity(record)
                        ranks[queue] = max(ranks.get(queue, 0), record["queue_rank"])
                inserted = []
                for key, fields in entries:
                    existing = current.get(key)
                    if existing is not None:
                        if plan_identity(existing) != plan_identity(fields) or values(
                            existing, ("item", "defaults")
                        ) != values(fields, ("item", "defaults")):
                            raise ValueError("conflicting previously admitted case")
                        continue
                    current[key] = {"admission_seq": seq}
                    _apply_fields(current[key], fields)
                    queue = queue_identity(fields)
                    if queue in ranks:
                        ranks[queue] += 1
                        current[key]["queue_rank"] = ranks[queue]
                    inserted.append(key)
                    seq += 1
                meta["next_sequence"] = seq
                if inserted or legacy or not had_meta:
                    self._write(current)
                return inserted

    def move_up(self, key: str, command: str) -> dict:
        """Commit adjacent swap and command outcome in the same generation."""
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                meta = current.setdefault(
                    _META,
                    {
                        "phase": "REPORTED",
                        "next_sequence": 1
                        + max(
                            (r.get("admission_seq", 0) for r in current.values()),
                            default=0,
                        ),
                    },
                )
                commands = meta.setdefault("control_moves", {})
                if command in commands:
                    return commands[command]
                record = current.get(key, {})
                pool, kind = queue_identity(record)
                if record.get("phase") != "QUEUED" or kind not in {
                    "train",
                    "npu_smoke",
                }:
                    result = {"status": "CONFLICT", "err": "TASK_NOT_QUEUED"}
                else:
                    queue = [
                        pair
                        for pair in ordered_queued(current)
                        if queue_identity(pair[1]) == (pool, kind)
                    ]
                    pos = next(
                        index
                        for index, (candidate, _) in enumerate(queue)
                        if candidate == key
                    )
                    if not pos:
                        result = {"status": "NOOP", "pos": 0}
                    else:
                        lower = max(
                            (
                                r.get("queue_rank", 0)
                                for r in current.values()
                                if queue_identity(r) == (pool, kind)
                            ),
                            default=0,
                        )
                        for rank, (_, other) in enumerate(queue, lower + 1):
                            other["queue_rank"] = rank
                        other = queue[pos - 1][1]
                        record["queue_rank"], other["queue_rank"] = (
                            other["queue_rank"],
                            record["queue_rank"],
                        )
                        result = {"status": "APPLIED", "pos": pos - 1}
                commands[command] = result
                self._write(current)
                return result

    def update_many(self, keys: list[str], **fields) -> None:
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                for key in keys:
                    if key not in current:
                        raise ValueError("missing admitted case")
                    _apply_fields(current[key], fields)
                self._write(current)
