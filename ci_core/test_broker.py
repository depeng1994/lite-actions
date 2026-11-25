"""Isolated Unix-socket broker separating untrusted Test Workers from tokens, SSH, and locks."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import logging
import os
import pwd
import socket
import socketserver
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from ci_core.config import runtime_case
from ci_core.contract import (
    PR_SMOKE_WORKFLOW,
    REPO,
    SELF_REPO,
    is_valid_worker_id,
    parse_plan,
    parse_worker_identity,
)
from ci_core.egress_policy import Deferred
from ci_core.executor import ExternalNpuOccupancy, execute
from ci_core.gitcode import RunChanged
from ci_core.plan import verify_definition
from ci_core.registry import validate_case_environment
from ci_core.reporting import case_passed, post, progress, report
from ci_core.resources import ResourceBusy
from ci_core.scheduler import candidates
from ci_core.ssh_runner import model_archive, remote_lease_dir, revoke_remote_lease
from ci_core.worker_probe import probe as probe_worker

MAX_FRAME_BYTES = 512 * 1024
ALLOWED_OPS = frozenset(
    {
        "HELLO",
        "POLL_RUNS",
        "FETCH_PLAN",
        "WORKFLOW_STATUS",
        "POST_PROGRESS",
        "REQUEST_RUN",
        "CANCEL_RUN",
        "REPORT_CONTROL_EVENTS",
    }
)
FORBIDDEN_OVERRIDE_KEYS = frozenset(
    {"allocation", "host_ssh", "ssh", "lease_id", "remote_lease_dir", "ports"}
)


class LeaseConfinementViolation(ValueError):
    """Raised when a Test Worker attempts an unauthorized or out-of-scope action."""


def _copy_allocation(allocation: list[dict] | None) -> list[dict]:
    return [
        {"host_id": h["host_id"], "devices": list(h["devices"])}
        for h in (allocation or [])
    ]


def _sanitize_pools(pools: dict) -> dict:
    cleaned = copy.deepcopy(pools)
    for pool in cleaned.values():
        if not isinstance(pool, dict):
            continue
        for node in pool.get("nodes", []):
            if not isinstance(node, dict):
                continue
            node.pop("ssh", None)
            node.pop("host_ssh", None)
            for container in (node.get("environments") or {}).values():
                if isinstance(container, dict):
                    container.pop("ssh", None)
    return cleaned


def _sanitize_workers(workers: dict, zone: str) -> dict:
    src = workers.get(zone) or {}
    return {
        zone: {
            "case_environments": copy.deepcopy(src.get("case_environments") or {}),
            "expected_hostname": src.get("expected_hostname"),
        }
    }


def _sanitize_run(run: dict) -> dict:
    allowed_keys = (
        "id",
        "run_attempt",
        "name",
        "status",
        "head_sha",
        "created_at",
        "path",
        "event",
        "head_branch",
        "display_title",
        "run_number",
        "pull_requests",
        "head_repository",
        "actor",
        "html_url",
        "_ci_repo",
        "_ci_provider",
        "_ci_pr_number",
        "_ci_pr_author",
        "_ci_execution_sha",
        "_ci_dispatch_id",
        "_ci_source_repo",
        "_ci_source",
        "_ci_platform_run_sha",
        "_ci_platform_head_branch",
        "_ci_platform_event",
    )
    return {k: copy.deepcopy(run[k]) for k in allowed_keys if k in run}


def build_protected_baseline_archive(
    checkout_root: Path, dest_dir: Path, sha: str | None = None
) -> Path:
    """Package verified baseline code strictly from the Main Worker's protected checkout."""
    root = Path(checkout_root).resolve()
    archive_path = Path(dest_dir) / "protected-baseline.tar.gz"
    if (root / ".git").exists():
        ref = sha or "HEAD"
        check = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode == 0:
            proc = subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar", ref],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                with (
                    archive_path.open("wb") as raw_out,
                    gzip.GzipFile(
                        fileobj=raw_out, mode="wb", compresslevel=6, mtime=0
                    ) as gz_out,
                ):
                    gz_out.write(proc.stdout)
                return archive_path
    # Fallback for non-.git synthetic test trees on trusted Main Worker checkout.
    excluded_top = {
        ".git",
        "__pycache__",
        ".venv",
        ".pytest_cache",
        "cache",
        "control-center",
        "logs",
        "node_modules",
        "state",
    }

    def _normalize_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        return info

    with (
        archive_path.open("wb") as raw_out,
        gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=6, mtime=0) as gz_out,
        tarfile.open(fileobj=gz_out, mode="w") as tar,
    ):
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in excluded_top or part == "__pycache__" for part in rel.parts):
                continue
            tar.add(
                path,
                arcname=rel.as_posix(),
                recursive=False,
                filter=_normalize_tarinfo,
            )
    return archive_path


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class TestBrokerServer:
    """Host-side Unix domain socket broker run by the privileged Main Worker."""

    def __init__(
        self,
        *,
        socket_path: Path,
        worker_id: str,
        main_root: Path,
        state_dir: Path,
        pools_path: Path,
        pools: dict,
        pools_revision: int,
        workers: dict,
        author: str,
        address: str,
        release_sha: str,
        baseline_sha: str | None = None,
        client,
        coordinator,
        repos: tuple[str, ...] = (REPO,),
        gitcode_repos: tuple[str, ...] = (),
        scope: dict | None = None,
        control_sink=None,
        allowed_uid: int | None = None,
        allowed_user: str | None = None,
        allowed_unit: str | None = None,
        enforce_peercred: bool = True,
        peercred_reader=None,
        protected_baseline_root: Path | None = None,
        quarantine_enabled: bool = True,
        runs_provider=None,
        publish_retry_timeout: float = 120.0,
        publish_retry_interval: float = 2.5,
    ):
        if not is_valid_worker_id(worker_id):
            raise ValueError(f"invalid test worker id: {worker_id!r}")
        ident = parse_worker_identity(worker_id)
        if not ident["is_test"]:
            raise ValueError("TestBrokerServer requires a test worker identity")
        self.socket_path = Path(socket_path)
        self.worker_id = worker_id
        self.zone = ident["zone"]
        self.main_root = Path(main_root)
        self.state_dir = Path(state_dir)
        self.pools_path = Path(pools_path)
        self.pools = pools
        self.pools_revision = pools_revision
        self.workers = workers
        self.author = author
        self.address = address
        self.release_sha = release_sha
        self.baseline_sha = baseline_sha or release_sha
        self.client = client
        self.coordinator = coordinator
        self.repos = tuple(repos)
        self.gitcode_repos = tuple(gitcode_repos)
        self.scope = dict(scope or {})
        self.control_sink = control_sink
        self.allowed_uid = allowed_uid
        self.allowed_user = allowed_user
        self.allowed_unit = allowed_unit
        self.enforce_peercred = enforce_peercred
        self.peercred_reader = peercred_reader
        self.protected_baseline_root = Path(protected_baseline_root or self.main_root)
        self.quarantine_enabled = quarantine_enabled
        self.runs_provider = runs_provider
        self.publish_retry_timeout = float(publish_retry_timeout)
        self.publish_retry_interval = float(publish_retry_interval)
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._uid_lock = threading.Lock()
        self._pinned_uid: int | None = None
        self._active_allocations: dict[str, list[dict]] = {}
        self._terminal_allocations: dict[str, list[dict]] = {}
        self._last_snapshot: dict | None = None
        self._alloc_lock = threading.RLock()
        self._plan_cache: dict[str, dict] = {}
        self._server: _ThreadedUnixServer | None = None
        self._thread: threading.Thread | None = None
        self.audit_log: list[dict] = []

    def client_for(self, run: dict):
        repo = run.get("_ci_repo", REPO)
        if hasattr(self.client, "for_run"):
            return self.client.for_run(run)
        if hasattr(self.client, "for_repo"):
            return self.client.for_repo(repo)
        return self.client

    def _result_author(self, run: dict) -> str:
        if run.get("_ci_provider") == "gitcode":
            return os.environ.get("LITE_ACTIONS_GITCODE_RESULT_AUTHOR", self.author)
        return self.author

    def pin_allowed_uid(self, uid: int) -> None:
        if type(uid) is not int or uid <= 0:
            raise ValueError(f"invalid dynamic uid: {uid!r}")
        with self._uid_lock:
            if self._pinned_uid is not None and self._pinned_uid != uid:
                raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
            self._pinned_uid = uid

    def cancel_all(self) -> None:
        with self._cancel_lock:
            for ev in self._cancel_events.values():
                ev.set()

    def _expected_uid(self) -> int:
        if self.allowed_uid is not None:
            return self.allowed_uid
        lookup_user = self.allowed_user or self.allowed_unit
        if lookup_user:
            try:
                return pwd.getpwnam(lookup_user).pw_uid
            except KeyError as exc:
                raise PermissionError(
                    f"LEASE_CONFINEMENT_VIOLATION: unknown systemd unit user {lookup_user}"
                ) from exc
        return os.getuid()

    def _read_peercred(self, sock: socket.socket) -> tuple[int, int, int] | None:
        if self.peercred_reader is not None:
            return self.peercred_reader(sock)
        if hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            return struct.unpack("3i", raw)
        if hasattr(os, "getpeereid"):
            uid, gid = os.getpeereid()
            return (0, uid, gid)
        return None

    def _verify_peer(self, sock: socket.socket) -> None:
        if not self.enforce_peercred:
            return
        cred = self._read_peercred(sock)
        if cred is None:
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        pid, uid, _gid = cred
        if self.allowed_unit and self.allowed_uid is None and self.allowed_user is None:
            unit_name = (
                self.allowed_unit
                if self.allowed_unit.endswith(".service")
                else f"{self.allowed_unit}.service"
            )
            cg_path = Path(f"/proc/{pid}/cgroup")
            try:
                proc_uid = os.stat(f"/proc/{pid}").st_uid
                cg = cg_path.read_text(errors="replace") if cg_path.exists() else ""
            except OSError:
                proc_uid = -1
                cg = ""
            if pid <= 1 or uid <= 0 or proc_uid != uid or unit_name not in cg:
                self.audit_log.append(
                    {
                        "event": "cgroup_or_uid_rejected",
                        "pid": pid,
                        "uid": uid,
                        "proc_uid": proc_uid,
                        "expected_unit": unit_name,
                    }
                )
                raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
            with self._uid_lock:
                if self._pinned_uid is None:
                    self._pinned_uid = uid
                elif uid != self._pinned_uid:
                    self.audit_log.append(
                        {
                            "event": "peercred_rejected",
                            "pid": pid,
                            "uid": uid,
                            "expected_uid": self._pinned_uid,
                        }
                    )
                    raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
            return
        expected = self._expected_uid()
        if uid != expected:
            self.audit_log.append(
                {
                    "event": "peercred_rejected",
                    "pid": pid,
                    "uid": uid,
                    "expected_uid": expected,
                }
            )
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        if self.allowed_unit and pid > 0 and Path(f"/proc/{pid}/cgroup").exists():
            cg = Path(f"/proc/{pid}/cgroup").read_text(errors="replace")
            unit_name = (
                self.allowed_unit
                if self.allowed_unit.endswith(".service")
                else f"{self.allowed_unit}.service"
            )
            if unit_name not in cg:
                self.audit_log.append(
                    {
                        "event": "cgroup_rejected",
                        "pid": pid,
                        "uid": uid,
                        "expected_unit": unit_name,
                    }
                )
                raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")

    def _matches_scope(self, run: dict) -> bool:
        path = str(run.get("path") or "")
        if path.endswith("/lite_actions_self_smoke.yml"):
            return False
        if not self.scope:
            return True
        target_pr = self.scope.get("pr_number")
        target_repo = self.scope.get("repo")
        target_gitcode = self.scope.get("gitcode_repo")
        run_repo = run.get("_ci_repo", REPO)
        provider = run.get("_ci_provider", "github")
        if provider == "gitcode":
            if target_gitcode and run_repo != target_gitcode:
                return False
            dispatch_id = str(run.get("_ci_dispatch_id") or "")
            if self.scope.get("allow_gitcode_test_worker"):
                return True
            if target_pr and str(run.get("_ci_pr_number") or "") == str(target_pr):
                return True
            return self.worker_id in dispatch_id
        if target_repo and run_repo != target_repo:
            return False
        if self.scope.get("source_run_id") and int(run.get("id", -1)) == int(
            self.scope["source_run_id"]
        ):
            return True
        if target_pr is not None:
            pulls = run.get("pull_requests") or []
            if any(
                int(pr.get("number", -1)) == int(target_pr)
                for pr in pulls
                if isinstance(pr, dict)
            ):
                return True
            if str(run.get("_ci_pr_number") or "") == str(target_pr):
                return True
        return target_pr is None and not self.scope.get("source_run_id")

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self_handler):
                try:
                    broker._verify_peer(self_handler.request)
                    raw = self_handler.rfile.readline(MAX_FRAME_BYTES + 1)
                    if not raw or len(raw) > MAX_FRAME_BYTES:
                        resp = {
                            "ok": False,
                            "error": "LEASE_CONFINEMENT_VIOLATION",
                        }
                    else:
                        req = json.loads(raw.decode("utf-8"))
                        resp = broker.dispatch(req)
                except LeaseConfinementViolation as exc:
                    resp = {
                        "ok": False,
                        "error": str(exc) or "LEASE_CONFINEMENT_VIOLATION",
                    }
                except Deferred as exc:
                    logging.warning("broker request deferred: %s", exc)
                    resp = {
                        "ok": False,
                        "error_type": "Deferred",
                        "error": str(exc) or "DEFERRED",
                    }
                except Exception as exc:
                    logging.exception("broker request failed")
                    resp = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    }
                try:
                    encoded = (
                        json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8")
                    self_handler.wfile.write(encoded)
                    self_handler.wfile.flush()
                except OSError:
                    pass

        self._server = _ThreadedUnixServer(str(self.socket_path), Handler)
        sock_mode = (
            0o666
            if self.allowed_unit
            and self.allowed_uid is None
            and self.allowed_user is None
            else 0o600
        )
        os.chmod(self.socket_path, sock_mode)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="test-broker"
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.socket_path.unlink(missing_ok=True)

    def dispatch(self, req: dict) -> dict:
        if not isinstance(req, dict):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        op = req.get("op")
        if op not in ALLOWED_OPS:
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        payload = req.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        req_worker = payload.get("worker_id")
        if req_worker is not None and req_worker != self.worker_id:
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        if op == "HELLO":
            return {
                "ok": True,
                "result": {
                    "worker_id": self.worker_id,
                    "zone": self.zone,
                    "is_test": True,
                    "state_dir": str(self.state_dir),
                    "pools_path": str(self.pools_path),
                    "pools_revision": self.pools_revision,
                    "pools": _sanitize_pools(self.pools),
                    "workers": _sanitize_workers(self.workers, self.zone),
                    "author": self.author,
                    "address": self.address,
                    "release_sha": self.release_sha,
                    "repos": list(self.repos),
                    "gitcode_repos": list(self.gitcode_repos),
                },
            }
        if op == "POLL_RUNS":
            cached = self.runs_provider() if self.runs_provider is not None else None
            runs = cached if cached is not None else self.client.running_workflows()
            scoped = [_sanitize_run(r) for r in runs if self._matches_scope(r)]
            return {"ok": True, "result": {"runs": scoped}}
        if op == "FETCH_PLAN":
            return {"ok": True, "result": self._handle_fetch_plan(payload)}
        if op == "WORKFLOW_STATUS":
            return {"ok": True, "result": self._handle_workflow_status(payload)}
        if op == "POST_PROGRESS":
            return {"ok": True, "result": self._handle_post_progress(payload)}
        if op == "CANCEL_RUN":
            key = str(payload.get("key") or "")
            with self._cancel_lock:
                ev = self._cancel_events.get(key)
                if ev is not None:
                    ev.set()
            return {"ok": True, "result": {"cancelled": bool(ev)}}
        if op == "REQUEST_RUN":
            return {"ok": True, "result": self._handle_request_run(payload)}
        if op == "REPORT_CONTROL_EVENTS":
            events = payload.get("events") or []
            snapshot = self._merge_active_allocations(payload.get("snapshot"))
            deferred = False
            if self.control_sink is not None:
                try:
                    self.control_sink(self.worker_id, events=events, snapshot=snapshot)
                except Exception as exc:
                    deferred = True
                    logging.warning(
                        "test worker control telemetry deferred (%s)",
                        type(exc).__name__,
                    )
            return {
                "ok": True,
                "result": {"accepted": len(events), "deferred": deferred},
            }
        raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")

    def _merge_active_allocations(self, snapshot):
        with self._alloc_lock:
            if snapshot is not None:
                self._last_snapshot = copy.deepcopy(snapshot)
            base = copy.deepcopy(
                snapshot if snapshot is not None else self._last_snapshot
            )
            active = copy.deepcopy(self._active_allocations)
            terminal = copy.deepcopy(self._terminal_allocations)
        if not isinstance(base, dict) or (not active and not terminal):
            return base
        records = base.get("records")
        if isinstance(records, dict):
            for k, rec in records.items():
                if not isinstance(rec, dict) or rec.get("allocation"):
                    continue
                if k in active:
                    rec["allocation"] = active[k]
                elif k in terminal and rec.get("phase") != "QUEUED":
                    rec["allocation"] = terminal[k]
        return base

    def _set_active_allocation(self, key: str, allocation: list[dict] | None) -> None:
        with self._alloc_lock:
            if allocation is None:
                self._active_allocations.pop(key, None)
            else:
                self._active_allocations[key] = _copy_allocation(allocation)
            has_last = self._last_snapshot is not None
        snap = self._merge_active_allocations(None) if has_last else None
        if snap is not None and self.control_sink is not None:
            try:
                self.control_sink(self.worker_id, events=[], snapshot=snap)
            except Exception:
                pass

    def _mark_terminal_allocation(self, key: str, allocation: list[dict]) -> None:
        """Retain completed task allocation for terminal snapshots without emitting []."""
        with self._alloc_lock:
            self._active_allocations.pop(key, None)
            self._terminal_allocations[key] = _copy_allocation(allocation)

    def _handle_fetch_plan(self, payload: dict) -> dict:
        run = payload.get("run")
        if not isinstance(run, dict) or not self._matches_scope(run):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        client = self.client_for(run)
        if run.get("_ci_provider") != "gitcode":
            is_pr_smoke = (
                run.get("_ci_repo") == SELF_REPO
                and str(run.get("path") or "")
                == f".github/workflows/{PR_SMOKE_WORKFLOW}"
            )
            if is_pr_smoke:
                plan = client.ci_request(run)
                if plan is None:
                    return {"found": False}
                parse_plan(
                    plan,
                    run,
                    run["path"].rsplit("/", 1)[-1],
                    repo=run.get("_ci_repo", REPO),
                )
            else:
                from ci_core.github_source import (
                    bind_request_source,
                    read_source,
                    read_source_artifact,
                )

                generic_source = read_source_artifact(client, run)
                if generic_source is None:
                    return {"found": False}
                run = bind_request_source(client, run, generic_source)
                plan = client.ci_request(run)
                if plan is None:
                    return {"found": False}
                parse_plan(
                    plan,
                    run,
                    run["path"].rsplit("/", 1)[-1],
                    repo=run.get("_ci_repo", REPO),
                )
                source = read_source(client, run, plan, source=generic_source)
                if source is None:
                    return {"found": False}
                run = {
                    **run,
                    "_ci_source": source,
                    "_ci_execution_sha": source["source_sha"],
                }
        else:
            plan = client.ci_request(run)
            if plan is None:
                return {"found": False}
            parse_plan(
                plan,
                run,
                run["path"].rsplit("/", 1)[-1],
                repo=run.get("_ci_repo", REPO),
            )
        return {"found": True, "run": _sanitize_run(run), "plan": plan}

    def _handle_workflow_status(self, payload: dict) -> dict:
        run = payload.get("run")
        if not isinstance(run, dict) or not self._matches_scope(run):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        client = self.client_for(run)
        if run.get("_ci_provider") == "gitcode" and (
            run.get("_ci_pr_number") or run.get("_ci_dispatch_id")
        ):
            try:
                client.validate_current(run)
                return {
                    "status": "in_progress",
                    "run_attempt": run.get("run_attempt", 1),
                }
            except RunChanged:
                return {"status": "completed", "run_attempt": run.get("run_attempt", 1)}
        status = client.workflow_status(run["id"])
        if run.get("_ci_source"):
            from ci_core.github_source import check_current

            try:
                check_current(client, run["_ci_source"])
            except ValueError:
                return {
                    "status": "completed",
                    "run_attempt": status.get("run_attempt", 1),
                }
        return status

    def _handle_post_progress(self, payload: dict) -> dict:
        action = payload.get("action")
        run = payload.get("run")
        item = payload.get("item")
        digest = payload.get("digest")
        if (
            not isinstance(run, dict)
            or not isinstance(item, dict)
            or not isinstance(digest, str)
            or item.get("worker") != self.worker_id
            or not self._matches_scope(run)
        ):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        client = self.client_for(run)
        if action == "progress":
            phase = payload.get("phase")
            if phase not in {"QUEUED", "STAGING", "RUNNING", "CLEANING"}:
                raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
            ident = progress(
                client,
                run,
                item,
                digest,
                phase,
                payload.get("progress_id"),
                retry=bool(payload.get("retry")),
            )
            return {"progress_id": ident}
        if action == "terminal_non_pass":
            res = payload.get("result")
            if not isinstance(res, dict) or res.get("status") in (None, "PASS"):
                raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
            post(
                client,
                run,
                item,
                res,
                self._result_author(run),
                local_worker=self.worker_id,
                verify_only=bool(payload.get("verify_only")),
                retry=bool(payload.get("retry")),
            )
            return {"posted": True}
        raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")

    def _resolve_verified_env(self, run: dict, item: dict, case: dict, defaults: dict):
        if "env_vars" in item:
            raw_env = dict(item["env_vars"])
        else:
            with tempfile.TemporaryDirectory(prefix="lite-broker-def-") as tmp:
                source_repo = run.get("_ci_repo", REPO)
                if source_repo == SELF_REPO:
                    archive = build_protected_baseline_archive(
                        self.protected_baseline_root,
                        Path(tmp),
                        sha=self.baseline_sha or None,
                    )
                else:
                    archive = model_archive(
                        run.get("_ci_execution_sha", run["head_sha"]),
                        Path(tmp),
                        source_repo=source_repo,
                        provider=run.get("_ci_provider", "github"),
                        fallback_repo=run.get("_ci_source_repo"),
                        source_pr=(
                            run["_ci_pr_number"]
                            if run.get("_ci_source", {}).get("mode") == "merge_result"
                            else None
                        ),
                    )
                definition = verify_definition(
                    archive, item, defaults, repo=source_repo
                )
            raw_env = dict(definition["env_vars"])
        rules = (
            self.workers[self.zone].get("case_environments", {}).get(case["docker"], {})
        )
        keys = (
            ("ASCEND_SET_ENV_PATH", "HF_ASSETS_PATH")
            if item["kind"] == "train"
            else ("ASCEND_SET_ENV_PATH",)
        )
        for name in keys:
            allowed = rules.get(name)
            if name not in raw_env and isinstance(allowed, list) and allowed:
                raw_env[name] = allowed[0]
        environment = validate_case_environment(raw_env, kind=item["kind"])
        approved_setup = rules.get("ASCEND_SET_ENV_PATH")
        if type(approved_setup) is not list or (
            environment.get("ASCEND_SET_ENV_PATH") not in approved_setup
        ):
            raise ValueError("MODEL_ENV_NOT_APPROVED")
        if any(
            k in rules and (type(rules[k]) is not list or v not in rules[k])
            for k, v in environment.items()
        ):
            raise ValueError("MODEL_ENV_NOT_APPROVED")
        return environment

    def _publish_result(
        self,
        key: str,
        run: dict,
        item: dict,
        digest: str,
        status: str,
        rc: int,
        tail: list[str],
        release_verified: bool,
        locator: dict | None,
        *,
        allocation: list[dict] | None = None,
    ):
        res = report(run, item, digest, status, rc, tail, release_verified)
        if locator is not None:
            res["log_locator"] = locator
        res["finished_at"] = int(time.time())
        deadline = time.monotonic() + max(0.0, self.publish_retry_timeout)
        attempt = 0
        while True:
            try:
                post(
                    self.client_for(run),
                    run,
                    item,
                    res,
                    self._result_author(run),
                    local_worker=self.worker_id,
                    retry=attempt > 0,
                )
                break
            except Deferred as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                logging.warning(
                    "broker result publish deferred for %s (%s); retrying",
                    key,
                    exc,
                )
                time.sleep(min(max(0.0, self.publish_retry_interval), remaining))
                attempt += 1
        return {
            "status": status,
            "exit_code": rc,
            "tail": tail,
            "release_verified": release_verified,
            "locator": locator,
            "allocation": _copy_allocation(allocation),
            "published_by_broker": True,
        }

    def _handle_request_run(self, payload: dict) -> dict:
        if any(k in payload for k in FORBIDDEN_OVERRIDE_KEYS):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
        key = payload.get("key")
        run = payload.get("run")
        item = payload.get("item")
        digest = payload.get("digest")
        defaults = payload.get("defaults") or {}
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(run, dict)
            or not isinstance(item, dict)
            or not isinstance(digest, str)
            or item.get("worker") != self.worker_id
            or not self._matches_scope(run)
            or any(k in item for k in FORBIDDEN_OVERRIDE_KEYS)
        ):
            raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")

        cancel = threading.Event()
        with self._cancel_lock:
            self._cancel_events[key] = cancel
        with self._alloc_lock:
            self._terminal_allocations.pop(key, None)
        try:
            if item["kind"] == "worker_probe":
                if (
                    "env_vars" in item
                    or item.get("target_pool") is not None
                    or item.get("docker") is not None
                    or item.get("resources") != {"nodes": 0, "devices_per_node": 0}
                ):
                    raise ValueError("INVALID_WORKER_PROBE_ITEM")
                proc = probe_worker()
                expected = self.workers[self.zone].get("expected_hostname")
                if (
                    expected
                    and f"WORKER_HOSTNAME={expected}" not in proc.stdout.splitlines()
                ):
                    raise ValueError("worker identity mismatch")
                status = "PASS" if proc.returncode == 0 else "INFRA_ERROR"
                tail = (proc.stdout + proc.stderr).splitlines()[:20]
                return self._publish_result(
                    key, run, item, digest, status, proc.returncode, tail, True, None
                )

            try:
                case = runtime_case(item, self.pools, self.worker_id)
                if self.pools.get(case["pool"], {}).get("owner") != self.zone:
                    raise LeaseConfinementViolation("LEASE_CONFINEMENT_VIOLATION")
                environment = self._resolve_verified_env(run, item, case, defaults)
                case = {**case, "verified_env": environment}
                if run.get("_ci_source"):
                    case = {**case, "native_source": run["_ci_source"]}
                if run.get("_ci_provider") == "gitcode":
                    self.client_for(run).validate_current(run)
                if run.get("_ci_source"):
                    from ci_core.github_source import check_current

                    check_current(
                        self.client_for(run), run["_ci_source"], check_run=True
                    )
            except Deferred as exc:
                return {
                    "retry_busy": True,
                    "reason": f"DEFERRED:{exc}",
                    "retry_after": 15,
                    "allocation": [],
                }

            last_busy = "RESOURCE_BUSY"
            for allocation in candidates(case, self.pools):
                if cancel.is_set():
                    return self._publish_result(
                        key,
                        run,
                        item,
                        digest,
                        "CANCELLED",
                        130,
                        ["Cancelled before launch"],
                        True,
                        None,
                    )
                try:
                    lease = self.coordinator.acquire(
                        allocation,
                        owner_worker=self.worker_id,
                        owner_zone=self.zone,
                        pool=case["pool"],
                        is_production=False,
                        task_key=key,
                        max_jobs_per_host=self.pools[case["pool"]].get(
                            "max_jobs_per_host", 1
                        ),
                        docker=case.get("docker"),
                        enforce_quarantine=self.quarantine_enabled,
                    )
                except ResourceBusy as exc:
                    last_busy = str(exc) or "RESOURCE_BUSY"
                    continue
                leased_case = {
                    **case,
                    "allocation": allocation,
                    "pools_snapshot": {
                        case["pool"]: copy.deepcopy(self.pools[case["pool"]])
                    },
                    "host_budget_slots": lease.host_budget_slots,
                    "lease_id": lease.lease_id,
                    **lease.ports,
                }
                launched = False
                clean_alloc = _copy_allocation(leased_case["allocation"])
                terminal_allocation: list[dict] | None = None
                self._set_active_allocation(key, clean_alloc)
                try:
                    is_self_repo = run.get("_ci_repo", REPO) == SELF_REPO
                    incremental_ids = [
                        str(x)
                        for x in (item.get("pr_incremental_test_ids") or ())
                        if isinstance(x, str) and x
                    ]
                    log_dir = (
                        self.state_dir
                        / "logs"
                        / "runs"
                        / hashlib.sha256(key.encode()).hexdigest()
                    )
                    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    subrun = {
                        **run,
                        "_ci_test_id": item["test_id"],
                        "_ci_cancel_event": cancel,
                        "_ci_lease": lease,
                        "_ci_zone": self.zone,
                        "_ci_worker_address": self.address,
                        "_ci_pipeline_folder": log_dir,
                    }
                    if payload.get("use_instance_id") or run.get("_ci_dispatch_id"):
                        subrun["_ci_case_id"] = item["case_id"]
                    if is_self_repo:
                        # Mandatory baseline Smoke MUST execute from protected main checkout, never PR code.
                        defer_revoke = bool(incremental_ids)
                        execution = None
                        try:
                            with tempfile.TemporaryDirectory(
                                prefix="lite-baseline-"
                            ) as btmp:
                                baseline_archive = build_protected_baseline_archive(
                                    self.protected_baseline_root,
                                    Path(btmp),
                                    sha=self.baseline_sha or None,
                                )
                                subrun["_ci_stage_archive"] = str(baseline_archive)
                                subrun["_ci_defer_lease_revoke"] = defer_revoke
                                execution = execute(
                                    leased_case, subrun, root=self.main_root
                                )
                                launched = True
                                valid = case_passed(execution.log, item["test_id"])
                                if (
                                    incremental_ids
                                    and execution.exit_code == 0
                                    and valid
                                ):
                                    for inc_idx, inc_test_id in enumerate(
                                        incremental_ids
                                    ):
                                        inc_subrun = {
                                            **run,
                                            "_ci_test_id": inc_test_id,
                                            "_ci_case_id": f"{item['case_id']}-inc-{inc_idx}",
                                            "_ci_cancel_event": cancel,
                                            "_ci_lease": lease,
                                            "_ci_zone": self.zone,
                                            "_ci_worker_address": self.address,
                                            "_ci_pipeline_folder": log_dir,
                                            "_ci_defer_lease_revoke": True,
                                        }
                                        inc_exec = execute(
                                            leased_case, inc_subrun, root=self.main_root
                                        )
                                        if inc_exec.exit_code != 0 or not case_passed(
                                            inc_exec.log, inc_test_id
                                        ):
                                            execution = inc_exec
                                            valid = False
                                            break
                        finally:
                            if defer_revoke and (
                                launched or lease.state in {"LAUNCHING", "ACTIVE"}
                            ):
                                revoked = revoke_remote_lease(
                                    {
                                        "lease_id": lease.lease_id,
                                        "remote_nodes": lease.remote_nodes,
                                        "remote_lease_dir": remote_lease_dir(),
                                    },
                                    check_npu=True,
                                )
                                lease.set_remote_revoked_hosts(revoked)
                                expected = {
                                    n["ssh"] for n in lease.remote_nodes if n.get("ssh")
                                }
                                if execution is not None:
                                    execution.release_verified = (
                                        execution.release_verified
                                        and expected.issubset(revoked)
                                    )
                    else:
                        execution = execute(leased_case, subrun, root=self.main_root)
                        launched = True
                        valid = case_passed(execution.log, item["test_id"])
                    status = (
                        "CANCELLED"
                        if cancel.is_set() or execution.exit_code == 130
                        else "TIMED_OUT"
                        if execution.exit_code == 124
                        else "INFRA_ERROR"
                        if not execution.release_verified
                        else "PASS"
                        if execution.exit_code == 0 and valid
                        else "FAIL"
                    )
                    res = self._publish_result(
                        key,
                        run,
                        item,
                        digest,
                        status,
                        130 if status == "CANCELLED" else execution.exit_code,
                        execution.tail,
                        execution.release_verified,
                        execution.locator,
                        allocation=clean_alloc,
                    )
                    terminal_allocation = clean_alloc
                    return res
                except ExternalNpuOccupancy:
                    if launched:
                        res = self._publish_result(
                            key,
                            run,
                            item,
                            digest,
                            "INFRA_ERROR",
                            125,
                            ["Occupancy after launch"],
                            False,
                            None,
                            allocation=clean_alloc,
                        )
                        terminal_allocation = clean_alloc
                        return res
                    last_busy = "EXTERNAL_OCCUPANCY"
                    continue
                except RunChanged:
                    if launched:
                        terminal_allocation = clean_alloc
                        return {"run_changed": True, "allocation": clean_alloc}
                    return {"run_changed": True}
                finally:
                    if terminal_allocation is not None or launched:
                        self._mark_terminal_allocation(
                            key,
                            terminal_allocation
                            if terminal_allocation is not None
                            else clean_alloc,
                        )
                    else:
                        self._set_active_allocation(key, None)
                    lease.close()
            return {
                "retry_busy": True,
                "reason": last_busy,
                "retry_after": 60 if last_busy == "EXTERNAL_OCCUPANCY" else 15,
                "allocation": [],
            }
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(key, None)


