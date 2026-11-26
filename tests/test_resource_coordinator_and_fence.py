"""Tests for PhysicalResourceCoordinator, two-stage remote lease fence, and TestBroker."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.config import execution_pools
from ci_core.contract import SELF_REPO
from ci_core.egress_policy import Deferred
from ci_core.executor import Execution, ExternalNpuOccupancy
from ci_core.resources import (
    PhysicalResourceCoordinator,
    ResourceBusy,
    UnverifiedRemoteRevoke,
)
from ci_core.scheduler import validate_allocation
from ci_core.ssh_runner import remote_command, revoke_remote_lease
from ci_core.test_broker import (
    BrokerClient,
    LeaseConfinementViolation,
    TestBrokerServer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_pools():
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
                    "ssh": "root@192.0.2.10",
                    "node_ip": "192.0.2.10",
                    "device_ids": [0, 1, 2, 3, 4, 5, 6, 7],
                    "allocation_groups": [
                        {
                            "devices": [0, 1, 2, 3, 4, 5, 6, 7],
                            "verified": True,
                        }
                    ],
                },
                {
                    "host_id": "192.0.2.11:22",
                    "ssh": "root@192.0.2.11",
                    "node_ip": "192.0.2.11",
                    "device_ids": [0, 1, 2, 3, 4, 5, 6, 7],
                    "allocation_groups": [
                        {
                            "devices": [0, 1, 2, 3, 4, 5, 6, 7],
                            "verified": True,
                        }
                    ],
                },
            ],
            "verified_multinode_allocations": [
                [
                    {"host_id": "192.0.2.10:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]},
                    {"host_id": "192.0.2.11:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]},
                ]
            ],
        }
    }


def _make_workers():
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


class PhysicalResourceCoordinatorTests(unittest.TestCase):
    def test_parallel_disjoint_and_conflicting_acquisitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = PhysicalResourceCoordinator(Path(tmp))
            alloc_a = [
                {"host_id": "192.0.2.10:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]}
            ]
            alloc_b = [
                {"host_id": "192.0.2.11:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]}
            ]
            lease_prod = coord.acquire(
                alloc_a,
                owner_worker="blue",
                owner_zone="blue",
                pool="a3_shared_pool",
                is_production=True,
                task_key="prod-1",
            )
            try:
                with self.assertRaises(ResourceBusy):
                    coord.acquire(
                        alloc_a,
                        owner_worker="yellow-test-id10a1",
                        owner_zone="yellow",
                        pool="a3_shared_pool",
                        is_production=False,
                        task_key="test-overlap",
                    )
                lease_test = coord.acquire(
                    alloc_b,
                    owner_worker="yellow-test-id10a1",
                    owner_zone="yellow",
                    pool="a3_shared_pool",
                    is_production=False,
                    task_key="test-parallel",
                )
                try:
                    self.assertEqual(len(coord.all_active_leases()), 2)
                finally:
                    lease_test.close()
            finally:
                lease_prod.close()
            self.assertEqual(coord.all_active_leases(), [])

    def test_production_priority_preempts_queued_test_acquires(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = PhysicalResourceCoordinator(Path(tmp))
            alloc = [{"host_id": "192.0.2.10:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]}]
            coord.register_production_waiter(
                "prod-wait-1",
                owner_worker="yellow",
                pool="a3_shared_pool",
                resources=[("192.0.2.10:22", d) for d in range(8)],
            )
            with self.assertRaisesRegex(ResourceBusy, "PRODUCTION_PRIORITY"):
                coord.acquire(
                    alloc,
                    owner_worker="yellow-test-id20a1",
                    owner_zone="yellow",
                    pool="a3_shared_pool",
                    is_production=False,
                    task_key="test-1",
                )
            coord.clear_production_waiter("prod-wait-1")
            lease = coord.acquire(
                alloc,
                owner_worker="yellow-test-id20a1",
                owner_zone="yellow",
                pool="a3_shared_pool",
                is_production=False,
                task_key="test-1",
            )
            lease.close()

    def test_drain_delete_blocks_and_remote_revoke_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            coord = PhysicalResourceCoordinator(Path(tmp))
            alloc = [
                {"host_id": "192.0.2.10:22", "devices": [0, 1]},
                {"host_id": "192.0.2.11:22", "devices": [0, 1]},
            ]
            res_keys = [("192.0.2.10:22", 0), ("192.0.2.11:22", 0)]
            lease = coord.acquire(
                alloc,
                owner_worker="yellow-test-id30a1",
                owner_zone="yellow",
                pool="a3_shared_pool",
                is_production=False,
                task_key="test-30",
            )
            lease.mark_launching(
                remote_nodes=[
                    {"host_id": "192.0.2.10:22", "ssh": "n0", "devices": [0, 1]},
                    {"host_id": "192.0.2.11:22", "ssh": "n1", "devices": [0, 1]},
                ]
            )
            lease.mark_active()
            with self.assertRaisesRegex(ValueError, "ACTIVE_LEASES_REMAINING"):
                coord.check_delete_allowed(res_keys)

            coord.set_drain_state("yellow", res_keys)
            self.assertEqual(coord.all_draining_resources(), set(res_keys))

            with self.assertRaises(UnverifiedRemoteRevoke):
                lease.release(remote_revoked_hosts={"n0"})
            # Lease remains recorded for reconciliation without writing quarantine locks.
            self.assertEqual(len(coord.all_active_leases()), 1)
            self.assertEqual(coord.all_quarantined_resources(), set())
            with self.assertRaisesRegex(ValueError, "ACTIVE_LEASES_REMAINING"):
                coord.check_delete_allowed(res_keys)

            coord.release(lease.lease_id, remote_revoked_hosts={"n0", "n1"})
            self.assertEqual(coord.all_active_leases(), [])
            coord.clear_drain_state("yellow")
            coord.check_delete_allowed(res_keys)


@unittest.skipUnless(
    sys.platform == "linux" and os.geteuid() == 0,
    "Requires root Linux PID visibility for remote lease fence verification",
)
class RemoteLeaseFenceTests(unittest.TestCase):
    @staticmethod
    def _setup_fake_repo(
        base: Path, *, sleep_seconds: float = 0.0
    ) -> tuple[dict, dict]:
        repo = base / "repo"
        pkg = repo / "tests/integration_tests/tools/lite_actions"
        pkg.mkdir(parents=True)
        for folder in (
            repo / "tests",
            repo / "tests/integration_tests",
            repo / "tests/integration_tests/tools",
            pkg,
        ):
            (folder / "__init__.py").write_text("", encoding="utf-8")
        marker = base / "child.started"
        if sleep_seconds > 0:
            (pkg / "entrypoint.py").write_text(
                f"import pathlib,subprocess,time\n"
                f"p=subprocess.Popen(['sleep','{sleep_seconds}'],start_new_session=True)\n"
                f"pathlib.Path({str(base / 'detached.pid')!r}).write_text(str(p.pid))\n"
                f"pathlib.Path({str(marker)!r}).write_text('started')\n"
                f"time.sleep({sleep_seconds})\n",
                encoding="utf-8",
            )
        else:
            (pkg / "entrypoint.py").write_text(
                f"import pathlib\npathlib.Path({str(marker)!r}).write_text('started')\n",
                encoding="utf-8",
            )
        setup = base / "set_env.sh"
        setup.write_text("true\n", encoding="utf-8")
        node = {
            "host_id": "192.0.2.10:22",
            "ssh": "node0-ssh",
            "node_ip": "192.0.2.10",
            "devices": [0, 1],
            "position": 0,
        }
        runner = {"kind": "npu_smoke", "env_vars": {"ASCEND_SET_ENV_PATH": str(setup)}}
        return node, runner

    @staticmethod
    def _local_ssh(host: str, command: str, *, timeout: int = 45, check: bool = True):
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def test_prelaunch_revoked_aborts_before_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ldir = base / "leases"
            dest = base / "dest"
            dest.mkdir()
            node, runner = self._setup_fake_repo(dest)
            lease_id = "leaseprecheck01"
            revoked = revoke_remote_lease(
                {"lease_id": lease_id, "remote_nodes": [node]},
                execute=self._local_ssh,
                lease_dir=str(ldir),
                check_npu=False,
            )
            self.assertEqual(revoked, {"node0-ssh"})
            cmd = remote_command(
                node,
                [node],
                runner,
                str(dest),
                "stamp1",
                30,
                "smoke_id",
                lease_id=lease_id,
                lease_dir=str(ldir),
            )
            proc = subprocess.run(
                ["bash", "-lc", cmd], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(proc.returncode, 130)
            self.assertIn("REMOTE_LEASE_REVOKED", proc.stdout)
            self.assertFalse((dest / "child.started").exists())

    def test_revoke_verifies_cleanup_while_the_container_has_an_unreaped_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease_dir = root / "leases"
            lease_dir.mkdir()
            dest = root / "dest"
            dest.mkdir()
            (dest / "job.intent").write_text("stamp-zombie")
            cgroup = root / "cgroup"
            cgroup.mkdir()
            lease_id = "leasezombie01"
            launcher = subprocess.Popen([sys.executable, "-c", "pass"])
            try:
                os.waitid(os.P_PID, launcher.pid, os.WEXITED | os.WNOWAIT)
                stat_file = Path(f"/proc/{launcher.pid}/stat")
                fields = stat_file.read_text().rsplit(") ", 1)[1].split()
                self.assertEqual(fields[0], "Z")
                (cgroup / "cgroup.procs").write_text(str(launcher.pid))
                (lease_dir / f"{lease_id}.state").write_text("ACTIVE\n")
                (lease_dir / f"{lease_id}.json").write_text(
                    json.dumps(
                        {
                            "pgid": launcher.pid,
                            "start_time": fields[19],
                            "cgroup_dir": str(cgroup),
                            "dest": str(dest),
                            "marker": "stamp-zombie",
                        }
                    )
                )
                revoked = revoke_remote_lease(
                    {"lease_id": lease_id, "remote_nodes": [{"ssh": "node0-ssh"}]},
                    execute=self._local_ssh,
                    lease_dir=str(lease_dir),
                )
                self.assertEqual(revoked, {"node0-ssh"})
                self.assertEqual(
                    (lease_dir / f"{lease_id}.state").read_text(), "REVOKED\n"
                )
                self.assertEqual(
                    (dest / "cleanup_verified.txt").read_text(), "stamp-zombie"
                )
                self.assertEqual(
                    stat_file.read_text().rsplit(") ", 1)[1].split()[0], "Z"
                )
            finally:
                launcher.wait(timeout=3)

    def test_active_revoke_kills_only_owned_and_respects_mock_npu_smi(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ldir = base / "leases"
            dest = base / "dest"
            dest.mkdir()
            node, runner = self._setup_fake_repo(dest, sleep_seconds=60)
            lease_id = "leaseactive02"
            cmd = remote_command(
                node,
                [node],
                runner,
                str(dest),
                "stamp2",
                90,
                "smoke_id",
                lease_id=lease_id,
                lease_dir=str(ldir),
            )
            launcher = subprocess.Popen(
                ["bash", "-lc", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            foreign = subprocess.Popen(
                ["sleep", "60"],
                env={**os.environ, "LITE_CI_LEASE_ID": "otherlease99"},
            )
            try:
                deadline = time.monotonic() + 10
                while not (dest / "child.started").exists():
                    if time.monotonic() > deadline:
                        self.fail("remote test child did not start")
                    time.sleep(0.05)
                self.assertEqual(
                    (ldir / f"{lease_id}.state").read_text().strip(), "ACTIVE"
                )
                detached_pid = int((dest / "detached.pid").read_text().strip())

                # Fake npu-smi with foreign process ONLY on unleased chip (card 2 die 0 -> chip 4).
                fake_bin = base / "bin"
                fake_bin.mkdir()
                npu_smi = fake_bin / "npu-smi"
                npu_smi.write_text(
                    "#!/bin/sh\n"
                    "cat <<'EOF'\n"
                    "| NPU   Chip | Process id | Process name | Memory |\n"
                    "| 2 0 | 99999 | python3 | 100 |\n"
                    "EOF\n",
                    encoding="utf-8",
                )
                npu_smi.chmod(0o755)
                with patch.dict(
                    os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
                ):
                    revoked = revoke_remote_lease(
                        {"lease_id": lease_id, "remote_nodes": [node]},
                        execute=self._local_ssh,
                        lease_dir=str(ldir),
                        check_npu=True,
                    )
                self.assertEqual(revoked, {"node0-ssh"})
                launcher.wait(5)
                self.assertFalse(Path(f"/proc/{detached_pid}").exists())
                self.assertIsNone(foreign.poll())
                self.assertEqual((dest / "cleanup_verified.txt").read_text(), "stamp2")

                # If mock npu-smi reports occupancy on LEASED chip 0, revoke fails verification.
                npu_smi.write_text(
                    "#!/bin/sh\n"
                    "cat <<'EOF'\n"
                    "| NPU   Chip | Process id | Process name | Memory |\n"
                    "| 0 0 | 88888 | python3 | 100 |\n"
                    "EOF\n",
                    encoding="utf-8",
                )
                with patch.dict(
                    os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
                ):
                    revoked_dirty = revoke_remote_lease(
                        {"lease_id": lease_id, "remote_nodes": [node]},
                        execute=self._local_ssh,
                        lease_dir=str(ldir),
                        check_npu=True,
                    )
                self.assertEqual(revoked_dirty, set())
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait(5)
                foreign.terminate()
                foreign.wait(5)


class TestBrokerAndWorkerSandboxTests(unittest.TestCase):
    def _make_broker(
        self,
        tmp: Path,
        *,
        worker_id="yellow-test-id77a1",
        client=None,
        allowed_uid=None,
        peercred_reader=None,
        control_sink=None,
    ):
        pools = _make_pools()
        workers = _make_workers()
        sock = tmp / "broker" / "broker.sock"
        state_dir = tmp / "test-state"
        pools_path = state_dir / "pools-active.json"
        coord = PhysicalResourceCoordinator(tmp / "locks")
        main_root = tmp / "main-checkout"
        pkg = main_root / "tests/integration_tests/tools/lite_actions"
        pkg.mkdir(parents=True, exist_ok=True)
        for d in (
            main_root / "tests",
            main_root / "tests/integration_tests",
            main_root / "tests/integration_tests/tools",
            pkg,
        ):
            (d / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "entrypoint.py").write_text(
            "raise SystemExit('PROTECTED_BASELINE_MARKER')\n", encoding="utf-8"
        )
        (main_root / "config").mkdir(exist_ok=True)
        (main_root / "config/pools.json").write_text(
            json.dumps(pools), encoding="utf-8"
        )
        (main_root / "config/workers.json").write_text(
            json.dumps(workers), encoding="utf-8"
        )

        class DummyClient:
            def __init__(self):
                self.comments = []
                self.runs = []

            def running_workflows(self):
                return list(self.runs)

            def commit_comments(self, sha, **kwargs):
                return list(self.comments)

            def send_result(self, sha, body, *, category=None):
                self.comments.append(
                    {"id": len(self.comments) + 1, "sha": sha, "body": body}
                )
                return self.comments[-1]

            def workflow_status(self, run_id):
                return {"status": "in_progress", "run_attempt": 1}

        broker = TestBrokerServer(
            socket_path=sock,
            worker_id=worker_id,
            main_root=main_root,
            state_dir=state_dir,
            pools_path=pools_path,
            pools=pools,
            pools_revision=1,
            workers=workers,
            author="depeng1994",
            address="192.0.2.10",
            release_sha="a" * 40,
            client=client or DummyClient(),
            coordinator=coord,
            repos=(SELF_REPO, "depeng1994/torchtitan-npu"),
            scope={"repo": SELF_REPO, "pr_number": 77},
            control_sink=control_sink,
            allowed_uid=allowed_uid,
            peercred_reader=peercred_reader,
            protected_baseline_root=main_root,
        )
        return broker, sock, main_root

    def test_peercred_and_confinement_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker, sock, _ = self._make_broker(
                root,
                peercred_reader=lambda _conn: (9999, os.getuid() + 1, 65534),
            )
            broker.start()
            try:
                mode = stat.S_IMODE(sock.stat().st_mode)
                self.assertEqual(mode, 0o600)
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                with self.assertRaisesRegex(
                    LeaseConfinementViolation, "LEASE_CONFINEMENT_VIOLATION"
                ):
                    client.hello()
                self.assertEqual(broker.audit_log[0]["event"], "peercred_rejected")
            finally:
                broker.stop()

            broker2, sock2, _ = self._make_broker(root / "ok")
            broker2.start()
            try:
                client = BrokerClient(sock2, worker_id="yellow-test-id77a1")
                hello = client.hello()
                self.assertEqual(hello["worker_id"], "yellow-test-id77a1")
                self.assertNotIn("ssh", hello["pools"]["a3_shared_pool"]["nodes"][0])
                for op in ("SELF_REPORT_PASS", "RELEASE_LEASE", "SYNC_CONFIG"):
                    with self.assertRaisesRegex(
                        LeaseConfinementViolation, "LEASE_CONFINEMENT_VIOLATION"
                    ):
                        client._call(op, {})
                with self.assertRaisesRegex(
                    LeaseConfinementViolation, "LEASE_CONFINEMENT_VIOLATION"
                ):
                    client.post_terminal_report(
                        {
                            "id": 100,
                            "run_attempt": 1,
                            "head_sha": "b" * 40,
                            "path": ".github/workflows/lite_actions_pr_smoke.yml",
                            "_ci_repo": SELF_REPO,
                            "pull_requests": [{"number": 77}],
                        },
                        {
                            "case_id": "c1",
                            "test_id": "smoke_8p",
                            "worker": "yellow-test-id77a1",
                            "kind": "npu_smoke",
                            "target_pool": "a3_shared_pool",
                            "docker": "torchtitan_ci_a3_r2",
                            "resources": {"nodes": 1, "devices_per_node": 8},
                        },
                        "c" * 64,
                        {"status": "PASS", "exit_code": 0},
                    )
                with self.assertRaisesRegex(
                    LeaseConfinementViolation, "LEASE_CONFINEMENT_VIOLATION"
                ):
                    client._call(
                        "REQUEST_RUN",
                        {
                            "key": "k1",
                            "allocation": [
                                {"host_id": "192.0.2.10:22", "devices": [0]}
                            ],
                            "run": {
                                "id": 100,
                                "run_attempt": 1,
                                "head_sha": "b" * 40,
                                "path": ".github/workflows/lite_actions_pr_smoke.yml",
                                "_ci_repo": SELF_REPO,
                                "pull_requests": [{"number": 77}],
                            },
                            "item": {
                                "case_id": "c1",
                                "test_id": "smoke_8p",
                                "worker": "yellow-test-id77a1",
                                "kind": "npu_smoke",
                                "target_pool": "a3_shared_pool",
                                "docker": "torchtitan_ci_a3_r2",
                                "resources": {"nodes": 1, "devices_per_node": 8},
                            },
                            "digest": "c" * 64,
                            "defaults": {},
                        },
                    )
            finally:
                broker2.stop()

    def test_protected_baseline_gateway_ignores_pr_fake_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen_archive_entries = []
            broker, sock, main_root = self._make_broker(root)

            def fake_execute(case, subrun, *, root=None):
                arch = Path(subrun["_ci_stage_archive"])
                with tarfile.open(arch, "r:gz") as tf:
                    content = (
                        tf.extractfile(
                            "tests/integration_tests/tools/lite_actions/entrypoint.py"
                        )
                        .read()
                        .decode()
                    )
                seen_archive_entries.append(content)
                log = root / "run.log"
                # Protected baseline intentionally fails; PR fake CASE_RESULT PASS 0 is not used.
                log.write_text("CASE_RESULT smoke_8p FAIL 1\n", encoding="utf-8")
                return Execution(1, ["PROTECTED_BASELINE_MARKER"], log, True)

            broker.start()
            try:
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                run = {
                    "id": 77,
                    "run_attempt": 1,
                    "head_sha": "b" * 40,
                    "path": ".github/workflows/lite_actions_pr_smoke.yml",
                    "_ci_repo": SELF_REPO,
                    "pull_requests": [{"number": 77}],
                }
                item = {
                    "case_id": "pr_smoke_1",
                    "test_id": "smoke_8p",
                    "worker": "yellow-test-id77a1",
                    "kind": "npu_smoke",
                    "target_pool": "a3_shared_pool",
                    "docker": "torchtitan_ci_a3_r2",
                    "resources": {"nodes": 1, "devices_per_node": 8},
                    "execution_timeout_seconds": 90,
                    "env_vars": {"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/set_env.sh"},
                }
                with (
                    patch("ci_core.test_broker.execute", side_effect=fake_execute),
                    patch("ci_core.test_broker.check_current", create=True),
                ):
                    res = client.request_run(
                        key="pr-smoke-key",
                        run=run,
                        item=item,
                        digest="d" * 64,
                        defaults={},
                    )
                self.assertEqual(res["status"], "FAIL")
                self.assertTrue(res["published_by_broker"])
                self.assertEqual(len(seen_archive_entries), 1)
                self.assertIn("PROTECTED_BASELINE_MARKER", seen_archive_entries[0])
                self.assertEqual(len(broker.client.comments), 1)
                self.assertIn(
                    "Lite-CI-V3 77:1:pr_smoke_1", broker.client.comments[0]["body"]
                )
            finally:
                broker.stop()

    def test_run_test_worker_script_once_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sink_calls = []

            def sink(wid, *, events, snapshot):
                sink_calls.append((wid, len(events), snapshot is not None))

            broker, sock, _ = self._make_broker(root, control_sink=sink)
            broker.start()
            try:
                ready = root / "worker-ready.json"
                state_dir = root / "isolated-state"
                pools_file = root / "isolated-state/pools-active.json"
                clean_env = {
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(root / "empty-home"),
                }
                (root / "empty-home").mkdir()
                proc = subprocess.run(
                    [
                        str(REPO_ROOT / "scripts/run_test_worker.sh"),
                        "--zone",
                        "yellow",
                        "--worker-id",
                        "yellow-test-id77a1",
                        "--state-dir",
                        str(state_dir),
                        "--broker-sock",
                        str(sock),
                        "--ready-file",
                        str(ready),
                        "--pools-file",
                        str(pools_file),
                        "--code-dir",
                        str(REPO_ROOT),
                        "--python",
                        sys.executable,
                        "--",
                        "--once",
                    ],
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertTrue(ready.exists())
                ready_doc = json.loads(ready.read_text())
                self.assertEqual(ready_doc["release_sha"], "a" * 40)
                self.assertTrue(sink_calls)
                self.assertTrue(sink_calls[0][2])
                sanitized_pools = json.loads(pools_file.read_text())["pools"]
                self.assertNotIn("ssh", sanitized_pools["a3_shared_pool"]["nodes"][0])
            finally:
                broker.stop()

    def test_request_run_attaches_live_pools_snapshot_over_empty_static_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker, sock, main_root = self._make_broker(root)
            static_empty = {
                "a3_shared_pool": {
                    "enabled": False,
                    "owner": "yellow",
                    "type": "ascend-a3",
                    "nodes": [],
                }
            }
            (main_root / "config/pools.json").write_text(
                json.dumps(static_empty), encoding="utf-8"
            )
            seen_cases = []

            def fake_execute(case, subrun, *, root=None):
                seen_cases.append(case)
                pools = execution_pools(root, case)
                validate_allocation(
                    case, pools, case["allocation"], include_paused=True
                )
                log = root / "run.log"
                log.write_text("CASE_RESULT smoke_8p PASS 0\n", encoding="utf-8")
                return Execution(0, ["OK"], log, True)

            broker.start()
            try:
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                run = {
                    "id": 78,
                    "run_attempt": 1,
                    "head_sha": "b" * 40,
                    "path": ".github/workflows/lite_actions_pr_smoke.yml",
                    "_ci_repo": SELF_REPO,
                    "pull_requests": [{"number": 77}],
                }
                item = {
                    "case_id": "yellow_case",
                    "test_id": "smoke_8p",
                    "worker": "yellow-test-id77a1",
                    "kind": "npu_smoke",
                    "target_pool": "a3_shared_pool",
                    "docker": "torchtitan_ci_a3_r2",
                    "resources": {"nodes": 1, "devices_per_node": 8},
                    "execution_timeout_seconds": 90,
                    "env_vars": {"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/set_env.sh"},
                }
                with (
                    patch("ci_core.test_broker.execute", side_effect=fake_execute),
                    patch("ci_core.test_broker.check_current", create=True),
                ):
                    res = client.request_run(
                        key="snapshot-key",
                        run=run,
                        item=item,
                        digest="e" * 64,
                        defaults={},
                    )
                self.assertEqual(res["status"], "PASS")
                self.assertTrue(res["published_by_broker"])
                self.assertEqual(
                    seen_cases[0]["pools_snapshot"]["a3_shared_pool"],
                    broker.pools["a3_shared_pool"],
                )
            finally:
                broker.stop()

    def test_publish_result_retries_transient_deferred_and_preserves_client_deferred(
        self,
    ):
        class FlakyBudgetClient:
            def __init__(self):
                self.comments = []
                self.send_attempts = 0
                self.progress_calls = 0

            def commit_comments(self, sha, **kwargs):
                return list(self.comments)

            def send_result(self, sha, body, *, category=None):
                if category == "ack":
                    self.progress_calls += 1
                    raise Deferred("YELLOW_BUDGET_EXHAUSTED")
                self.send_attempts += 1
                if self.send_attempts == 1:
                    raise Deferred("YELLOW_BUDGET_EXHAUSTED")
                self.comments.append(
                    {
                        "id": len(self.comments) + 1,
                        "sha": sha,
                        "body": body,
                        "category": category,
                    }
                )
                return self.comments[-1]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client_stub = FlakyBudgetClient()
            broker, sock, _ = self._make_broker(root, client=client_stub)
            broker.publish_retry_timeout = 1.0
            broker.publish_retry_interval = 0.01
            broker.workers["yellow"].pop("expected_hostname", None)
            broker.start()
            try:
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                run = {
                    "id": 79,
                    "run_attempt": 1,
                    "head_sha": "b" * 40,
                    "path": ".github/workflows/lite_actions_pr_smoke.yml",
                    "_ci_repo": SELF_REPO,
                    "pull_requests": [{"number": 77}],
                }
                item = {
                    "case_id": "probe_yellow",
                    "test_id": "probe",
                    "worker": "yellow-test-id77a1",
                    "kind": "worker_probe",
                    "target_pool": None,
                    "docker": None,
                    "resources": {"nodes": 0, "devices_per_node": 0},
                }
                fake_proc = subprocess.CompletedProcess(
                    args=["probe"], returncode=0, stdout="WORKER_OK\n", stderr=""
                )
                with (
                    patch("ci_core.test_broker.probe_worker", return_value=fake_proc),
                    patch("ci_core.test_broker.check_current", create=True),
                ):
                    res = client.request_run(
                        key="probe-deferred-key",
                        run=run,
                        item=item,
                        digest="f" * 64,
                        defaults={},
                    )
                self.assertEqual(res["status"], "PASS")
                self.assertTrue(res["published_by_broker"])
                self.assertEqual(client_stub.send_attempts, 2)
                self.assertEqual(client_stub.comments[0]["category"], "terminal_retry")

                with self.assertRaisesRegex(Deferred, "YELLOW_BUDGET_EXHAUSTED"):
                    client.post_progress(run, item, "f" * 64, "QUEUED")
                self.assertEqual(client_stub.progress_calls, 1)
            finally:
                broker.stop()

    def test_request_run_rotates_candidate_on_prelaunch_external_occupancy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker, sock, main_root = self._make_broker(root)
            tried_hosts = []

            def fake_execute(case, subrun, *, root=None):
                host_id = case["allocation"][0]["host_id"]
                tried_hosts.append(host_id)
                if host_id == "192.0.2.10:22":
                    raise ExternalNpuOccupancy("external job on .10")
                log = root / "run.log"
                log.write_text("CASE_RESULT smoke_8p PASS 0\n", encoding="utf-8")
                return Execution(0, ["ROTATED_OK"], log, True)

            broker.start()
            try:
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                run = {
                    "id": 80,
                    "run_attempt": 1,
                    "head_sha": "b" * 40,
                    "path": ".github/workflows/lite_actions_pr_smoke.yml",
                    "_ci_repo": SELF_REPO,
                    "pull_requests": [{"number": 77}],
                }
                item = {
                    "case_id": "rotate_case",
                    "test_id": "smoke_8p",
                    "worker": "yellow-test-id77a1",
                    "kind": "npu_smoke",
                    "target_pool": "a3_shared_pool",
                    "docker": "torchtitan_ci_a3_r2",
                    "resources": {"nodes": 1, "devices_per_node": 8},
                    "execution_timeout_seconds": 90,
                    "env_vars": {"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/set_env.sh"},
                }
                with (
                    patch("ci_core.test_broker.execute", side_effect=fake_execute),
                    patch("ci_core.test_broker.check_current", create=True),
                ):
                    res = client.request_run(
                        key="rotate-key",
                        run=run,
                        item=item,
                        digest="1" * 64,
                        defaults={},
                    )
                self.assertEqual(res["status"], "PASS")
                self.assertEqual(tried_hosts, ["192.0.2.10:22", "192.0.2.11:22"])
            finally:
                broker.stop()

    def test_request_run_respects_quarantine_enabled_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker, sock, main_root = self._make_broker(root)
            broker.quarantine_enabled = False
            broker.coordinator.quarantine_lease(
                allocation=[
                    {"host_id": "192.0.2.10:22", "devices": list(range(8))},
                    {"host_id": "192.0.2.11:22", "devices": list(range(8))},
                ],
                owner_worker="yellow-test-id77a1",
                task_key="prior-failed",
                reason="test_quarantine",
            )

            def fake_execute(case, subrun, *, root=None):
                log = root / "run.log"
                log.write_text("CASE_RESULT smoke_8p PASS 0\n", encoding="utf-8")
                return Execution(0, ["OK"], log, True)

            broker.start()
            try:
                client = BrokerClient(sock, worker_id="yellow-test-id77a1")
                run = {
                    "id": 82,
                    "run_attempt": 1,
                    "head_sha": "b" * 40,
                    "path": ".github/workflows/lite_actions_pr_smoke.yml",
                    "_ci_repo": SELF_REPO,
                    "pull_requests": [{"number": 77}],
                }
                item = {
                    "case_id": "q_case",
                    "test_id": "smoke_8p",
                    "worker": "yellow-test-id77a1",
                    "kind": "npu_smoke",
                    "target_pool": "a3_shared_pool",
                    "docker": "torchtitan_ci_a3_r2",
                    "resources": {"nodes": 1, "devices_per_node": 8},
                    "execution_timeout_seconds": 90,
                    "env_vars": {"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/set_env.sh"},
                }
                with (
                    patch("ci_core.test_broker.execute", side_effect=fake_execute),
                    patch("ci_core.test_broker.check_current", create=True),
                ):
                    res = client.request_run(
                        key="quarantine-disabled-key",
                        run=run,
                        item=item,
                        digest="2" * 64,
                        defaults={},
                    )
                self.assertEqual(res["status"], "PASS")
            finally:
                broker.stop()

    def test_request_run_preserves_allocation_through_broker_and_router_completion(
        self,
    ):
        import threading

        from ci_core.control_client import ControlClient
        from ci_core.egress_policy import Egress
        from ci_core.router import Router
        from ci_core.test_broker import BrokerControlAdapter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker_snapshots = []
            cfg = {
                "state_dir": str(root / "main_state"),
                "daily_upload_bytes": 1_000_000,
                "control_center": {"url": "https://control.example.test"},
            }
            ctrl = ControlClient(
                "yellow",
                cfg,
                "t" * 64,
                Egress("yellow", cfg),
                threading.Event(),
                lambda: {"records": {}, "devices": [], "queues": {}, "revision": 1},
            )
            sent_events = []

            def fake_send(route, payload=None):
                if route == "events":
                    sent_events.extend(payload.get("ev") or [])
                    return {"ok": True, "need_snapshot": False}
                if route == "snapshots":
                    return {"ok": True, "complete": True}
                return {"ok": True, "worker_id": (payload or {}).get("w")}

            def sink(wid, *, events, snapshot):
                broker_snapshots.append(snapshot)
                ctrl.forward_test_telemetry(wid, events=events, snapshot=snapshot)

            broker, sock, main_root = self._make_broker(root, control_sink=sink)
            run = {
                "id": 81,
                "run_attempt": 1,
                "name": "lite-ci-pr-smoke",
                "status": "in_progress",
                "event": "pull_request",
                "head_repository": {"full_name": SELF_REPO},
                "head_sha": "b" * 40,
                "path": ".github/workflows/lite_actions_pr_smoke.yml",
                "_ci_repo": SELF_REPO,
                "pull_requests": [{"number": 77}],
            }
            from types import SimpleNamespace

            from ci_core.contract import PR_SMOKE_WORKFLOW, create_plan, normalize

            defaults = {"worker": "", "target_pool": "", "docker": ""}
            item = normalize(
                SimpleNamespace(
                    test_name="smoke_8p",
                    test_descr="Yellow A3 8P smoke",
                    ci_kind="npu_smoke",
                    ci_overrides={
                        "worker": "yellow-test-id77a1",
                        "target_pool": "a3_shared_pool",
                        "docker": "torchtitan_ci_a3_r2",
                    },
                    nnodes=1,
                    ngpu=8,
                    timeout=90,
                    env_vars={"ASCEND_SET_ENV_PATH": "/usr/local/Ascend/set_env.sh"},
                ),
                defaults,
                0,
            )
            plan = create_plan(
                [item],
                run_id=81,
                attempt=1,
                sha="b" * 40,
                workflow=PR_SMOKE_WORKFLOW,
                defaults=defaults,
                repo=SELF_REPO,
            )
            broker.runs_provider = lambda: [run]
            test_state = root / "test_state"
            test_state.mkdir(parents=True, exist_ok=True)
            pools_file = root / "test_pools.json"
            import hashlib

            pools_json = json.dumps(
                broker.pools, ensure_ascii=False, separators=(",", ":")
            )
            pools_file.write_text(
                json.dumps(
                    {
                        "pools": broker.pools,
                        "config_json": pools_json,
                        "sha256": hashlib.sha256(pools_json.encode()).hexdigest(),
                        "workers": broker.workers,
                        "revision": 1,
                    }
                ),
                encoding="utf-8",
            )
            broker_client = BrokerClient(sock, worker_id="yellow-test-id77a1")
            adapter = None

            def fake_execute(case, subrun, *, root=None):
                if adapter is not None:
                    adapter.flush_snapshot(force=True)
                log = root / "run.log"
                log.write_text("CASE_RESULT smoke_8p PASS 0\n", encoding="utf-8")
                return Execution(0, ["PASS_OK"], log, True)

            broker.start()
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "LITE_ACTIONS_STATE_DIR": str(test_state),
                            "LITE_ACTIONS_ACTIVE_POOLS_PATH": str(pools_file),
                        },
                    ),
                    patch.object(ctrl, "_send", side_effect=fake_send),
                    patch("ci_core.test_broker.execute", side_effect=fake_execute),
                    patch("ci_core.test_broker.check_current", create=True),
                    patch.object(
                        broker.client,
                        "ci_request",
                        return_value=plan,
                        create=True,
                    ),
                ):
                    router = Router(
                        main_root,
                        "yellow-test-id77a1",
                        broker_client,
                        "depeng1994",
                        deployment_config={
                            "state_dir": str(test_state),
                            "address": "local",
                            "case_environments": broker.workers["yellow"][
                                "case_environments"
                            ],
                        },
                        broker_client=broker_client,
                    )
                    adapter = BrokerControlAdapter(
                        router.broker, threading.Event(), router.control_view
                    )
                    router.control = adapter
                    ctrl.register_test_worker(
                        {
                            "worker_id": "yellow-test-id77a1",
                            "repo": SELF_REPO,
                            "run_id": 81,
                            "attempt": 1,
                        }
                    )
                    router.tick()
                    router.training.shutdown(wait=True)
                    router.pump()
                    adapter.flush_snapshot(force=True)
            finally:
                broker.stop()

            records = router.state.read()
            task_key = f"{SELF_REPO}:81:1:{item['case_id']}"
            rec = records[task_key]
            expected_alloc = [
                {"host_id": "192.0.2.10:22", "devices": [0, 1, 2, 3, 4, 5, 6, 7]}
            ]
            self.assertEqual(rec["phase"], "REPORTED")
            self.assertEqual(rec["allocation"], expected_alloc)
            self.assertIn("FINISHED", rec["phase_timestamps"])
            # Broker never pushed a RUNNING snapshot with empty allocation on finish.
            for snap in broker_snapshots:
                snap_rec = (snap or {}).get("records", {}).get(task_key)
                if snap_rec and snap_rec.get("phase") in {
                    "RUNNING",
                    "CLEANING",
                    "REPORTED",
                }:
                    self.assertEqual(snap_rec.get("allocation"), expected_alloc)
            # Even if a snapshot omits allocation after completion, broker merges terminal allocation.
            merged = broker._merge_active_allocations(
                {"records": {task_key: {"phase": "REPORTED", "allocation": []}}}
            )
            self.assertEqual(merged["records"][task_key]["allocation"], expected_alloc)
            # Telemetry sent TL with real devices and TF, and never emitted TL with a=[].
            tl_events = [ev for ev in sent_events if ev.get("t") == "TL"]
            tf_events = [ev for ev in sent_events if ev.get("t") == "TF"]
            self.assertTrue(tl_events)
            self.assertTrue(all(ev.get("a") for ev in tl_events))
            self.assertEqual(len(tf_events), 1)
            self.assertEqual(tf_events[0]["st"], "PASS")


if __name__ == "__main__":
    unittest.main()
