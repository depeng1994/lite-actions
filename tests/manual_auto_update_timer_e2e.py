"""Isolated timer/supervisor E2E for container hosts lacking systemd.

This script never calls real systemctl, changes production Worker state, or
dispatches GitHub Actions. It exercises the real update transaction with two
real Git commits and supervised CPU-only probe child processes.

Run: python3 -m tests.manual_auto_update_timer_e2e
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from auto_update.check_source import checkout_sha
from auto_update.update import Settings, _run
from auto_update.version_switch import SwitchError, current_release

PROJECT = Path(__file__).resolve().parents[1]

CHILD = r"""
import json
import signal
import sys
import time
from pathlib import Path

from ci_core.worker_probe import probe
from ci_core.worker_ready import publish_ready

root, ready, report = (Path(x) for x in sys.argv[1:4])
def stop(_signum, _frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
result = probe()
if result.returncode != 0 or "WORKER_HOSTNAME=" not in result.stdout:
    sys.exit(3)
sha = publish_ready(root, ready)["release_sha"]
with report.open("a", encoding="utf-8") as f:
    f.write(json.dumps({
        "probe_status": "CPU_PROBE_PASS",
        "sha": sha,
        "pid": __import__("os").getpid(),
        "lines": len(result.stdout.splitlines()),
    }) + "\n")
    f.flush()
while True:
    time.sleep(0.1)
"""


def git(*args: str, cwd: Path | None = None) -> str:
    p = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=True, timeout=30
    )
    return p.stdout.strip()


def create_origin(tmp: Path) -> tuple[Path, str, str]:
    repo = tmp / "origin"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("checkout", "-qb", "main", cwd=repo)
    git("config", "user.name", "Auto Update Test", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    # Real production module files, but all fixtures are in an isolated temp tree.
    files = [
        "unified_router.py",
        "auto_update/update.py",
        "auto_update/version_switch.py",
        "ci_core/config.py",
        "ci_core/router.py",
        "ci_core/worker_ready.py",
    ]
    for name in files:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / name, target)
    (repo / "version.txt").write_text("A\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "fixture-A", cwd=repo)
    old = git("rev-parse", "HEAD", cwd=repo)
    (repo / "version.txt").write_text("B\n")
    git("commit", "-qam", "fixture-B", cwd=repo)
    target = git("rev-parse", "HEAD", cwd=repo)
    return repo, old, target


class CPUWorkerSupervisor:
    """A fake systemctl command with REAL child processes and SIGTERM."""

    def __init__(self, root: Path, ready: Path, report: Path, new_sha: str):
        self.root = root
        self.ready = ready
        self.report = report
        self.new_sha = new_sha
        self.proc: subprocess.Popen | None = None
        self.fail_candidate_start = False
        self.events: list[tuple[str, float]] = []

    def command(self, action: str, unit: str, *, timeout: int = 60) -> str:
        assert unit == "fixture-worker.service", unit
        self.events.append((action, time.monotonic()))
        if action == "is-active":
            return "active" if self.alive() else "inactive"
        if action == "show":
            return str(self.proc.pid if self.alive() else 0)
        if action == "stop":
            self.stop()
            return ""
        if action == "start":
            if self.fail_candidate_start and checkout_sha(
                (self.root / "current").resolve()
            ) == self.new_sha:
                raise SwitchError("simulated_candidate_start_failure")
            self.start()
            return ""
        raise ValueError(action)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.alive():
            raise RuntimeError("duplicate_worker")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT)
        self.proc = subprocess.Popen(
            [
                sys.executable, "-u", "-c", CHILD,
                str(self.root / "current"), str(self.ready), str(self.report),
            ],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self) -> None:
        if not self.alive():
            return
        assert self.proc
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise SwitchError("simulated_shutdown_timeout")


def scenario_ok(tmp: Path) -> dict:
    repo, old_sha, new_sha = create_origin(tmp)
    root = tmp / "runtime"
    releases = root / "releases"
    releases.mkdir(parents=True)
    old_release = releases / old_sha
    git("clone", "-q", "--no-checkout", str(repo), str(old_release))
    git("-C", str(old_release), "checkout", "-q", "--detach", old_sha)
    (root / "current").symlink_to(old_release)
    ready, report = root / "worker-ready.json", root / "cpu-probe.jsonl"
    cfg = Settings(
        root=root, source=str(repo), ref="refs/heads/main",
        unit="fixture-worker.service", state_dir=root / "management",
        ready_file=ready, log_file=root / "management" / "updates.jsonl",
        lock_file=root / "management.lock", ready_timeout=10,
    )
    supervisor = CPUWorkerSupervisor(root, ready, report, new_sha)
    try:
        supervisor.start()
        # Allow CPU probe and Ready publication by the original Worker.
        limit = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < limit:
            time.sleep(0.05)
        assert ready.exists(), "fixture Worker failed to start"
        pid_a = supervisor.proc.pid

        def local_source(url, ref):
            assert url == str(repo) and ref == "refs/heads/main"
            return git("ls-remote", url, ref).split()[0]

        with (
            patch("auto_update.check_source.validate_source"),
            patch("auto_update.update.check_source", side_effect=local_source),
            patch("auto_update.version_switch.systemctl",
                  side_effect=supervisor.command),
        ):
            # Compressed timer triggers (0.2 s vs actual configured 3 min).
            # Never use this test as a timing proof of the real systemd timer.
            time.sleep(0.2)
            assert _run(cfg) == 0, "A-to-B update failed"
            assert current_release(root)[1] == new_sha
            assert supervisor.alive()
            pid_b = supervisor.proc.pid
            assert pid_b != pid_a, "Worker process did not restart"
            time.sleep(0.2)
            assert _run(cfg) == 0, "repeat NO_CHANGE failed"
            assert supervisor.proc.pid == pid_b, "NO_CHANGE restarted Worker"
        entries = [json.loads(s) for s in cfg.log_file.read_text().splitlines()]
        updates = [e for e in entries if e["event"] == "UPDATED"]
        assert len(updates) == 1
        assert any(e["event"] == "NO_CHANGE" for e in entries)
        assert (updates[0]["switch_to_start_ms"] <= 5000)
        assert (updates[0]["switch_to_pid_ms"] <= 30000)
        assert (updates[0]["start_to_ready_ms"] <= 120000)
        probes = [json.loads(s) for s in report.read_text().splitlines()]
        assert len(probes) >= 2
        assert probes[0]["sha"] == old_sha and probes[-1]["sha"] == new_sha
        assert all(p["probe_status"] == "CPU_PROBE_PASS" for p in probes)
        return {
            "status": "SIMULATED_TIMER_PASS",
            "old_sha": old_sha, "new_sha": new_sha,
            "old_pid": pid_a, "new_pid": pid_b,
            "probe_count": len(probes),
            "timing_ms": {k: v for k, v in updates[0].items()
                          if k.endswith("_ms")},
            "systemd": "NOT_TESTED",
            "github_actions_dispatch": "NOT_TESTED",
            "npu": "NOT_TESTED",
        }
    finally:
        supervisor.stop()


def scenario_failure(tmp: Path) -> dict:
    repo, old_sha, new_sha = create_origin(tmp)
    root = tmp / "runtime"
    releases = root / "releases"
    releases.mkdir(parents=True)
    old_release = releases / old_sha
    git("clone", "-q", "--no-checkout", str(repo), str(old_release))
    git("-C", str(old_release), "checkout", "-q", "--detach", old_sha)
    (root / "current").symlink_to(old_release)
    ready, report = root / "worker-ready.json", root / "cpu-probe.jsonl"
    cfg = Settings(
        root=root, source=str(repo), ref="refs/heads/main",
        unit="fixture-worker.service", state_dir=root / "management",
        ready_file=ready, log_file=root / "management" / "updates.jsonl",
        lock_file=root / "management.lock", ready_timeout=10,
    )
    supervisor = CPUWorkerSupervisor(root, ready, report, new_sha)
    supervisor.fail_candidate_start = True
    try:
        supervisor.start()
        def local_source(url, ref):
            return git("ls-remote", url, ref).split()[0]

        with (
            patch("auto_update.check_source.validate_source"),
            patch("auto_update.update.check_source", side_effect=local_source),
            patch("auto_update.version_switch.systemctl",
                  side_effect=supervisor.command),
        ):
            assert _run(cfg) == 3, "candidate failure did not cause rollback"
            assert current_release(root)[1] == old_sha
            assert supervisor.alive(), "old Worker not recovered"
            assert _run(cfg) == 0, "blocked failed version should be skipped"
            events = [json.loads(s) for s in cfg.log_file.read_text().splitlines()]
            assert any(x["event"] == "ROLLED_BACK" for x in events)
            assert any(x["event"] == "SKIPPED_FAILED_SHA" for x in events)
        return {
            "status": "SIMULATED_ROLLBACK_PASS",
            "restored_sha": old_sha,
            "bad_sha": new_sha,
            "systemd": "NOT_TESTED",
        }
    finally:
        supervisor.stop()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lite-actions-timer-e2e-") as tmp:
        result = scenario_ok(Path(tmp))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    with tempfile.TemporaryDirectory(prefix="lite-actions-timer-rollback-") as tmp:
        result = scenario_failure(Path(tmp))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