class BrokerClient:
    """Unprivileged client used inside sandboxed Test Worker instances."""

    def __init__(self, socket_path: Path | str, worker_id: str | None = None):
        self.socket_path = Path(socket_path)
        self.worker_id = worker_id
        self.repo = None

    def for_run(self, run: dict):
        return self

    def for_repo(self, repo: str):
        return self

    def _call(
        self, op: str, payload: dict | None = None, *, timeout: float = 30.0
    ) -> dict:
        body = {"op": op, "payload": payload or {}}
        if self.worker_id:
            body["payload"].setdefault("worker_id", self.worker_id)
        encoded = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            raise LeaseConfinementViolation("REQUEST_TOO_LARGE")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(self.socket_path))
            conn.sendall(encoded)
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
        resp = json.loads(line)
        if not resp.get("ok"):
            err = str(resp.get("error") or "BROKER_ERROR")
            if resp.get("error_type") == "Deferred":
                raise Deferred(err)
            if "LEASE_CONFINEMENT_VIOLATION" in err:
                raise LeaseConfinementViolation(err)
            raise RuntimeError(err)
        return resp["result"]

    def hello(self) -> dict:
        res = self._call("HELLO")
        self.worker_id = res["worker_id"]
        return res

    def running_workflows(self) -> list[dict]:
        return self._call("POLL_RUNS").get("runs", [])

    def fetch_plan(self, run: dict) -> tuple[dict, dict] | None:
        res = self._call("FETCH_PLAN", {"run": run}, timeout=120.0)
        if not res.get("found"):
            return None
        return res["run"], res["plan"]

    def check_run_status(self, run: dict) -> dict:
        return self._call("WORKFLOW_STATUS", {"run": run})

    def post_progress(
        self,
        run: dict,
        item: dict,
        digest: str,
        phase: str,
        progress_id=None,
        *,
        retry: bool = False,
    ):
        res = self._call(
            "POST_PROGRESS",
            {
                "action": "progress",
                "run": run,
                "item": item,
                "digest": digest,
                "phase": phase,
                "progress_id": progress_id,
                "retry": retry,
            },
        )
        return res.get("progress_id")

    def post_terminal_report(
        self,
        run: dict,
        item: dict,
        digest: str,
        result: dict,
        *,
        verify_only: bool = False,
        retry: bool = False,
    ) -> dict:
        return self._call(
            "POST_PROGRESS",
            {
                "action": "terminal_non_pass",
                "run": run,
                "item": item,
                "digest": digest,
                "result": result,
                "verify_only": verify_only,
                "retry": retry,
            },
        )

    def request_run(
        self,
        *,
        key: str,
        run: dict,
        item: dict,
        digest: str,
        defaults: dict,
        use_instance_id: bool = False,
        cancel_event: threading.Event | None = None,
        on_phase=None,
    ) -> dict:
        if on_phase is not None:
            on_phase("LAUNCHING")
            on_phase("RUNNING")
        stop_watch = threading.Event()

        def watch_cancel():
            sent = False
            while not stop_watch.wait(0.5):
                if not sent and cancel_event is not None and cancel_event.is_set():
                    sent = True
                    try:
                        self._call("CANCEL_RUN", {"key": key}, timeout=5.0)
                    except Exception:
                        pass

        watcher = threading.Thread(target=watch_cancel, daemon=True)
        watcher.start()
        try:
            timeout = float(item.get("timeout_seconds") or 10800) + 300.0
            return self._call(
                "REQUEST_RUN",
                {
                    "key": key,
                    "run": run,
                    "item": item,
                    "digest": digest,
                    "defaults": defaults,
                    "use_instance_id": use_instance_id,
                },
                timeout=timeout,
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=1)

    def report_control_events(self, *, events=(), snapshot=None) -> dict:
        return self._call(
            "REPORT_CONTROL_EVENTS",
            {"events": list(events), "snapshot": snapshot},
        )


