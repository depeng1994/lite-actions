"""RFC #19 AU-10/11/13/14: integration contracts without production systemctl."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import unified_router
from auto_update.update import _run, exclusive
from auto_update.update_log import record
from ci_core.contract import PR_SMOKE_WORKFLOW, SELF_REPO
from ci_core.router import Router
from ci_core.self_smoke import prepare_pr, prepare_pr_cleanup
from ci_core.test_broker import BrokerClient
from ci_core.test_supervisor import TestSupervisor
from tests.test_auto_update_version_switch import checkout, settings
from tests.test_supervisor_fault_injection import FakeGitHubClient

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_au10_exclusive_lock_and_idempotent_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exclusive.lock"
            with exclusive(path):
                with self.assertRaisesRegex(Exception, "update_already_running"):
                    with exclusive(path):
                        pass
            with exclusive(path):
                pass
            root = Path(tmp)
            version, sha = checkout(root, "idempotent")
            with (
                patch(
                    "auto_update.update.current_release", return_value=(version, sha)
                ),
                patch("auto_update.update.check_source", return_value=sha),
                patch("auto_update.update.stop_worker") as stopped,
            ):
                self.assertEqual(_run(settings(root)), 0)
                stopped.assert_not_called()

    def test_update_waits_for_pr_deploy_and_broker_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, old_sha = checkout(root, "running")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            config = settings(root)
            plan_env = {
                "GITHUB_REPOSITORY": SELF_REPO,
                "GITHUB_RUN_ID": "45765918150",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": old_sha,
                "PR_NUMBER": "64",
                "PR_HEAD_SHA": "a" * 40,
                "SELF_SMOKE_PROFILE": "blue-only",
            }
            plan = prepare_pr({**plan_env, "SELF_SMOKE_PLAN": str(root / "plan.json")})
            cleanup = prepare_pr_cleanup(
                {**plan_env, "SELF_SMOKE_PLAN": str(root / "cleanup.json")}
            )
            run = {
                "id": plan["run_id"],
                "run_attempt": plan["attempt"],
                "head_sha": old_sha,
                "name": "lite-ci-pr-smoke",
                "path": f".github/workflows/{PR_SMOKE_WORKFLOW}",
                "status": "in_progress",
                "_ci_repo": SELF_REPO,
                "_ci_pr_number": 64,
            }
            client = FakeGitHubClient(head_sha=plan_env["PR_HEAD_SHA"])
            cleanup_requested = False
            client.running_workflows = lambda: [run]
            client.ci_request = lambda run: plan
            client.ci_cleanup_request = lambda run: (
                cleanup if cleanup_requested else None
            )

            preparing = threading.Event()
            allow_deploy = threading.Event()
            sockets = []
            routers = []
            stop, start = Mock(), Mock()
            now = 0

            def prepare_code(_client, _run, _lifecycle, code_dir):
                preparing.set()
                if not allow_deploy.wait(10):
                    raise TimeoutError("test did not release the PR deployment")
                code_dir.mkdir(parents=True, exist_ok=True)

            def launch_test_worker(_record, *, broker_sock, **kwargs):
                sockets.append(broker_sock)
                return {"pid": os.getpid(), "skip_ready_check": True}

            def supervisor(**kwargs):
                return TestSupervisor(
                    **kwargs,
                    runtime_root=root / "rt",
                    worktree_root=root / "worktrees",
                    code_preparer=prepare_code,
                    launcher=launch_test_worker,
                    stopper=lambda _worker, _record: None,
                    alive_checker=lambda _record: True,
                    enforce_peercred=False,
                )

            def router_factory(*args, **kwargs):
                worker = Router(*args, **kwargs)
                routers.append(worker)
                return worker

            def finish_active_job():
                ((future, _, _),) = routers[0].active.values()
                future.result(timeout=10)

            def assert_deferred():
                self.assertEqual(_run(config), 0)
                stop.assert_not_called()
                self.assertEqual((root / "current").resolve(), old)

            def during_deploy():
                try:
                    self.assertTrue(preparing.wait(10))
                    assert_deferred()
                finally:
                    allow_deploy.set()
                finish_active_job()

            def between_cases():
                nonlocal cleanup_requested
                self.assertFalse(routers[0].active)
                broker = BrokerClient(sockets[0])
                target = plan["cases"][0]["lifecycle"]["target_worker"]
                self.assertEqual(broker.hello()["worker_id"], target)
                assert_deferred()
                self.assertEqual(broker.hello()["worker_id"], target)
                cleanup_requested = True

            def after_cleanup():
                self.assertFalse(sockets[0].exists())
                self.assertEqual(_run(config), 0)
                stop.assert_called_once_with(config.unit)
                start.assert_called_once_with(config.unit)
                self.assertEqual((root / "current").resolve(), new)
                raise KeyboardInterrupt

            polls = iter(
                (during_deploy, between_cases, finish_active_job, after_cleanup)
            )

            def advance_poll(_seconds):
                nonlocal now
                next(polls)()
                now += 61
                return False

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_READY_FILE": str(config.ready_file),
                        "LITE_ACTIONS_AGENT_LOCK": str(root / "agent.lock"),
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "resources"),
                        "LITE_ACTIONS_RESULT_AUTHOR": "depeng1994",
                        "LITE_ACTIONS_PR_MAINTAINERS": "depeng1994",
                    },
                    clear=True,
                ),
                patch.object(sys, "argv", ["unified_router.py", "--worker-id", "blue"]),
                patch.object(signal, "signal"),
                patch.object(os, "umask"),
                patch.multiple(
                    unified_router,
                    time=SimpleNamespace(monotonic=lambda: now),
                    threading=SimpleNamespace(Event=lambda: Mock(wait=advance_poll)),
                    configured_repos=lambda: (SELF_REPO,),
                    deployment=lambda _worker: (
                        "blue",
                        {
                            "state_dir": str(root / "worker"),
                            "case_environments": {},
                            "address": "192.0.2.10",
                        },
                    ),
                    authorization=lambda: "fixture",
                    GitHubClient=lambda *args, **kwargs: client,
                    Router=router_factory,
                ),
                patch("ci_core.test_supervisor.TestSupervisor", side_effect=supervisor),
                patch("ci_core.router.progress", return_value=1),
                patch("ci_core.router.post") as post,
                patch.multiple(
                    "auto_update.update",
                    check_source=lambda *_: new_sha,
                    stage_source=lambda *_: new,
                    _validate_candidate=lambda _: None,
                    prepare_release=lambda *_: new,
                    stop_worker=stop,
                    start_worker=start,
                    wait_ready=lambda *args, **kwargs: (
                        1001,
                        time.monotonic() - kwargs["switch_time"],
                        time.monotonic() - kwargs["switch_time"] + 0.01,
                    ),
                ),
            ):
                try:
                    unified_router.main()
                finally:
                    if sockets and sockets[0].exists():
                        target = plan["cases"][0]["lifecycle"]["target_worker"]
                        routers[0].supervisor.cleanup_worker(target)

            self.assertEqual(
                [
                    (call.args[2]["kind"], call.args[3]["status"])
                    for call in post.call_args_list
                ],
                [("test_worker_deploy", "PASS"), ("test_worker_cleanup", "PASS")],
            )
            events = [
                json.loads(line)["event"]
                for line in config.log_file.read_text().splitlines()
            ]
            self.assertEqual(events, ["DEFERRED_BUSY", "DEFERRED_BUSY", "UPDATED"])

    def test_au13_bootstrap_pins_source_and_new_install_will_load_next(self):
        script = (ROOT / "auto_update/bootstrap.sh").read_text()
        self.assertIn('readlink -f "$ROOT/current"', script)
        self.assertIn('cd "$code_dir"', script)
        self.assertIn(
            'configured_python="$(printenv LITE_ACTIONS_UPDATE_PYTHON || true)"', script
        )
        self.assertIn('exec "$PYTHON" -u -m auto_update.update', script)
        self.assertIn("for name in python3 python3.11 python3.12", script)
        self.assertNotIn("kill -9", script)

    def test_au14_install_logging_timer_and_genericity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "management" / "updates.jsonl"
            record(
                p,
                "UPDATED",
                old_sha="a" * 40,
                target_sha="b" * 40,
                switch_to_start_ms=12,
            )
            rows = p.read_text().splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["switch_to_start_ms"], 12)
            self.assertEqual(p.stat().st_mode & 0o777, 0o644)
        for shell in ("auto_update/bootstrap.sh", "scripts/install_auto_update.sh"):
            subprocess.run(["bash", "-n", str(ROOT / shell)], check=True)
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/install_auto_update.sh"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--worker-unit", result.stdout)
        timer = (ROOT / "deploy/lite-actions-auto-update.timer").read_text()
        self.assertIn("OnUnitInactiveSec=3min", timer)
        self.assertIn("AccuracySec=10s", timer)
        paths = [
            ROOT / "auto_update" / p
            for p in (
                "check_source.py",
                "update.py",
                "version_switch.py",
                "update_log.py",
            )
        ]
        for path in paths:
            text = path.read_text().lower()
            self.assertNotIn("blue", text)
            self.assertNotIn("yellow", text)

    def test_worker_setup_failure_rolls_back_without_permanently_blocking_sha(
        self,
    ):
        from auto_update.version_switch import SwitchError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, _ = checkout(root, "old")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            stage = root / "staging" / "candidate"
            stage.mkdir(parents=True)
            cfg = settings(root)
            cfg.state_dir.mkdir()

            def restore(root_arg, old_arg, unit, ready_file):
                from auto_update.version_switch import switch_current

                switch_current(root_arg, old_arg)
                return "old_ready"

            with (
                patch("auto_update.update.check_source", return_value=new_sha),
                patch("auto_update.update.stage_source", return_value=stage),
                patch("auto_update.update._validate_candidate"),
                patch("auto_update.update.prepare_release", return_value=new),
                patch("auto_update.update.stop_worker"),
                patch("auto_update.update.start_worker"),
                patch(
                    "auto_update.update.wait_ready",
                    side_effect=SwitchError("worker_not_active"),
                ),
                patch("auto_update.update.restore_old", side_effect=restore),
            ):
                self.assertEqual(_run(cfg), 3)
            self.assertEqual((root / "current").resolve(), old)
            self.assertFalse((cfg.state_dir / "blocked.json").exists())
            txn = json.loads((cfg.state_dir / "transaction.json").read_text())
            self.assertEqual(txn["phase"], "ROLLED_BACK")

    def test_candidate_failure_still_permanently_blocks_sha(self):
        from auto_update.version_switch import SwitchError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, old_sha = checkout(root, "old")
            new, new_sha = checkout(root, "candidate")
            (root / "current").symlink_to(old)
            stage = root / "staging" / "candidate"
            stage.mkdir(parents=True)
            cfg = settings(root)
            cfg.state_dir.mkdir()
            with (
                patch("auto_update.update.check_source", return_value=new_sha),
                patch("auto_update.update.stage_source", return_value=stage),
                patch("auto_update.update._validate_candidate"),
                patch("auto_update.update.prepare_release", return_value=new),
                patch("auto_update.update.stop_worker"),
                patch("auto_update.update.start_worker"),
                patch(
                    "auto_update.update.wait_ready",
                    side_effect=SwitchError("candidate_runtime_invalid"),
                ),
                patch("auto_update.update.restore_old", return_value="old_ready"),
            ):
                self.assertEqual(_run(cfg), 3)
            blocked = json.loads((cfg.state_dir / "blocked.json").read_text())
            self.assertEqual(blocked["sha"], new_sha)


if __name__ == "__main__":
    unittest.main()
