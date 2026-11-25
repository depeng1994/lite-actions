"""Fault-injection tests for TestSupervisor pre-write ledger, fencing, and recovery."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ci_core.contract import SELF_REPO
from ci_core.resources import PhysicalResourceCoordinator, ResourceBusy
from ci_core.ssh_runner import remote_command, revoke_remote_lease
from ci_core.test_supervisor import TestSupervisor

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pools():
    return {
        "a3_shared_pool": {
            "owner": "yellow",
            "type": "ascend-a3",
            "process_indexing": "card-die",
            "default_docker": "torchtitan_ci_a3_r2",
            "force_reclaim": False,
            "max_jobs_per_host": 1,
            "nodes": [
                {
                    "host_id": "192.0.2.10:22",
                    "ssh": "host0",
                    "node_ip": "192.0.2.10",
                    "device_ids": [0, 1],
                    "allocation_groups": [{"devices": [0, 1], "verified": True}],
                },
                {
                    "host_id": "192.0.2.11:22",
                    "ssh": "host1",
                    "node_ip": "192.0.2.11",
                    "device_ids": [0, 1],
                    "allocation_groups": [{"devices": [0, 1], "verified": True}],
                },
            ],
        }
    }


def _workers():
    return {
        "yellow": {
            "enabled": True,
            "case_environments": {
                "torchtitan_ci_a3_r2": {
                    "ASCEND_SET_ENV_PATH": ["/usr/local/Ascend/set_env.sh"]
                }
            },
        }
    }


class FakeGitHubClient:
    def __init__(
        self,
        *,
        pr_state="open",
        head_sha="a" * 40,
        run_status="in_progress",
        run_attempt=1,
        jobs=None,
        pr_author="depeng1994",
        reviews=None,
    ):
        self.pr_state = pr_state
        self.head_sha = head_sha
        self.run_status = run_status
        self.run_attempt = run_attempt
        self.jobs = jobs if jobs is not None else []
        self.pr_author = pr_author
        self.reviews = reviews if reviews is not None else []

    def request(self, method, path, data=None, *, egress_category=None):
        if "/attempts/" in path and path.endswith("/jobs"):
            return {"jobs": list(self.jobs)}
        if "/reviews" in path:
            return list(self.reviews)
        if path.startswith("/pulls/"):
            return {
                "state": self.pr_state,
                "head": {"sha": self.head_sha},
                "user": {"login": self.pr_author},
            }
        raise AssertionError(f"unexpected request {method} {path}")

    def workflow_status(self, run_id):
        return {"status": self.run_status, "run_attempt": self.run_attempt}


class FakeControl:
    def __init__(self):
        self.registered = []
        self.deregistered = []
        self.telemetry = []

    def register_test_worker(self, record):
        self.registered.append(record["worker_id"])

    def deregister_test_worker(self, worker_id, *, reason=""):
        self.deregistered.append((worker_id, reason))

    def forward_test_telemetry(self, worker_id, *, events=(), snapshot=None):
        self.telemetry.append((worker_id, len(events)))


class SupervisorFaultInjectionTests(unittest.TestCase):
    @staticmethod
    def _deploy_run_and_item(
        *,
        run_id=501,
        attempt=1,
        pr_number=52,
        head_sha="a" * 40,
        ttl=14400,
    ):
        target_test_id = f"id{run_id}a{attempt}"
        target_worker = f"yellow-test-{target_test_id}"
        run = {
            "id": run_id,
            "run_attempt": attempt,
            "head_sha": head_sha,
            "path": ".github/workflows/lite_actions_pr_smoke.yml",
            "_ci_repo": SELF_REPO,
            "_ci_pr_number": pr_number,
        }
        deploy_item = {
            "case_id": "deploy_yellow",
            "test_id": "deploy_yellow",
            "worker": "yellow",
            "kind": "test_worker_deploy",
            "target_pool": None,
            "docker": None,
            "resources": {"nodes": 0, "devices_per_node": 0},
            "lifecycle": {
                "pr_number": pr_number,
                "pr_head_sha": head_sha,
                "target_test_id": target_test_id,
                "target_worker": target_worker,
                "github_run_id": run_id,
                "github_attempt": attempt,
                "generation": 1,
                "ttl_seconds": ttl,
            },
        }
        cleanup_item = {
            "case_id": "cleanup_yellow",
            "test_id": "cleanup_yellow",
            "worker": "yellow",
            "kind": "test_worker_cleanup",
            "target_pool": None,
            "docker": None,
            "resources": {"nodes": 0, "devices_per_node": 0},
            "lifecycle": {
                "pr_number": pr_number,
                "pr_head_sha": head_sha,
                "target_test_id": target_test_id,
                "target_worker": target_worker,
                "github_run_id": run_id,
                "github_attempt": attempt,
            },
        }
        return run, deploy_item, cleanup_item, target_worker

    @staticmethod
    def _make_supervisor(tmp: Path, *, client=None, control=None, clock=None, **kwargs):
        coord = PhysicalResourceCoordinator(tmp / "locks")
        return (
            TestSupervisor(
                zone="yellow",
                main_root=REPO_ROOT,
                state_dir=tmp / "main-state",
                pools=_pools(),
                pools_revision=1,
                workers=_workers(),
                author="depeng1994",
                address="192.0.2.10",
                release_sha="f" * 40,
                client=client,
                coordinator=coord,
                control_provider=(lambda: control) if control else None,
                runtime_root=tmp / "rt",
                worktree_root=tmp / "wt",
                code_preparer=lambda _c, _r, _l, d: d.mkdir(
                    parents=True, exist_ok=True
                ),
                launcher=kwargs.pop(
                    "launcher",
                    lambda rec, **kw: {
                        "pid": 999999,
                        "boot_id": "b",
                        "start_ticks": 1,
                        "skip_ready_check": True,
                    },
                ),
                stopper=kwargs.pop("stopper", lambda wid, rec: None),
                alive_checker=kwargs.pop("alive_checker", lambda rec: True),
                clock=clock or time.time,
                enforce_peercred=False,
                **kwargs,
            ),
            coord,
        )

    def test_01_crash_recovery_preserves_production_contention_until_revoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = FakeGitHubClient()
            ctrl = FakeControl()
            sup, coord = self._make_supervisor(root, client=gh, control=ctrl)
            run, deploy_item, _, target = self._deploy_run_and_item()
            out = sup.handle_deploy(gh, run, deploy_item)
            self.assertEqual(out["status"], "PASS")
            self.assertIn(target, ctrl.registered)

            alloc = [{"host_id": "192.0.2.10:22", "devices": [0, 1]}]
            lease = coord.acquire(
                alloc,
                owner_worker=target,
                owner_zone="yellow",
                pool="a3_shared_pool",
                is_production=False,
                task_key="test-lease",
            )
            lease.mark_launching(
                remote_nodes=[
                    {"host_id": "192.0.2.10:22", "ssh": "host0", "devices": [0, 1]}
                ]
            )
            # Simulate Main Worker SIGKILL crash (closing flock FD manually on crash object).
            lease._stack.close()
            lease._stack = None
            sup._brokers[target].stop()

            # Restarted supervisor where remote revoke initially fails on host0.
            sup_fail, coord2 = self._make_supervisor(
                root,
                client=gh,
                control=ctrl,
                revoke_fn=lambda _lease: set(),
            )
            rec = sup_fail.recover()
            self.assertEqual(len(rec), 1)
            self.assertEqual(rec[0]["status"], "INFRA_ERROR")
            self.assertFalse(rec[0]["release_verified"])
            # Production must still be blocked while lease remains unrevoked.
            with self.assertRaises(ResourceBusy):
                coord2.acquire(
                    alloc,
                    owner_worker="yellow",
                    owner_zone="yellow",
                    pool="a3_shared_pool",
                    is_production=True,
                    task_key="prod-1",
                )

            # Once host0 becomes reachable and revokes cleanly, recovery frees hardware.
            sup_ok, coord3 = self._make_supervisor(
                root,
                client=gh,
                control=ctrl,
                revoke_fn=lambda _lease: {"host0"},
            )
            rec_ok = sup_ok.recover()
            self.assertEqual(rec_ok[0]["status"], "PASS")
            coord3.clear_quarantine()
            prod = coord3.acquire(
                alloc,
                owner_worker="yellow",
                owner_zone="yellow",
                pool="a3_shared_pool",
                is_production=True,
                task_key="prod-1",
            )
            prod.close()

    def test_02_cleanup_writes_revoked_before_delayed_ssh_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ldir = root / "remote-leases"
            dest = root / "remote-dest"
            dest.mkdir()
            pkg = dest / "repo/tests/integration_tests/tools/lite_actions"
            pkg.mkdir(parents=True)
            for p in (
                dest / "repo/tests",
                dest / "repo/tests/integration_tests",
                dest / "repo/tests/integration_tests/tools",
                pkg,
            ):
                (p / "__init__.py").write_text("", encoding="utf-8")
            started_marker = dest / "child.started"
            (pkg / "entrypoint.py").write_text(
                f"import pathlib\npathlib.Path({str(started_marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            setup = dest / "set_env.sh"
            setup.write_text("true\n", encoding="utf-8")
            node = {
                "host_id": "192.0.2.10:22",
                "ssh": "host0",
                "node_ip": "192.0.2.10",
                "devices": [0, 1],
                "position": 0,
            }

            def local_ssh(_host, command, *, timeout=45, check=True):
                return subprocess.run(
                    ["bash", "-c", command],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=check,
                )

            gh = FakeGitHubClient()
            sup, coord = self._make_supervisor(
                root,
                client=gh,
                revoke_fn=lambda lease: revoke_remote_lease(
                    lease, execute=local_ssh, lease_dir=str(ldir), check_npu=False
                ),
            )
            run, deploy_item, cleanup_item, target = self._deploy_run_and_item()
            self.assertEqual(sup.handle_deploy(gh, run, deploy_item)["status"], "PASS")
            lease = coord.acquire(
                [{"host_id": "192.0.2.10:22", "devices": [0, 1]}],
                owner_worker=target,
                owner_zone="yellow",
                pool="a3_shared_pool",
                is_production=False,
                task_key="race-test",
            )
            lease.mark_launching(remote_nodes=[node])
            lease_id = lease.lease_id

            # Release local flock FD just before cleanup simulates Main Worker cleanup path.
            lease._stack.close()
            lease._stack = None
            out = sup.handle_cleanup(gh, run, cleanup_item)
            self.assertEqual(out["status"], "PASS")

            # Delayed SSH command arrives after cleanup wrote REVOKED -> must abort with 0 NPU procs.
            delayed_cmd = remote_command(
                node,
                [node],
                {"kind": "npu_smoke", "env_vars": {"ASCEND_SET_ENV_PATH": str(setup)}},
                str(dest),
                "delayed-stamp",
                30,
                "smoke_id",
                lease_id=lease_id,
                lease_dir=str(ldir),
            )
            proc = subprocess.run(
                ["bash", "-c", delayed_cmd], capture_output=True, text=True, timeout=20
            )
            self.assertEqual(proc.returncode, 130)
            self.assertIn("REMOTE_LEASE_REVOKED", proc.stdout)
            self.assertFalse(started_marker.exists())

    def test_03_concurrent_deploy_and_cleanup_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = FakeGitHubClient()
            events = []
            mutex_guard = threading.Lock()
            active_ops = {"count": 0}
            entered_deploy = threading.Event()
            allow_deploy_finish = threading.Event()

            def launcher(rec, **kw):
                with mutex_guard:
                    active_ops["count"] += 1
                    self.assertEqual(active_ops["count"], 1)
                entered_deploy.set()
                allow_deploy_finish.wait(5)
                events.append("deploy_done")
                with mutex_guard:
                    active_ops["count"] -= 1
                return {
                    "pid": 88888,
                    "boot_id": "b",
                    "start_ticks": 1,
                    "skip_ready_check": True,
                }

            def stopper(wid, rec):
                with mutex_guard:
                    active_ops["count"] += 1
                    self.assertEqual(active_ops["count"], 1)
                    events.append("cleanup_done")
                    active_ops["count"] -= 1

            sup, coord = self._make_supervisor(
                root, client=gh, launcher=launcher, stopper=stopper
            )
            run, deploy_item, cleanup_item, target = self._deploy_run_and_item()
            results = {}

            def run_deploy():
                results["deploy"] = sup.handle_deploy(gh, run, deploy_item)

            def run_cleanup():
                entered_deploy.wait(5)
                t = threading.Timer(0.15, allow_deploy_finish.set)
                t.start()
                results["cleanup"] = sup.handle_cleanup(gh, run, cleanup_item)

            t1 = threading.Thread(target=run_deploy)
            t2 = threading.Thread(target=run_cleanup)
            t1.start()
            t2.start()
            t1.join(10)
            t2.join(10)
            self.assertEqual(results["deploy"]["status"], "PASS")
            self.assertEqual(results["cleanup"]["status"], "PASS")
            self.assertEqual(events, ["deploy_done", "cleanup_done"])
            self.assertEqual(sup._read_ledger()["workers"][target]["status"], "STOPPED")

    def test_04_partial_rerun_detected_with_rerun_all_jobs_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Cleanup triggered on attempt 2 without prior deploy record.
            gh_cleanup = FakeGitHubClient(
                run_attempt=2,
                jobs=[{"name": "cleanup", "run_attempt": 2, "status": "in_progress"}],
            )
            sup, _ = self._make_supervisor(root, client=gh_cleanup)
            run, deploy_item, cleanup_item, _ = self._deploy_run_and_item(attempt=2)
            out_clean = sup.handle_cleanup(gh_cleanup, run, cleanup_item)
            self.assertEqual(out_clean["status"], "FAIL")
            self.assertIn("Re-run all jobs", "\n".join(out_clean["tail"]))

            # Deploy attempt 2 where attempt/2/jobs shows deploy still at run_attempt=1 (partial).
            gh_partial_deploy = FakeGitHubClient(
                run_attempt=2,
                jobs=[
                    {"name": "prepare", "run_attempt": 1, "status": "completed"},
                    {"name": "deploy-yellow", "run_attempt": 1, "status": "completed"},
                ],
            )
            out_dep = sup.handle_deploy(gh_partial_deploy, run, deploy_item)
            self.assertEqual(out_dep["status"], "FAIL")
            self.assertIn("Re-run all jobs", "\n".join(out_dep["tail"]))

            # Full "Re-run all jobs" with deploy run_attempt=2 succeeds.
            gh_full_rerun = FakeGitHubClient(
                run_attempt=2,
                jobs=[
                    {"name": "prepare", "run_attempt": 2, "status": "completed"},
                    {
                        "name": "deploy-yellow",
                        "run_attempt": 2,
                        "status": "in_progress",
                    },
                ],
            )
            self.assertEqual(
                sup.handle_deploy(gh_full_rerun, run, deploy_item)["status"], "PASS"
            )

    def test_05_partial_host_revoke_failure_quarantines_and_preserves_foreign_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign = subprocess.Popen(["sleep", "30"])
            try:
                gh = FakeGitHubClient()
                sup, coord = self._make_supervisor(
                    root,
                    client=gh,
                    revoke_fn=lambda _lease: {"host0"},  # Missing host1.
                )
                run, deploy_item, cleanup_item, target = self._deploy_run_and_item()
                sup.handle_deploy(gh, run, deploy_item)
                # Tamper ledger PID to point to foreign PID with mismatched start_ticks.
                with sup._locked():
                    doc = sup._read_ledger()
                    doc["workers"][target]["pid"] = foreign.pid
                    doc["workers"][target]["start_ticks"] = 999999999999
                    sup._write_ledger(doc)

                alloc = [
                    {"host_id": "192.0.2.10:22", "devices": [0, 1]},
                    {"host_id": "192.0.2.11:22", "devices": [0, 1]},
                ]
                lease = coord.acquire(
                    alloc,
                    owner_worker=target,
                    owner_zone="yellow",
                    pool="a3_shared_pool",
                    is_production=False,
                    task_key="multi-lease",
                )
                lease.mark_launching(
                    remote_nodes=[
                        {"host_id": "192.0.2.10:22", "ssh": "host0", "devices": [0, 1]},
                        {"host_id": "192.0.2.11:22", "ssh": "host1", "devices": [0, 1]},
                    ]
                )
                lease._stack.close()
                lease._stack = None
                out = sup.handle_cleanup(gh, run, cleanup_item)
                self.assertEqual(out["status"], "INFRA_ERROR")
                self.assertFalse(out["release_verified"])
                self.assertEqual(len(coord.all_active_leases()), 1)
                self.assertIn(("192.0.2.11:22", 0), coord.all_quarantined_resources())
                # Unrelated process with mismatched identity was NEVER killed.
                self.assertIsNone(foreign.poll())
            finally:
                foreign.terminate()
                foreign.wait(5)

    def test_06_ttl_expiry_and_pr_head_move_watchdog_reap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [1000.0]
            gh = FakeGitHubClient(head_sha="a" * 40)
            ctrl = FakeControl()
            sup, _ = self._make_supervisor(
                root, client=gh, control=ctrl, clock=lambda: now[0]
            )
            run, deploy_item, _, target1 = self._deploy_run_and_item(
                run_id=601, ttl=300
            )
            self.assertEqual(sup.handle_deploy(gh, run, deploy_item)["status"], "PASS")
            now[0] = 1301.0
            reaped = sup.tick_watchdog(gh, check_remote_every=9999)
            self.assertEqual(len(reaped), 1)
            self.assertEqual(
                sup._read_ledger()["workers"][target1]["stopped_reason"], "ttl_expired"
            )

            run2, deploy2, _, target2 = self._deploy_run_and_item(run_id=602, ttl=14400)
            self.assertEqual(sup.handle_deploy(gh, run2, deploy2)["status"], "PASS")
            gh.head_sha = "b" * 40  # PR head moved!
            sup._last_watchdog_remote_check = 0.0
            reaped2 = sup.tick_watchdog(gh, check_remote_every=0.0)
            self.assertEqual(len(reaped2), 1)
            self.assertEqual(
                sup._read_ledger()["workers"][target2]["stopped_reason"],
                "pr_changed_or_closed",
            )

    def test_07_idempotent_cleanup_and_invalid_authority_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh_closed = FakeGitHubClient(pr_state="closed")
            launched = []
            sup, _ = self._make_supervisor(
                root,
                client=gh_closed,
                launcher=lambda *a, **kw: launched.append(1) or {},
            )
            run, deploy_item, cleanup_item, target = self._deploy_run_and_item()
            out = sup.handle_deploy(gh_closed, run, deploy_item)
            self.assertEqual(out["status"], "FAIL")
            self.assertEqual(launched, [])
            self.assertNotIn(target, sup._read_ledger()["workers"])

            first = sup.handle_cleanup(gh_closed, run, cleanup_item)
            second = sup.handle_cleanup(gh_closed, run, cleanup_item)
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(second["status"], "PASS")

    def test_08_deploy_authority_enforces_maintainer_and_review_rules(self):
        sha, earlier = "a" * 40, "b" * 40

        def rev(state, cid=sha):
            return {"user": {"login": "depeng1994"}, "state": state, "commit_id": cid}

        cases = [
            ("self_pr_direct", "depeng1994", [], True),
            ("external_unreviewed", "contributor", [], False),
            (
                "external_approved_earlier_sha",
                "contributor",
                [rev("APPROVED", earlier)],
                True,
            ),
            (
                "external_dismissed",
                "contributor",
                [rev("APPROVED"), rev("DISMISSED")],
                False,
            ),
            (
                "external_approved_then_commented",
                "contributor",
                [rev("APPROVED"), rev("COMMENTED")],
                True,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            sup, _ = self._make_supervisor(Path(tmp))
            run, deploy_item, _, _ = self._deploy_run_and_item(run_id=701, head_sha=sha)
            for name, author, reviews, ok in cases:
                with self.subTest(name=name):
                    gh = FakeGitHubClient(
                        head_sha=sha, pr_author=author, reviews=reviews
                    )
                    if ok:
                        sup.verify_deploy_authority(gh, run, deploy_item["lifecycle"])
                    else:
                        with self.assertRaisesRegex(
                            ValueError, "requires maintainer APPROVED review"
                        ):
                            sup.verify_deploy_authority(
                                gh, run, deploy_item["lifecycle"]
                            )


if __name__ == "__main__":
    unittest.main()