class BrokerControlAdapter:
    """Control adapter inside sandboxed Test Workers that proxies via TestBroker."""

    def __init__(self, broker_client: BrokerClient, wake: threading.Event, view_fn):
        self.broker = broker_client
        self.wake = wake
        self.view_fn = view_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_digest: str | None = None

    def commands(self) -> list[dict]:
        return []

    def complete(self, command, status, **fields) -> None:
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.flush_snapshot()
            self._stop.wait(5.0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="broker-control", daemon=True
        )
        self._thread.start()

    def flush_snapshot(self, *, force: bool = False) -> None:
        with self._lock:
            try:
                view = self.view_fn()
                raw_queues = view.get("queues") or {}
                if isinstance(raw_queues, dict):
                    queues_wire = [
                        {"pool": k[0], "kind": k[1], "rows": v}
                        if isinstance(k, tuple)
                        else {
                            "pool": k.split("|", 1)[0],
                            "kind": k.split("|", 1)[1],
                            "rows": v,
                        }
                        for k, v in sorted(
                            raw_queues.items(), key=lambda kv: str(kv[0])
                        )
                    ]
                else:
                    queues_wire = list(raw_queues)
                payload = {
                    "revision": int(view.get("revision", 0)),
                    "records": view.get("records", {}),
                    "queues": queues_wire,
                }
                digest = json.dumps(
                    payload, sort_keys=True, default=str, separators=(",", ":")
                )
                if not force and digest == self._last_digest:
                    return
                self.broker.report_control_events(events=[], snapshot=payload)
                self._last_digest = digest
            except Exception as exc:
                logging.warning(
                    "broker control report skipped (%s)", type(exc).__name__
                )

    def close(self) -> None:
        self._stop.set()
        self.wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.flush_snapshot(force=True)
