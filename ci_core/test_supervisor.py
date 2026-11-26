"""Host-side supervisor managing dynamic Test Worker lifecycles on Main Workers."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ci_core.contract import (
    REPO,
    SELF_REPO,
    is_valid_worker_id,
    validate_lifecycle_payload,
)
from ci_core.resources import (
    PhysicalResourceCoordinator,
    UnverifiedRemoteRevoke,
    shared_resource_lock_root,
)
from ci_core.ssh_runner import model_archive, revoke_remote_lease
from ci_core.test_broker import TestBrokerServer
from ci_core.worker_ready import process_identity, verify_ready

MAX_TTL_SECONDS = 14400
UNIT_PREFIX = "lite-actions-test-"


def _prepare_merged_pr_checkout(
    repo_url: str,
    pr_number: int,
    code_dir: Path,
    *,
    base_branch: str = "main",
    git_env: dict[str, str] | None = None,
) -> None:
    """Merge fresh origin/<base_branch> + refs/pull/<pr>/head locally into code_dir."""
    env = {
        **(git_env if git_env is not None else os.environ),
        "GIT_TERMINAL_PROMPT": "0",
    }
    with tempfile.TemporaryDirectory(prefix="lite-tw-merge-") as tmp:
        repo_dir = Path(tmp) / "repo"
        archive_path = Path(tmp) / "merged.tar.gz"
        subprocess.run(["git", "init", "-q", str(repo_dir)], check=True, timeout=25)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "fetch",
                "-q",
                repo_url,
                f"refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
                f"refs/pull/{pr_number}/head:refs/remotes/origin/pr",
            ],
            check=True,
            timeout=180,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "checkout",
                "-q",
                "-B",
                "test-merge",
                f"refs/remotes/origin/{base_branch}",
            ],
            check=True,
            timeout=30,
            capture_output=True,
            env=env,
        )
        merge = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "-c",
                "user.name=lite-actions",
                "-c",
                "user.email=ci@lite-actions",
                "merge",
                "-q",
                "--no-edit",
                "refs/remotes/origin/pr",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        if merge.returncode != 0:
            detail = (merge.stderr or merge.stdout or "").strip().replace("\n", " ")
            raise RuntimeError(
                f"PR #{pr_number} failed to merge cleanly with {base_branch}: {detail[:180]}"
            )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "archive",
                "--format=tar.gz",
                "-o",
                str(archive_path),
                "HEAD",
            ],
            check=True,
            timeout=90,
            capture_output=True,
            env=env,
        )
        _safe_extract_tar_gz(archive_path, code_dir)


def _safe_extract_tar_gz(archive_path: Path, dest_dir: Path) -> None:
    """Safely extract repository archives without symlinks or path traversal."""
    dest = dest_dir.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk() or member.isdev():
                continue
            rel = Path(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe archive path: {member.name}")
            target = (dest / rel).resolve()
            if target != dest and dest not in target.parents:
                raise ValueError(f"archive escape attempt: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is not None:
                    with src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    mode = 0o755 if (member.mode & 0o111) else 0o644
                    os.chmod(target, mode)


def build_systemd_command(
    *,
    unit_name: str,
    zone: str,
    worker_id: str,
    state_dir: Path,
    broker_sock: Path,
    ready_file: Path,
    pools_file: Path,
    code_dir: Path,
    python_bin: str | None = None,
    state_dir_name: str | None = None,
) -> list[str]:
    """Build the systemd-run invocation confining a Test Worker to an ephemeral DynamicUser."""
    py = python_bin or sys.executable
    py_resolved = Path(py).resolve()
    py_dir = str(py_resolved.parent)
    bind_ro_paths = [str(code_dir)]
    if py_dir and not py_dir.startswith(("/usr", "/bin")):
        py_prefix = (
            py_resolved.parent.parent
            if py_resolved.parent.name in {"bin", "Scripts"}
            and py_resolved.parent.parent not in {Path("/"), Path("/usr"), Path("/opt")}
            else py_resolved.parent
        )
        extra_bind = str(py_prefix)
        if extra_bind not in bind_ro_paths:
            bind_ro_paths.append(extra_bind)
    cmd = [
        "systemd-run",
        "--collect",
        f"--unit={unit_name}",
        "-p",
        "DynamicUser=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=yes",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "PrivateTmp=yes",
        "-p",
        "TimeoutStopSec=15",
    ]
    if state_dir_name:
        cmd.extend(
            [
                "-p",
                f"StateDirectory={state_dir_name}",
                "-p",
                "StateDirectoryMode=0700",
            ]
        )
    else:
        cmd.extend(["-p", f"ReadWritePaths={state_dir}"])
    cmd.extend(["-p", f"ReadWritePaths={broker_sock.parent}"])
    for ro_path in bind_ro_paths:
        cmd.extend(["-p", f"BindReadOnlyPaths={ro_path}"])
    cmd.extend(
        [
            "--",
            str(code_dir / "scripts/run_test_worker.sh"),
            "--zone",
            zone,
            "--worker-id",
            worker_id,
            "--state-dir",
            str(state_dir),
            "--broker-sock",
            str(broker_sock),
            "--ready-file",
            str(ready_file),
            "--pools-file",
            str(pools_file),
            "--code-dir",
            str(code_dir),
            "--python",
            py,
        ]
    )
    return cmd


class TestSupervisor:
    """Write-ahead lifecycle controller for dynamic PR Test Workers."""

    def __init__(
        self,
        *,
        zone: str,
        main_root: Path,
        state_dir: Path,
        pools: dict,
        pools_revision: int,
        workers: dict,
        author: str,
        address: str,
        release_sha: str,
        client=None,
        coordinator: PhysicalResourceCoordinator | None = None,
        repos: tuple[str, ...] = (REPO,),
        gitcode_repos: tuple[str, ...] = (),
        control_provider: Callable[[], object] | None = None,
        runtime_root: Path | None = None,
        worktree_root: Path | None = None,
        launcher=None,
        stopper=None,
        alive_checker=None,
        code_preparer=None,
        revoke_fn=None,
        clock: Callable[[], float] = time.time,
        enforce_peercred: bool = True,
        quarantine_enabled: bool = False,
        ready_timeout: float = 45.0,
    ):
        self.zone = zone
        self.main_root = Path(main_root)
        self.state_dir = Path(state_dir)
        self.ledger_path = self.state_dir / "test-workers.json"
        self.lock_path = self.state_dir / "test-workers.lock"
        self.pools = pools
        self.pools_revision = pools_revision
        self.workers = workers
        self.author = author
        self.address = address
        self.release_sha = release_sha
        self.client = client
        self.coordinator = coordinator or PhysicalResourceCoordinator(
            shared_resource_lock_root()
        )
        self.repos = tuple(repos)
        self.gitcode_repos = tuple(gitcode_repos)
        self.control_provider = control_provider
        default_rt = Path(
            os.environ.get("LITE_ACTIONS_TEST_RUNTIME_ROOT", "")
            or "/run/lite-actions-broker"
        )
        default_wt = Path(
            os.environ.get("LITE_ACTIONS_TEST_WORKERS_DIR", "")
            or str(self.state_dir.parent / f"{self.zone}-test-workers")
        )
        self.runtime_root = Path(runtime_root) if runtime_root else default_rt
        self.worktree_root = Path(worktree_root) if worktree_root else default_wt
        self._launcher = launcher
        self._stopper = stopper
        self._alive_checker = alive_checker
        self._code_preparer = code_preparer
        self._revoke_fn = revoke_fn or revoke_remote_lease
        self.clock = clock
        self.enforce_peercred = enforce_peercred
        self.quarantine_enabled = quarantine_enabled
        self.ready_timeout = ready_timeout
        self._brokers: dict[str, TestBrokerServer] = {}
        self._op_mutex = threading.RLock()
        self._last_watchdog_remote_check = 0.0
        self._cached_runs: list[dict] = []
        self._cached_runs_at: float = 0.0
        self._cached_runs_lock = threading.Lock()

    def update_cached_runs(self, runs: list[dict]) -> None:
        with self._cached_runs_lock:
            self._cached_runs = copy.deepcopy(list(runs))
            self._cached_runs_at = time.monotonic()

    def get_cached_runs(self, max_age: float = 90.0) -> list[dict] | None:
        with self._cached_runs_lock:
            if (
                self._cached_runs_at <= 0.0
                or (time.monotonic() - self._cached_runs_at) > max_age
            ):
                return None
            return copy.deepcopy(self._cached_runs)

    @contextmanager
    def _locked(self):
        with self._op_mutex:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as fd:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)

    def _read_ledger(self) -> dict:
        if not self.ledger_path.exists():
            return {"workers": {}}
        try:
            doc = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("workers"), dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"workers": {}}

    def _write_ledger(self, doc: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile(dir=self.state_dir, delete=False) as tmp:
            temp = Path(tmp.name)
            try:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp.replace(self.ledger_path)
                dfd = os.open(self.state_dir, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                temp.unlink(missing_ok=True)

    @staticmethod
    def _unit_name(worker_id: str) -> str:
        return f"{UNIT_PREFIX}{worker_id}"

    @staticmethod
    def _short_id(worker_id: str) -> str:
        return hashlib.sha256(worker_id.encode()).hexdigest()[:12]

    def _client_for(self, client, run: dict):
        c = client or self.client
        repo = run.get("_ci_repo", SELF_REPO)
        if hasattr(c, "for_run"):
            return c.for_run(run)
        if hasattr(c, "for_repo"):
            return c.for_repo(repo)
        return c

    def _broker_client(self, client):
        """Prefer multi-platform or multi-repo clients over a single-repo deploy client."""
        for candidate in (self.client, client):
            if candidate is not None and hasattr(candidate, "for_run"):
                return candidate
        for candidate in (self.client, client):
            if candidate is not None and hasattr(candidate, "for_repo"):
                return candidate
        return client or self.client

    def _control(self):
        return self.control_provider() if self.control_provider else None

    def _forward_control(self, test_worker_id: str, *, events=(), snapshot=None):
        ctrl = self._control()
        if ctrl is not None and hasattr(ctrl, "forward_test_telemetry"):
            ctrl.forward_test_telemetry(
                test_worker_id, events=events, snapshot=snapshot
            )

    def _notify_control_register(self, record: dict) -> None:
        ctrl = self._control()
        if ctrl is not None and hasattr(ctrl, "register_test_worker"):
            try:
                ctrl.register_test_worker(record)
            except Exception as exc:
                logging.warning(
                    "test worker register deferred (%s)", type(exc).__name__
                )

    def _notify_control_deregister(self, worker_id: str, *, reason: str) -> None:
        ctrl = self._control()
        if ctrl is not None and hasattr(ctrl, "deregister_test_worker"):
            try:
                ctrl.deregister_test_worker(worker_id, reason=reason)
            except Exception as exc:
                logging.warning(
                    "test worker deregister deferred (%s)", type(exc).__name__
                )

    def verify_deploy_authority(self, client, run: dict, lifecycle: dict) -> None:
        """Directly verify PR state, head SHA, maintainer/review authority, and run attempt via platform REST API."""
        c = self._client_for(client, run)
        pr_number = lifecycle["pr_number"]
        run_id = lifecycle["github_run_id"]
        attempt = lifecycle["github_attempt"]
        pr = c.request("GET", f"/pulls/{pr_number}", egress_category="metadata_get")
        if pr.get("state") != "open":
            raise ValueError(f"PR #{pr_number} is not open")
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha != lifecycle["pr_head_sha"]:
            raise ValueError(
                f"PR #{pr_number} head moved: expected {lifecycle['pr_head_sha']}, got {head_sha}"
            )
        pr_author = ((pr.get("user") or {}).get("login") or "").strip().lower()
        if not pr_author:
            raise ValueError(f"PR #{pr_number} missing author identity")
        trusted_owners = {
            item.strip().lower()
            for item in os.environ.get(
                "LITE_ACTIONS_PR_MAINTAINERS", "depeng1994"
            ).split(",")
            if item.strip()
        } or {"depeng1994"}
        if pr_author not in trusted_owners:
            reviews = c.request(
                "GET",
                f"/pulls/{pr_number}/reviews?per_page=100",
                egress_category="metadata_get",
            )
            if not isinstance(reviews, list):
                raise ValueError(f"PR #{pr_number} reviews payload invalid")
            latest_decisive = {
                u: str(rev.get("state") or "").upper()
                for rev in reviews
                if isinstance(rev, dict)
                and (u := ((rev.get("user") or {}).get("login") or "").strip().lower())
                and u != pr_author
                and u in trusted_owners
                and str(rev.get("state") or "").upper()
                in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
            }
            if "APPROVED" not in latest_decisive.values():
                raise ValueError(
                    f"PR #{pr_number} by {pr_author} requires maintainer APPROVED review"
                )
        run_info = c.workflow_status(run_id)
        if run_info.get("status") not in {"queued", "in_progress"}:
            raise ValueError(f"GitHub run {run_id} is not active")
        if run_info.get("run_attempt", attempt) != attempt:
            raise ValueError(
                f"GitHub run {run_id} attempt mismatch: {run_info.get('run_attempt')} != {attempt}"
            )
        return pr

    def detect_partial_rerun(
        self, client, run: dict, lifecycle: dict, *, is_cleanup: bool = False
    ) -> str | None:
        """Detect partial attempt re-runs lacking a fresh deploy and guide user to Re-run all jobs."""
        attempt = lifecycle["github_attempt"]
        if attempt <= 1:
            return None
        ledger = self._read_ledger()["workers"]
        target = lifecycle["target_worker"]
        if is_cleanup and target not in ledger:
            return (
                f"Partial re-run detected for {target} (attempt {attempt} without "
                "prior deploy in this attempt); please use 'Re-run all jobs'."
            )
        c = self._client_for(client, run)
        try:
            data = c.request(
                "GET",
                f"/actions/runs/{lifecycle['github_run_id']}/attempts/{attempt}/jobs",
                egress_category="metadata_get",
            )
        except Exception:
            if is_cleanup and target not in ledger:
                return "Partial re-run detected; please use 'Re-run all jobs'."
            return None
        jobs = data.get("jobs") or []
        if not jobs:
            return None
        fresh_prepare_or_deploy = False
        has_stale_deploy = False
        for job in jobs:
            name = str(job.get("name") or "").lower()
            job_attempt = job.get("run_attempt", attempt)
            if "deploy" in name:
                if job_attempt < attempt:
                    has_stale_deploy = True
                elif job_attempt == attempt and (
                    self.zone in name
                    or job.get("status") in {"queued", "in_progress", "completed"}
                ):
                    fresh_prepare_or_deploy = True
            elif "prepare" in name and job_attempt == attempt:
                fresh_prepare_or_deploy = True
        if has_stale_deploy or not fresh_prepare_or_deploy:
            return (
                f"Partial re-run detected on attempt {attempt}: deploy/prepare jobs "
                "did not re-run freshly; please use 'Re-run all jobs'."
            )
        return None

    def _default_prepare_code(
        self, client, run: dict, lifecycle: dict, code_dir: Path
    ) -> None:
        if code_dir.exists():
            shutil.rmtree(code_dir, ignore_errors=True)
        repo = run.get("_ci_repo", SELF_REPO)
        provider = run.get("_ci_provider", "github")
        pr_number = lifecycle.get("pr_number")
        if provider == "github" and isinstance(pr_number, int) and pr_number > 0:
            base_branch = str(run.get("_ci_pr_base_ref") or "main").strip() or "main"
            _prepare_merged_pr_checkout(
                f"https://github.com/{repo}.git",
                pr_number,
                code_dir,
                base_branch=base_branch,
            )
        else:
            sha = run.get("_ci_execution_sha") or lifecycle["pr_head_sha"]
            fallback = (run.get("head_repository") or {}).get("full_name") or run.get(
                "_ci_source_repo"
            )
            with tempfile.TemporaryDirectory(prefix="lite-tw-src-") as tmp:
                archive = model_archive(
                    sha,
                    Path(tmp),
                    source_repo=repo,
                    provider=provider,
                    fallback_repo=fallback,
                    source_pr=(
                        pr_number
                        if run.get("_ci_source", {}).get("mode") == "merge_result"
                        else None
                    ),
                )
                _safe_extract_tar_gz(archive, code_dir)
        # Ensure clean permissions readable by DynamicUser=yes under BindReadOnlyPaths.
        for path in code_dir.rglob("*"):
            try:
                if path.is_dir():
                    os.chmod(path, 0o755)
                elif os.access(path, os.X_OK):
                    os.chmod(path, 0o755)
                else:
                    os.chmod(path, 0o644)
            except OSError:
                pass
        runner_script = code_dir / "scripts/run_test_worker.sh"
        if runner_script.exists():
            os.chmod(runner_script, 0o755)
        os.chmod(code_dir, 0o755)

    def _default_launch_systemd(
        self,
        record: dict,
        *,
        code_dir: Path,
        state_dir: Path,
        broker_sock: Path,
        ready_file: Path,
        pools_file: Path,
    ) -> dict:
        unit_name = record["unit_name"]
        subprocess.run(
            ["systemctl", "reset-failed", f"{unit_name}.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        cli_state_dir = (
            Path(record["unit_state_dir"])
            if record.get("unit_state_dir")
            else state_dir
        )
        cmd = build_systemd_command(
            unit_name=unit_name,
            zone=self.zone,
            worker_id=record["worker_id"],
            state_dir=cli_state_dir,
            broker_sock=broker_sock,
            ready_file=cli_state_dir / "worker-ready.json",
            pools_file=cli_state_dir / "pools-active.json",
            code_dir=code_dir,
            state_dir_name=record.get("state_dir_name"),
        )
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"systemd-run failed ({proc.returncode}): {proc.stderr.strip()[:160]}"
            )
        pid = 0
        deadline = time.monotonic() + min(15.0, self.ready_timeout)
        while time.monotonic() < deadline:
            show = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "-p",
                    "MainPID",
                    "--value",
                    f"{unit_name}.service",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            text = show.stdout.strip()
            if text.isdecimal() and int(text) > 1:
                pid = int(text)
                break
            time.sleep(0.1)
        if pid <= 1:
            raise RuntimeError("transient test worker MainPID unavailable")
        boot_id, ticks = process_identity(pid)
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            uid = None
        return {"pid": pid, "boot_id": boot_id, "start_ticks": ticks, "uid": uid}

    def _is_alive(self, record: dict) -> bool:
        if self._alive_checker is not None:
            return bool(self._alive_checker(record))
        pid = record.get("pid")
        ticks = record.get("start_ticks")
        boot = record.get("boot_id")
        if isinstance(pid, int) and pid > 1 and isinstance(ticks, int):
            try:
                cur_boot, cur_ticks = process_identity(pid)
                if cur_boot == boot and cur_ticks == ticks:
                    return True
            except OSError:
                return False
        return False

    def _stop_process_or_unit(self, record: dict) -> None:
        if self._stopper is not None:
            self._stopper(record["worker_id"], record)
            return
        unit_name = record.get("unit_name")
        has_systemctl = bool(
            unit_name and self._launcher is None and shutil.which("systemctl")
        )
        if has_systemctl:
            try:
                subprocess.run(
                    ["systemctl", "stop", f"{unit_name}.service"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            except (subprocess.TimeoutExpired, OSError):
                try:
                    subprocess.run(
                        [
                            "systemctl",
                            "kill",
                            "-s",
                            "SIGKILL",
                            "--kill-who=all",
                            f"{unit_name}.service",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                except (subprocess.TimeoutExpired, OSError):
                    pass
        pid = record.get("pid")
        ticks = record.get("start_ticks")
        boot = record.get("boot_id")
        if isinstance(pid, int) and pid > 1 and isinstance(ticks, int):
            # Never signal a PID without confirming boot_id + start_ticks identity.
            try:
                cur_boot, cur_ticks = process_identity(pid)
            except OSError:
                cur_boot, cur_ticks = None, None
            if cur_boot == boot and cur_ticks == ticks:
                sent = False
                try:
                    os.kill(pid, signal.SIGTERM)
                    sent = True
                except ProcessLookupError:
                    sent = False
                if sent:
                    deadline = time.monotonic() + 8.0
                    still_running = True
                    while time.monotonic() < deadline:
                        try:
                            cur_boot, cur_ticks = process_identity(pid)
                            if cur_boot != boot or cur_ticks != ticks:
                                still_running = False
                                break
                        except OSError:
                            still_running = False
                            break
                        time.sleep(0.1)
                    if still_running:
                        try:
                            cur_boot, cur_ticks = process_identity(pid)
                            if cur_boot == boot and cur_ticks == ticks:
                                os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
        if has_systemctl:
            try:
                subprocess.run(
                    ["systemctl", "reset-failed", f"{unit_name}.service"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass

    def handle_deploy(self, client, run: dict, item: dict) -> dict:
        lifecycle = item.get("lifecycle")
        if not isinstance(lifecycle, dict):
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": ["Missing lifecycle payload"],
                "release_verified": True,
            }
        try:
            validate_lifecycle_payload(
                "test_worker_deploy", lifecycle, worker=self.zone, run=run
            )
        except ValueError as exc:
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": [f"Invalid lifecycle payload: {exc}"],
                "release_verified": True,
            }

        target_worker = lifecycle["target_worker"]
        expected_worker = f"{self.zone}-test-{lifecycle['target_test_id']}"
        if target_worker != expected_worker or not is_valid_worker_id(target_worker):
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": ["TARGET_WORKER_IDENTITY_MISMATCH"],
                "release_verified": True,
            }

        partial_msg = self.detect_partial_rerun(
            client, run, lifecycle, is_cleanup=False
        )
        if partial_msg:
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": [partial_msg],
                "release_verified": True,
            }

        try:
            pr_info = self.verify_deploy_authority(client, run, lifecycle)
        except Exception as exc:
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": [f"Deploy authority check failed: {exc}"],
                "release_verified": True,
            }
        base_ref = (
            ((pr_info.get("base") or {}).get("ref") or "").strip()
            if isinstance(pr_info, dict)
            else ""
        )
        if base_ref:
            run = {**run, "_ci_pr_base_ref": base_ref}

        ttl = max(60, min(MAX_TTL_SECONDS, int(lifecycle["ttl_seconds"])))
        now = self.clock()
        expires_at = int(now + ttl)
        repo = run.get("_ci_repo", SELF_REPO)

        with self._locked():
            doc = self._read_ledger()
            workers_map = doc["workers"]
            # Reap superseded live Test Workers for the same PR.
            for existing_id, rec in list(workers_map.items()):
                if existing_id == target_worker:
                    continue
                if (
                    rec.get("zone") == self.zone
                    and rec.get("repo") == repo
                    and rec.get("pr_number") == lifecycle["pr_number"]
                    and rec.get("status") in {"DEPLOYING", "ACTIVE"}
                    and (
                        rec.get("pr_head_sha") != lifecycle["pr_head_sha"]
                        or rec.get("attempt", 0) < lifecycle["github_attempt"]
                        or rec.get("run_id") != lifecycle["github_run_id"]
                    )
                ):
                    self._cleanup_worker_unlocked(doc, existing_id, reason="superseded")

            existing = workers_map.get(target_worker)
            if (
                existing
                and existing.get("status") == "ACTIVE"
                and existing.get("pr_head_sha") == lifecycle["pr_head_sha"]
                and self._is_alive(existing)
            ):
                return {
                    "status": "PASS",
                    "exit_code": 0,
                    "tail": [f"Test worker already active: {target_worker}"],
                    "release_verified": True,
                }

            short = self._short_id(target_worker)
            instance_root = self.worktree_root / short
            code_dir = instance_root / "code"
            if self._launcher is None:
                state_dir_name = f"lite-actions-tw-{short}"
                unit_state_dir = Path("/var/lib") / state_dir_name
                state_dir = Path("/var/lib/private") / state_dir_name
                user_name = None
            else:
                state_dir_name = None
                state_dir = instance_root / "state"
                unit_state_dir = state_dir
                user_name = f"latw-{short}"
            pools_file = state_dir / "pools-active.json"
            ready_file = state_dir / "worker-ready.json"
            sock_dir = self.runtime_root / f"b-{short}"
            broker_sock = sock_dir / "b.sock"
            unit_name = self._unit_name(target_worker)

            # Write-ahead DEPLOYING record before spawning broker or worker.
            record = {
                "worker_id": target_worker,
                "zone": self.zone,
                "repo": repo,
                "pr_number": lifecycle["pr_number"],
                "pr_head_sha": lifecycle["pr_head_sha"],
                "run_id": lifecycle["github_run_id"],
                "attempt": lifecycle["github_attempt"],
                "generation": lifecycle.get("generation", 1),
                "status": "DEPLOYING",
                "unit_name": unit_name,
                "user_name": user_name,
                "state_dir_name": state_dir_name,
                "unit_state_dir": str(unit_state_dir),
                "instance_root": str(instance_root),
                "state_dir": str(state_dir),
                "code_dir": str(code_dir),
                "pools_file": str(pools_file),
                "ready_file": str(ready_file),
                "sock_dir": str(sock_dir),
                "broker_sock": str(broker_sock),
                "pid": None,
                "boot_id": None,
                "start_ticks": None,
                "ttl_seconds": ttl,
                "created_at": int(now),
                "expires_at": expires_at,
                "updated_at": int(now),
            }
            workers_map[target_worker] = record
            self._write_ledger(doc)
            self._notify_control_register(record)

            try:
                self.worktree_root.mkdir(parents=True, exist_ok=True)
                os.chmod(self.worktree_root, 0o711)
                instance_root.mkdir(parents=True, exist_ok=True)
                os.chmod(instance_root, 0o711)
                if self._launcher is not None:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(state_dir, 0o700)
                else:
                    if unit_state_dir.is_symlink():
                        unit_state_dir.unlink(missing_ok=True)
                    elif unit_state_dir.exists():
                        shutil.rmtree(unit_state_dir, ignore_errors=True)
                    shutil.rmtree(state_dir, ignore_errors=True)
                self.runtime_root.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.runtime_root, 0o711)
                except OSError:
                    pass
                sock_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(sock_dir, 0o711)
                except OSError:
                    pass

                if self._code_preparer is not None:
                    self._code_preparer(client, run, lifecycle, code_dir)
                else:
                    self._default_prepare_code(client, run, lifecycle, code_dir)

                code_sha = run.get("_ci_execution_sha") or lifecycle["pr_head_sha"]
                broker = TestBrokerServer(
                    socket_path=broker_sock,
                    worker_id=target_worker,
                    main_root=self.main_root,
                    state_dir=state_dir,
                    pools_path=pools_file,
                    pools=self.pools,
                    pools_revision=self.pools_revision,
                    workers=self.workers,
                    author=self.author,
                    address=self.address,
                    release_sha=code_sha,
                    baseline_sha=self.release_sha,
                    client=self._broker_client(client),
                    coordinator=self.coordinator,
                    repos=self.repos,
                    gitcode_repos=self.gitcode_repos,
                    scope={
                        "repo": repo,
                        "pr_number": lifecycle["pr_number"],
                        "pr_head_sha": lifecycle["pr_head_sha"],
                        "source_run_id": lifecycle["github_run_id"],
                        "allow_gitcode_test_worker": True,
                    },
                    control_sink=self._forward_control,
                    allowed_user=None,
                    allowed_unit=(unit_name if self._launcher is None else None),
                    allowed_uid=(os.getuid() if self._launcher is not None else None),
                    enforce_peercred=self.enforce_peercred,
                    protected_baseline_root=self.main_root,
                    quarantine_enabled=self.quarantine_enabled,
                    runs_provider=self.get_cached_runs,
                )
                broker.start()
                self._brokers[target_worker] = broker

                start_ts = self.clock()
                if self._launcher is not None:
                    proc_meta = self._launcher(
                        record,
                        code_dir=code_dir,
                        state_dir=state_dir,
                        broker_sock=broker_sock,
                        ready_file=ready_file,
                        pools_file=pools_file,
                    )
                else:
                    proc_meta = self._default_launch_systemd(
                        record,
                        code_dir=code_dir,
                        state_dir=state_dir,
                        broker_sock=broker_sock,
                        ready_file=ready_file,
                        pools_file=pools_file,
                    )
                if (
                    self._launcher is None
                    and proc_meta.get("uid") is not None
                    and int(proc_meta["uid"]) > 0
                ):
                    broker.pin_allowed_uid(int(proc_meta["uid"]))
                pid = int(proc_meta["pid"])
                boot_id = proc_meta.get("boot_id")
                start_ticks = proc_meta.get("start_ticks")
                record["pid"] = pid
                record["boot_id"] = boot_id
                record["start_ticks"] = start_ticks
                self._write_ledger(doc)

                deadline = time.monotonic() + self.ready_timeout
                ready_ok = False
                while time.monotonic() < deadline:
                    if proc_meta.get("skip_ready_check"):
                        ready_ok = True
                        break
                    if verify_ready(
                        ready_file,
                        expected_sha=code_sha,
                        expected_pid=pid,
                        not_before=start_ts - 1.0,
                    ):
                        ready_ok = True
                        break
                    if not self._is_alive(record):
                        break
                    time.sleep(0.1)
                if not ready_ok:
                    raise RuntimeError("test worker failed to publish ready marker")

                record["status"] = "ACTIVE"
                record["updated_at"] = int(self.clock())
                self._write_ledger(doc)
                self._notify_control_register(record)
                return {
                    "status": "PASS",
                    "exit_code": 0,
                    "tail": [f"Deployed {target_worker} pid={pid}"],
                    "release_verified": True,
                }
            except Exception as exc:
                logging.exception("test worker deploy failed for %s", target_worker)
                self._cleanup_worker_unlocked(
                    doc, target_worker, reason="deploy_failed"
                )
                detail = " ".join(str(exc).split())[:120]
                return {
                    "status": "INFRA_ERROR",
                    "exit_code": 125,
                    "tail": [
                        f"Deploy failed for {target_worker}: {type(exc).__name__}"
                        + (f": {detail}" if detail else "")
                    ],
                    "release_verified": False,
                }

    def handle_cleanup(self, client, run: dict, item: dict) -> dict:
        lifecycle = item.get("lifecycle")
        if not isinstance(lifecycle, dict):
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": ["Missing lifecycle payload"],
                "release_verified": True,
            }
        try:
            validate_lifecycle_payload(
                "test_worker_cleanup", lifecycle, worker=self.zone, run=run
            )
        except ValueError as exc:
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": [f"Invalid lifecycle payload: {exc}"],
                "release_verified": True,
            }

        target_worker = lifecycle["target_worker"]
        expected_worker = f"{self.zone}-test-{lifecycle['target_test_id']}"
        if target_worker != expected_worker or not is_valid_worker_id(target_worker):
            return {
                "status": "FAIL",
                "exit_code": 1,
                "tail": ["TARGET_WORKER_IDENTITY_MISMATCH"],
                "release_verified": True,
            }

        with self._locked():
            doc = self._read_ledger()
            partial_msg = self.detect_partial_rerun(
                client, run, lifecycle, is_cleanup=True
            )
            if partial_msg and target_worker not in doc["workers"]:
                return {
                    "status": "FAIL",
                    "exit_code": 1,
                    "tail": [partial_msg],
                    "release_verified": True,
                }
            return self._cleanup_worker_unlocked(doc, target_worker, reason="cleanup")

    def cleanup_worker(self, worker_id: str, *, reason: str = "cleanup") -> dict:
        with self._locked():
            doc = self._read_ledger()
            return self._cleanup_worker_unlocked(doc, worker_id, reason=reason)

    def _cleanup_worker_unlocked(
        self, doc: dict, worker_id: str, *, reason: str
    ) -> dict:
        workers_map = doc["workers"]
        record = workers_map.get(worker_id)
        leases = self.coordinator.active_leases_for_owner(worker_id)
        if (
            record
            and record.get("status") == "STOPPED"
            and not leases
            and worker_id not in self._brokers
        ):
            return {
                "status": "PASS",
                "exit_code": 0,
                "tail": [f"Cleanup idempotent (already stopped): {worker_id}"],
                "release_verified": True,
            }

        if worker_id in self._brokers:
            self._brokers[worker_id].cancel_all()
        if record:
            self._stop_process_or_unit(record)
        broker = self._brokers.pop(worker_id, None)
        if broker is not None:
            try:
                broker.stop()
            except Exception:
                logging.exception("broker stop error for %s", worker_id)

        cleanup_ok = True
        failures = []
        for lease in leases:
            lease_id = lease["lease_id"]
            allow_unlaunched = lease.get("state") == "RESERVED" and not lease.get(
                "remote_hosts"
            )
            try:
                revoked_hosts = (
                    self._revoke_fn(lease) if lease.get("remote_hosts") else set()
                )
                self.coordinator.release(
                    lease_id,
                    remote_revoked_hosts=revoked_hosts,
                    allow_unlaunched=allow_unlaunched,
                )
            except UnverifiedRemoteRevoke as exc:
                cleanup_ok = False
                failures.append(str(exc))
            except Exception as exc:
                cleanup_ok = False
                failures.append(f"{lease_id}: {type(exc).__name__}")

        if cleanup_ok:
            if record is None:
                workers_map[worker_id] = {
                    "worker_id": worker_id,
                    "zone": self.zone,
                    "status": "STOPPED",
                    "updated_at": int(self.clock()),
                }
            else:
                record["status"] = "STOPPED"
                record["stopped_reason"] = reason
                record["updated_at"] = int(self.clock())
                for dkey in (
                    "sock_dir",
                    "instance_root",
                    "state_dir",
                    "unit_state_dir",
                ):
                    path_str = record.get(dkey)
                    if not path_str:
                        continue
                    path_obj = Path(path_str)
                    if path_obj.is_symlink():
                        path_obj.unlink(missing_ok=True)
                    elif path_obj.exists():
                        shutil.rmtree(path_obj, ignore_errors=True)
            self._write_ledger(doc)
            self._notify_control_deregister(worker_id, reason=reason)
            return {
                "status": "PASS",
                "exit_code": 0,
                "tail": [f"Cleaned up {worker_id} ({reason})"],
                "release_verified": True,
            }

        if record is not None:
            record["status"] = "CLEANUP_FAILED"
            record["stopped_reason"] = reason
            record["updated_at"] = int(self.clock())
            self._write_ledger(doc)
        return {
            "status": "INFRA_ERROR",
            "exit_code": 125,
            "tail": [
                f"Cleanup unverified for {worker_id}: {'; '.join(failures)}"[:180]
            ],
            "release_verified": False,
        }

    def recover(self) -> list[dict]:
        """Reconcile any leftover test workers and active test leases on Main Worker startup."""
        results = []
        zone_prefix = f"{self.zone}-test-"
        with self._locked():
            doc = self._read_ledger()
            candidates_ids = {
                wid
                for wid, rec in doc["workers"].items()
                if wid.startswith(zone_prefix)
                and rec.get("status") in {"DEPLOYING", "ACTIVE", "CLEANUP_FAILED"}
            }
            for lease in self.coordinator.all_active_leases():
                owner = lease.get("owner_worker", "")
                if owner.startswith(zone_prefix):
                    candidates_ids.add(owner)
            for wid in sorted(candidates_ids):
                results.append(
                    self._cleanup_worker_unlocked(doc, wid, reason="startup_recovery")
                )
        return results

    def tick_watchdog(
        self, client=None, *, check_remote_every: float = 60.0
    ) -> list[dict]:
        """Reap expired, crashed, or PR/run-completed Test Workers."""
        results = []
        now = self.clock()
        mono = time.monotonic()
        do_remote = mono - self._last_watchdog_remote_check >= check_remote_every
        with self._locked():
            doc = self._read_ledger()
            active = [
                (wid, rec)
                for wid, rec in doc["workers"].items()
                if rec.get("status") in {"DEPLOYING", "ACTIVE"}
            ]
            if not active:
                return results
            for wid, rec in active:
                reason = None
                if now >= float(rec.get("expires_at", 0)):
                    reason = "ttl_expired"
                elif rec.get("status") == "ACTIVE" and not self._is_alive(rec):
                    reason = "worker_died"
                elif do_remote and (client or self.client):
                    try:
                        c = self._client_for(
                            client, {"_ci_repo": rec.get("repo", SELF_REPO)}
                        )
                        pr = c.request(
                            "GET",
                            f"/pulls/{rec['pr_number']}",
                            egress_category="metadata_get",
                        )
                        if pr.get("state") != "open" or (pr.get("head") or {}).get(
                            "sha"
                        ) != rec.get("pr_head_sha"):
                            reason = "pr_changed_or_closed"
                        else:
                            st = c.workflow_status(rec["run_id"])
                            if st.get("status") == "completed":
                                reason = "run_completed"
                    except Exception as exc:
                        logging.warning(
                            "watchdog remote check skipped for %s (%s)",
                            wid,
                            type(exc).__name__,
                        )
                if reason:
                    try:
                        results.append(
                            self._cleanup_worker_unlocked(doc, wid, reason=reason)
                        )
                    except Exception:
                        logging.exception("watchdog cleanup failed for %s", wid)
            if do_remote:
                self._last_watchdog_remote_check = mono
        return results
