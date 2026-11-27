"""Cross-process physical Ascend device leases on a shared dispatcher host.

Advisory locks coordinate *our* CI agents. They cannot block unrelated users.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from ci_core.permissions import PRIVATE_DIR_MODE


class ResourceBusy(RuntimeError):
    """One of the requested physical chips is already leased."""


class AgentAlreadyRunning(RuntimeError):
    """A second unified Dispatcher process attempted to start."""


def agent_lock_path() -> Path:
    configured = os.environ.get("LITE_ACTIONS_AGENT_LOCK")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "lite-actions" / "agent.lock"
    )
    if not path.is_absolute():
        raise ValueError("LITE_ACTIONS_AGENT_LOCK must be absolute")
    return path


def shared_resource_lock_root() -> Path:
    """Shared across dispatcher worktrees and processes of the same OS user."""
    configured = os.environ.get("LITE_ACTIONS_RESOURCE_LOCK_ROOT")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "lite-actions" / "resource-leases"
    )
    if not path.is_absolute():
        raise ValueError("LITE_ACTIONS_RESOURCE_LOCK_ROOT must be absolute")
    return path


def resource_keys(resources: Iterable[Mapping]) -> tuple[tuple[str, int], ...]:
    keys = set()
    for node in resources:
        host = node["host_id"]
        if not isinstance(host, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.:-]{0,79}", host
        ):
            raise ValueError("invalid canonical host identifier")
        for device in node["devices"]:
            if (
                isinstance(device, bool)
                or not isinstance(device, int)
                or not 0 <= device < 256
            ):
                raise ValueError("invalid physical NPU chip ID")
            keys.add((host, device))
    return tuple(sorted(keys))


@contextmanager
def reserve(
    lock_root: Path, resources: Iterable[Mapping]
) -> Iterator[tuple[tuple[str, int], ...]]:
    """All-or-nothing nonblocking lock, held until the context exits."""
    devices = resource_keys(resources)
    lock_root.mkdir(parents=True, exist_ok=True)
    held = []
    try:
        for host, device in devices:
            fd = (
                lock_root
                / f"{hashlib.sha256(host.encode()).hexdigest()}_chip-{device:03d}.lock"
            ).open("a+b")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                fd.close()
                raise ResourceBusy(f"{host}:chip-{device}") from exc
            held.append(fd)
        yield devices
    finally:
        for fd in reversed(held):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()


@contextmanager
def reserve_host_budget(
    lock_root: Path, resources: Iterable[Mapping], *, max_jobs_per_host: int
) -> Iterator[dict[str, int]]:
    """Atomically acquire one Host-capacity slot on every participating node.

    Unlike mutable case status, flock stops concurrent admission races before
    state has been persisted. A distributed allocation never holds half a Host
    quota after contention, and no running training is ever preempted.
    """
    if type(max_jobs_per_host) is not int or max_jobs_per_host not in (1, 2):
        raise ValueError("unsafe Host budget")
    hosts = sorted({host for host, _ in resource_keys(resources)})
    if not hosts:
        raise ValueError("host reservation requires physical devices")
    lock_root.mkdir(parents=True, exist_ok=True)
    held = []
    chosen = {}
    try:
        for host in hosts:
            for slot in range(max_jobs_per_host):
                fd = (lock_root / f"{host}_job-{slot:02d}.lock").open("a+b")
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fd.close()
                    continue
                held.append(fd)
                chosen[host] = slot
                break
            else:
                raise ResourceBusy(f"{host} Host budget full")
        yield chosen
    finally:
        for fd in reversed(held):
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()


def port_profile(slot: int) -> dict:
    """Exactly one static, mutually disjoint HCCL network port namespace."""
    if type(slot) is not int or not 0 <= slot < 4:
        raise ValueError("invalid approved port slot")
    host_base = 24000 + 128 * slot
    npu_base = 28000 + 128 * slot
    return {
        "master_port": 16000 + slot,
        "hccl_if_base_port": 20000 + 128 * slot,
        "hccl_host_port_range": f"{host_base}-{host_base + 63}",
        "hccl_npu_port_range": f"{npu_base}-{npu_base + 63}",
        "port_slot": slot,
    }


@contextmanager
def reserve_ports(lock_root: Path, resources: Iterable[Mapping]) -> Iterator[dict]:
    """Reserve one matching TCP/HCCL port namespace across every node.

    An occupied port slot on any participating node is rejected atomically.
    The lease remains held for the full remote job lifetime, independently of
    repository worktree or dispatcher worker. External users are not covered.
    """
    hosts = sorted({host for host, _ in resource_keys(resources)})
    if not hosts:
        raise ValueError("port reservation requires physical devices")
    lock_root.mkdir(parents=True, exist_ok=True)
    # Four separately reserved communication profiles, all outside the
    # dispatcher's host ephemeral-port range on verified A3 hosts. Never
    # enlarge without installed-CANN + real HCCL bind verification.
    for slot in range(4):
        held = []
        entered = False
        try:
            for host in hosts:
                fd = (lock_root / f"{host}_port-slot-{slot:03d}.lock").open("a+b")
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fd.close()
                    raise ResourceBusy(f"{host} rendezvous slot {slot}")
                held.append(fd)
            entered = True
            yield port_profile(slot)
            return
        except ResourceBusy:
            if entered:
                raise
            if slot == 3:
                raise ResourceBusy("all rendezvous slots in use")
        finally:
            for fd in reversed(held):
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()


@contextmanager
def single_agent(lock_path: Path | None = None) -> Iterator[None]:
    """Fail fast if another unified Dispatcher process is already active."""
    path = lock_path or agent_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    fd = path.open("a+b")
    acquired = False
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise AgentAlreadyRunning(
                f"another Lite Actions Dispatcher owns {path}"
            ) from exc
        yield
    finally:
        if acquired:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


class UnverifiedRemoteRevoke(RuntimeError):
    """Raised when a launched lease cannot prove all remote hosts are REVOKED."""


class CoordinatedLease:
    """Exclusive physical lease combining OS flocks with durable coordinator state."""

    def __init__(
        self,
        coordinator: "PhysicalResourceCoordinator",
        *,
        lease_id: str,
        owner_worker: str,
        owner_zone: str,
        is_production: bool,
        allocation: list[dict],
        host_budget_slots: dict[str, int],
        ports: dict,
        stack,
    ):
        self._coordinator = coordinator
        self.lease_id = lease_id
        self.owner_worker = owner_worker
        self.owner_zone = owner_zone
        self.is_production = is_production
        self.allocation = allocation
        self.host_budget_slots = host_budget_slots
        self.ports = ports
        self._stack = stack
        self.state = "RESERVED"
        self.remote_hosts: list[str] = []
        self.remote_nodes: list[dict] = []
        self._revoked_hosts: set[str] | None = None
        self._released = False

    def mark_launching(
        self,
        remote_nodes: list[dict] | None = None,
        *,
        remote_hosts: Iterable[str] | None = None,
    ) -> dict:
        doc = self._coordinator.mark_launching(
            self.lease_id,
            remote_nodes=remote_nodes,
            remote_hosts=remote_hosts,
        )
        self.state = doc["state"]
        self.remote_nodes = list(doc.get("remote_nodes") or [])
        self.remote_hosts = list(doc.get("remote_hosts") or [])
        return doc

    def mark_active(self) -> dict:
        doc = self._coordinator.mark_active(self.lease_id)
        self.state = doc["state"]
        return doc

    def set_remote_revoked_hosts(self, hosts: Iterable[str]) -> None:
        self._revoked_hosts = set(hosts)

    def release(
        self,
        remote_revoked_hosts: Iterable[str] | None = None,
        *,
        allow_unlaunched: bool = False,
    ) -> None:
        if self._released:
            return
        revoked = (
            set(remote_revoked_hosts)
            if remote_revoked_hosts is not None
            else self._revoked_hosts
        )
        try:
            self._coordinator.release(
                self.lease_id,
                remote_revoked_hosts=revoked,
                allow_unlaunched=allow_unlaunched,
            )
        finally:
            self._released = True
            if self._stack is not None:
                self._stack.close()
                self._stack = None

    def close(self) -> None:
        if self._released:
            return
        unlaunched = self.state == "RESERVED" and not self.remote_hosts
        try:
            self.release(allow_unlaunched=unlaunched)
        except UnverifiedRemoteRevoke:
            import logging

            logging.exception(
                "lease %s kept locked/quarantined due to unverified remote revoke",
                self.lease_id,
            )


class PhysicalResourceCoordinator:
    """Shared coordinator for production and test leases across A3 zones."""

    WAITER_TTL_SECONDS = 43200

    def __init__(self, lock_root: Path | None = None):
        self.lock_root = (
            Path(lock_root) if lock_root is not None else shared_resource_lock_root()
        )
        self.lock_path = self.lock_root / "coordinator.lock"
        self.active_dir = self.lock_root / "active-leases"
        self.quarantine_dir = self.lock_root / "quarantine"
        self.drain_path = self.lock_root / "drain-state.json"
        self.waiters_path = self.lock_root / "prod-waiters.json"

    def _ensure_dirs(self) -> None:
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_dirs()
        with self.lock_path.open("a+b") as fd:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_json(path: Path, default):
        import json

        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_json(path: Path, value) -> None:
        import json
        import tempfile

        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            temp = Path(tmp.name)
            try:
                tmp.write(encoded)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp.replace(path)
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                temp.unlink(missing_ok=True)

    @staticmethod
    def _resource_filename(host_id: str, device: int) -> str:
        import hashlib

        digest = hashlib.sha256(f"{host_id}\0{int(device)}".encode()).hexdigest()[:24]
        return f"res-{digest}.json"

    def _active_leases_unlocked(self) -> list[dict]:
        rows = []
        if not self.active_dir.exists():
            return rows
        for path in sorted(self.active_dir.glob("*.json")):
            doc = self._read_json(path, None)
            if isinstance(doc, dict) and doc.get("lease_id"):
                rows.append(doc)
        return rows

    def _quarantined_map_unlocked(self) -> dict[tuple[str, int], dict]:
        if self.quarantine_dir.exists():
            for path in sorted(self.quarantine_dir.glob("*.json")):
                path.unlink(missing_ok=True)
        return {}

    def _draining_set_unlocked(self) -> set[tuple[str, int]]:
        doc = self._read_json(self.drain_path, {})
        result: set[tuple[str, int]] = set()
        by_owner = doc.get("by_owner") if isinstance(doc, dict) else None
        if isinstance(by_owner, dict):
            for items in by_owner.values():
                if not isinstance(items, list):
                    continue
                for pair in items:
                    if (
                        isinstance(pair, (list, tuple))
                        and len(pair) == 2
                        and isinstance(pair[0], str)
                        and type(pair[1]) is int
                    ):
                        result.add((pair[0], pair[1]))
        return result

    def _load_waiters_unlocked(self, now: float) -> dict[str, dict]:
        doc = self._read_json(self.waiters_path, {})
        if not isinstance(doc, dict):
            return {}
        return {
            k: v
            for k, v in doc.items()
            if isinstance(v, dict)
            and now - float(v.get("updated_at", 0)) <= self.WAITER_TTL_SECONDS
        }

    def all_active_leases(self) -> list[dict]:
        with self._locked():
            return self._active_leases_unlocked()

    def active_leases_for_owner(self, owner_worker: str) -> list[dict]:
        with self._locked():
            return [
                doc
                for doc in self._active_leases_unlocked()
                if doc.get("owner_worker") == owner_worker
            ]

    def all_quarantined_resources(self) -> set[tuple[str, int]]:
        with self._locked():
            self._quarantined_map_unlocked()
            return set()

    def all_draining_resources(self) -> set[tuple[str, int]]:
        with self._locked():
            return self._draining_set_unlocked()

    def register_production_waiter(
        self,
        wait_key: str,
        *,
        owner_worker: str,
        pool: str,
        resources: Iterable[tuple[str, int]],
    ) -> None:
        import time

        if not wait_key:
            return
        now = time.time()
        normalized = sorted({(str(h), int(d)) for h, d in resources})
        if not normalized:
            return
        with self._locked():
            waiters = self._load_waiters_unlocked(now)
            waiters[wait_key] = {
                "owner_worker": owner_worker,
                "pool": pool,
                "resources": [[h, d] for h, d in normalized],
                "updated_at": now,
            }
            self._write_json(self.waiters_path, waiters)

    def clear_production_waiter(self, wait_key: str | None) -> None:
        import time

        if not wait_key:
            return
        with self._locked():
            waiters = self._load_waiters_unlocked(time.time())
            if wait_key in waiters:
                del waiters[wait_key]
                self._write_json(self.waiters_path, waiters)

    def clear_production_waiters_for_owner(self, owner_worker: str) -> None:
        import time

        with self._locked():
            waiters = self._load_waiters_unlocked(time.time())
            filtered = {
                k: v
                for k, v in waiters.items()
                if v.get("owner_worker") != owner_worker
            }
            if len(filtered) != len(waiters) or self.waiters_path.exists():
                self._write_json(self.waiters_path, filtered)

    def set_drain_state(
        self, worker: str, resources: Iterable[tuple[str, int]]
    ) -> None:
        import time

        pairs = sorted([str(h), int(d)] for h, d in set(resources))
        with self._locked():
            doc = self._read_json(self.drain_path, {})
            if not isinstance(doc, dict):
                doc = {}
            by_owner = dict(doc.get("by_owner") or {})
            if pairs:
                by_owner[worker] = pairs
            else:
                by_owner.pop(worker, None)
            self._write_json(
                self.drain_path, {"by_owner": by_owner, "updated_at": time.time()}
            )

    def clear_drain_state(self, worker: str) -> None:
        self.set_drain_state(worker, ())

    def check_delete_allowed(
        self, resources: Iterable[tuple[str, int]] | None = None
    ) -> None:
        target = set(resources) if resources is not None else None
        with self._locked():
            self._quarantined_map_unlocked()
            for doc in self._active_leases_unlocked():
                lease_res = set(resource_keys(doc.get("allocation") or []))
                if target is None or lease_res.intersection(target):
                    raise ValueError("ACTIVE_LEASES_REMAINING")

    def quarantine_lease(
        self,
        lease_id: str | None = None,
        *,
        allocation: list[dict] | None = None,
        owner_worker: str = "",
        task_key: str = "",
        reason: str = "cleanup_unverified",
        source: str = "worker",
    ) -> None:
        # Quarantine locks are disabled; cleanup relies on preemption instead.
        with self._locked():
            self._quarantined_map_unlocked()

    def clear_quarantine(
        self, resources: Iterable[tuple[str, int]] | None = None
    ) -> None:
        target = set(resources) if resources is not None else None
        with self._locked():
            if not self.quarantine_dir.exists():
                return
            for path in sorted(self.quarantine_dir.glob("*.json")):
                doc = self._read_json(path, None)
                if target is None or (
                    isinstance(doc, dict)
                    and (doc.get("host_id"), doc.get("device")) in target
                ):
                    path.unlink(missing_ok=True)

    def acquire(
        self,
        allocation: list[dict],
        *,
        owner_worker: str,
        owner_zone: str | None = None,
        pool: str,
        is_production: bool,
        task_key: str = "",
        max_jobs_per_host: int = 1,
        docker: str | None = None,
        wait_key: str | None = None,
        enforce_quarantine: bool = False,
        register_waiter_on_busy: bool = False,
    ) -> CoordinatedLease:
        import time
        import uuid
        from contextlib import ExitStack

        from ci_core.contract import worker_zone

        zone = owner_zone or worker_zone(owner_worker)
        req_keys = set(resource_keys(allocation))
        if not req_keys:
            raise ValueError("allocation cannot be empty")
        now = time.time()
        with self._locked():
            self._quarantined_map_unlocked()
            if self._draining_set_unlocked().intersection(req_keys):
                raise ResourceBusy("RESOURCE_DRAINING")
            for doc in self._active_leases_unlocked():
                if set(resource_keys(doc.get("allocation") or [])).intersection(
                    req_keys
                ):
                    if is_production and register_waiter_on_busy and wait_key:
                        waiters = self._load_waiters_unlocked(now)
                        waiters[wait_key] = {
                            "owner_worker": owner_worker,
                            "pool": pool,
                            "resources": [[h, d] for h, d in sorted(req_keys)],
                            "updated_at": now,
                        }
                        self._write_json(self.waiters_path, waiters)
                    raise ResourceBusy("RESOURCE_LEASED")
            if not is_production:
                waiters = self._load_waiters_unlocked(now)
                for waiter in waiters.values():
                    wait_res = {
                        (pair[0], pair[1])
                        for pair in waiter.get("resources", [])
                        if isinstance(pair, list) and len(pair) == 2
                    }
                    if wait_res.intersection(req_keys):
                        raise ResourceBusy("PRODUCTION_PRIORITY")
            stack = ExitStack()
            try:
                stack.enter_context(reserve(self.lock_root / "devices", allocation))
                budget = stack.enter_context(
                    reserve_host_budget(
                        self.lock_root / "host-budget",
                        allocation,
                        max_jobs_per_host=max_jobs_per_host,
                    )
                )
                ports = stack.enter_context(
                    reserve_ports(self.lock_root / "ports", allocation)
                )
            except ResourceBusy:
                stack.close()
                if is_production and register_waiter_on_busy and wait_key:
                    waiters = self._load_waiters_unlocked(now)
                    waiters[wait_key] = {
                        "owner_worker": owner_worker,
                        "pool": pool,
                        "resources": [[h, d] for h, d in sorted(req_keys)],
                        "updated_at": now,
                    }
                    self._write_json(self.waiters_path, waiters)
                raise
            except BaseException:
                stack.close()
                raise
            lease_id = uuid.uuid4().hex[:24]
            doc = {
                "lease_id": lease_id,
                "owner_worker": owner_worker,
                "owner_zone": zone,
                "is_production": bool(is_production),
                "task_key": task_key,
                "pool": pool,
                "docker": docker,
                "allocation": allocation,
                "host_budget_slots": budget,
                "ports": ports,
                "state": "RESERVED",
                "remote_hosts": [],
                "remote_nodes": [],
                "created_at": now,
                "updated_at": now,
            }
            self._write_json(self.active_dir / f"{lease_id}.json", doc)
            if is_production and wait_key:
                waiters = self._load_waiters_unlocked(now)
                if wait_key in waiters:
                    del waiters[wait_key]
                    self._write_json(self.waiters_path, waiters)
            return CoordinatedLease(
                self,
                lease_id=lease_id,
                owner_worker=owner_worker,
                owner_zone=zone,
                is_production=bool(is_production),
                allocation=allocation,
                host_budget_slots=budget,
                ports=ports,
                stack=stack,
            )

    def mark_launching(
        self,
        lease_id: str,
        *,
        remote_nodes: list[dict] | None = None,
        remote_hosts: Iterable[str] | None = None,
    ) -> dict:
        import time

        with self._locked():
            path = self.active_dir / f"{lease_id}.json"
            doc = self._read_json(path, None)
            if not isinstance(doc, dict):
                raise KeyError(f"unknown lease {lease_id}")
            if doc.get("state") not in {"RESERVED", "LAUNCHING"}:
                raise ValueError(
                    f"cannot transition lease {lease_id} from {doc.get('state')}"
                )
            nodes = (
                list(remote_nodes)
                if remote_nodes is not None
                else list(doc.get("remote_nodes") or [])
            )
            hosts = sorted(
                {
                    *(n["ssh"] for n in nodes if isinstance(n, dict) and n.get("ssh")),
                    *(str(h) for h in (remote_hosts or ())),
                    *(str(h) for h in (doc.get("remote_hosts") or ())),
                }
            )
            doc["state"] = "LAUNCHING"
            doc["remote_nodes"] = nodes
            doc["remote_hosts"] = hosts
            doc["updated_at"] = time.time()
            self._write_json(path, doc)
            return doc

    def mark_active(self, lease_id: str) -> dict:
        import time

        with self._locked():
            path = self.active_dir / f"{lease_id}.json"
            doc = self._read_json(path, None)
            if not isinstance(doc, dict):
                raise KeyError(f"unknown lease {lease_id}")
            if doc.get("state") not in {"RESERVED", "LAUNCHING", "ACTIVE"}:
                raise ValueError(
                    f"cannot activate lease {lease_id} from {doc.get('state')}"
                )
            doc["state"] = "ACTIVE"
            doc["updated_at"] = time.time()
            self._write_json(path, doc)
            return doc

    def release(
        self,
        lease_id: str,
        *,
        remote_revoked_hosts: Iterable[str] | None = None,
        allow_unlaunched: bool = False,
    ) -> None:
        import time

        with self._locked():
            path = self.active_dir / f"{lease_id}.json"
            doc = self._read_json(path, None)
            if not isinstance(doc, dict):
                return
            state = doc.get("state")
            required = set(doc.get("remote_hosts") or ())
            if allow_unlaunched and state == "RESERVED" and not required:
                path.unlink(missing_ok=True)
                return
            revoked = (
                set(remote_revoked_hosts) if remote_revoked_hosts is not None else set()
            )
            if remote_revoked_hosts is None or not required.issubset(revoked):
                missing = (
                    sorted(required - revoked)
                    if required
                    else ["remote_revocation_unverified"]
                )
                doc["state"] = "REVOKE_FAILED"
                doc["missing_revoked_hosts"] = missing
                doc["updated_at"] = time.time()
                self._write_json(path, doc)
                raise UnverifiedRemoteRevoke(
                    "remote lease revocation unverified for " + ",".join(missing)
                )
            path.unlink(missing_ok=True)

    def reconcile_quarantined_leases(self, *, revoke_fn=None) -> list[str]:
        """Re-verify REVOKE_FAILED leases and automatically clear recovered quarantines."""
        import logging
        import time

        if revoke_fn is None:
            from ci_core.ssh_runner import revoke_remote_lease as revoke_fn

        with self._locked():
            failed_leases = [
                dict(doc)
                for doc in self._active_leases_unlocked()
                if doc.get("state") == "REVOKE_FAILED" and doc.get("lease_id")
            ]
            active_ids = {
                doc.get("lease_id")
                for doc in self._active_leases_unlocked()
                if doc.get("lease_id")
            }
            if self.quarantine_dir.exists():
                for qpath in sorted(self.quarantine_dir.glob("*.json")):
                    qdoc = self._read_json(qpath, None)
                    if (
                        isinstance(qdoc, dict)
                        and qdoc.get("source") == "coordinator_revoke_failed"
                        and qdoc.get("lease_id")
                        and qdoc.get("lease_id") not in active_ids
                    ):
                        qpath.unlink(missing_ok=True)

        recovered: list[str] = []
        for doc in failed_leases:
            lease_id = doc["lease_id"]
            remote_nodes = list(doc.get("remote_nodes") or [])
            missing = set(doc.get("missing_revoked_hosts") or ())
            required = set(doc.get("remote_hosts") or ())
            if not missing and required:
                missing = set(required)
            pending_nodes = [
                n
                for n in remote_nodes
                if isinstance(n, dict) and n.get("ssh") in missing
            ]
            if not pending_nodes and remote_nodes and missing:
                pending_nodes = remote_nodes
            revoked: set[str] = set()
            if pending_nodes:
                try:
                    revoked = set(
                        revoke_fn(
                            {
                                "lease_id": lease_id,
                                "remote_nodes": pending_nodes,
                                "remote_lease_dir": doc.get("remote_lease_dir"),
                            },
                            check_npu=False,
                        )
                    )
                except Exception:
                    logging.warning(
                        "deferred quarantine reconcile retry failed for lease %s",
                        lease_id,
                    )
                    continue
            remaining = sorted(missing - revoked)
            with self._locked():
                path = self.active_dir / f"{lease_id}.json"
                current = self._read_json(path, None)
                if (
                    not isinstance(current, dict)
                    or current.get("state") != "REVOKE_FAILED"
                ):
                    continue
                if not remaining:
                    path.unlink(missing_ok=True)
                    if self.quarantine_dir.exists():
                        for qpath in sorted(self.quarantine_dir.glob("*.json")):
                            qdoc = self._read_json(qpath, None)
                            if (
                                isinstance(qdoc, dict)
                                and qdoc.get("lease_id") == lease_id
                            ):
                                qpath.unlink(missing_ok=True)
                    recovered.append(lease_id)
                    logging.info(
                        "automatically recovered quarantined lease %s after remote revoke succeeded",
                        lease_id,
                    )
                elif len(remaining) < len(missing):
                    current["missing_revoked_hosts"] = remaining
                    current["updated_at"] = time.time()
                    self._write_json(path, current)
        return recovered
