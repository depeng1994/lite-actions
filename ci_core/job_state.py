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


def workflow_identity(record):
    run = record.get("run", {})
    return ":".join(
        (
            str(run.get("_ci_provider", "github")),
            str(run.get("_ci_repo", "")),
            str(run.get("id", "")),
            str(run.get("run_attempt", 1)),
        )
    )


def workflow_order_key(identity):
    if isinstance(identity, str):
        return identity
    return ":".join(
        (
            str(identity.get("provider", "github")),
            str(identity.get("repo", "")),
            str(identity.get("run_id", "")),
            str(identity.get("attempt", 1)),
        )
    )


_TERMINAL_PHASES = {"PENDING_REPORT", "REPORTED"}


def _prune_workflow_order(current, order, *, keep=()):
    keep_set = set(keep)
    has_active = set()
    has_any = set()
    for key, record in current.items():
        if key == _META:
            continue
        wf = workflow_identity(record)
        has_any.add(wf)
        if record.get("phase") not in _TERMINAL_PHASES:
            has_active.add(wf)
    pruned = []
    seen = set()
    for wf in order:
        if wf in seen:
            continue
        if wf in keep_set or wf in has_active or wf not in has_any:
            pruned.append(wf)
            seen.add(wf)
    return pruned[-128:]


def _skip_workflow_priority(record):
    item = record.get("item", {})
    kind = item.get("kind")
    if kind in {"worker_probe", "test_worker_deploy", "test_worker_cleanup"}:
        return True
    worker = item.get("worker")
    if isinstance(worker, str):
        from ci_core.contract import parse_worker_identity

        try:
            if parse_worker_identity(worker)["is_test"]:
                return True
        except ValueError:
            pass
    return False


def apply_workflow_order(current, meta):
    raw_order = meta.get("workflow_order", [])
    if not raw_order:
        return
    pruned = _prune_workflow_order(current, raw_order)
    meta["workflow_order"] = pruned
    order = {key: index for index, key in enumerate(pruned)}
    if not order:
        return
    schedule = ordered_queued(current)
    queues = {}
    for key, record in schedule:
        if _skip_workflow_priority(record):
            continue
        queues.setdefault(queue_identity(record), []).append((key, record))
    for members in queues.values():
        indexed = list(enumerate(members))
        indexed.sort(
            key=lambda entry: (
                order.get(workflow_identity(entry[1][1]), 10**9),
                entry[0],
            )
        )
        for rank, (_, (_, record)) in enumerate(indexed, 1):
            record["queue_rank"] = rank


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
                if fields.get("phase") == "QUEUED":
                    apply_workflow_order(
                        current,
                        current.setdefault(
                            _META, {"phase": "REPORTED", "next_sequence": 1}
                        ),
                    )
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
                    "_ci_plan_stage",
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
                apply_workflow_order(current, meta)
                if inserted or legacy or not had_meta:
                    self._write(current)
                return inserted

    def swap_workflow(self, payload: dict, command: str) -> dict:
        """Apply one durable Workflow-level exchange exactly once."""
        with _MUTEX:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self._read_raw()
                meta = current.setdefault(
                    _META, {"phase": "REPORTED", "next_sequence": 1}
                )
                applied = meta.setdefault("applied_priority_ops", {})
                operation = str(payload.get("op_id") or command)
                if operation in applied:
                    return applied[operation]
                target = workflow_order_key(payload.get("a", {}))
                predecessor = workflow_order_key(payload.get("b", {}))

                # Detect CONFLICT when target has already left QUEUED on this
                # worker (e.g. entered in-flight or finished) with no remaining
                # queued non-probe cases.
                target_queued = False
                target_non_queued_pools = []
                for key, record in current.items():
                    if (
                        key == _META
                        or _skip_workflow_priority(record)
                        or workflow_identity(record) != target
                    ):
                        continue
                    if record.get("phase") == "QUEUED":
                        target_queued = True
                    else:
                        pool, kind = queue_identity(record)
                        if (pool, kind) not in target_non_queued_pools:
                            target_non_queued_pools.append((pool, kind))
                if not target_queued and target_non_queued_pools:
                    result = {
                        "status": "CONFLICT",
                        "pools": [
                            {"p": pool, "k": kind, "st": "CONFLICT", "c": 0}
                            for pool, kind in target_non_queued_pools
                        ],
                    }
                    applied[operation] = result
                    applied[command] = result
                    self._write(current)
                    return result

                schedule = ordered_queued(current)
                queues = {}
                for key, record in schedule:
                    if _skip_workflow_priority(record):
                        continue
                    queues.setdefault(queue_identity(record), []).append((key, record))

                # Seed workflow_order with all currently active non-probe
                # workflows in their existing relative order BEFORE swapping so
                # earlier workflows (e.g. W1) are never demoted on requeue.
                order = _prune_workflow_order(
                    current,
                    meta.get("workflow_order", []),
                    keep=(target, predecessor),
                )
                in_flight = sorted(
                    (
                        (k, r)
                        for k, r in current.items()
                        if k != _META
                        and r.get("phase") not in (_TERMINAL_PHASES | {"QUEUED"})
                        and r.get("item", {}).get("kind") != "worker_probe"
                    ),
                    key=lambda pair: (
                        pair[1].get("queue_rank", 10**9),
                        pair[1].get("admission_seq", 10**9),
                    ),
                )
                for _, record in [*in_flight, *schedule]:
                    if record.get("item", {}).get("kind") == "worker_probe":
                        continue
                    wf = workflow_identity(record)
                    if wf not in order:
                        order.append(wf)
                if predecessor in order:
                    if target not in order or order.index(target) > order.index(
                        predecessor
                    ):
                        if target in order:
                            order.remove(target)
                        order.insert(order.index(predecessor), target)
                elif target in order:
                    order.insert(order.index(target) + 1, predecessor)
                else:
                    order.extend([target, predecessor])
                meta["workflow_order"] = order

                receipts = []
                changed = False
                for (pool, kind), members in queues.items():
                    target_indices = [
                        i
                        for i, (_, row) in enumerate(members)
                        if workflow_identity(row) == target
                    ]
                    pred_indices = [
                        i
                        for i, (_, row) in enumerate(members)
                        if workflow_identity(row) == predecessor
                    ]
                    if not target_indices or not pred_indices:
                        receipts.append({"p": pool, "k": kind, "st": "NOOP", "c": 0})
                        continue
                    if max(target_indices) < min(pred_indices):
                        receipts.append({"p": pool, "k": kind, "st": "NOOP", "c": 0})
                        continue
                    slots = sorted(target_indices + pred_indices)
                    placed = [members[i] for i in target_indices] + [
                        members[i] for i in pred_indices
                    ]
                    for slot, pair in zip(slots, placed):
                        members[slot] = pair
                    for rank, (_, row) in enumerate(members, 1):
                        row["queue_rank"] = rank
                    changed = True
                    receipts.append(
                        {
                            "p": pool,
                            "k": kind,
                            "st": "APPLIED",
                            "c": len(placed),
                        }
                    )
                result = {"status": "APPLIED" if changed else "NOOP", "pools": receipts}
                applied[operation] = result
                applied[command] = result
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
