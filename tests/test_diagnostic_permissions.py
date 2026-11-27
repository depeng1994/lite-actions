"""Regressions for diagnostics hidden by private files or parent directories."""

import contextlib
import io
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import unified_router
from auto_update.update_log import record
from ci_core.control_client import write_json
from ci_core.egress_policy import Egress
from ci_core.executor import execute
from ci_core.idle_probe import IdleProbe
from ci_core.job_state import JobState
from ci_core.router import Router
from ci_core.test_supervisor import TestSupervisor
from ci_core.worker_activity import activity_lock
from ci_core.worker_ready import publish_ready
from tests.scenarios import Service, definition, write_workspace


@unittest.skipUnless(
    os.name == "posix" and Path("/proc/self/stat").exists(),
    "Linux file permissions required",
)
class DiagnosticPermissionsTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        temporary = tempfile.TemporaryDirectory(prefix="lite-diagnostics-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o755)
        self.writers = (
            ("worker-ready.json", self.write_ready),
            ("control-outbox.json", self.write_control),
        )

    def write_ready(self, path):
        return publish_ready(self.root, path, release_sha="a" * 40)

    @staticmethod
    def write_control(path):
        value = {"events": [{"phase": "QUEUED"}], "generation": 1}
        write_json(path, value)
        return value

    def assert_readable(self, *paths):
        # Keep independent numeric expectations: changing the policy must fail tests.
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, str(path))
        if os.geteuid() == 0:
            nobody = pwd.getpwnam("nobody")
            subprocess.run(
                [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    "import os,sys; from pathlib import Path; "
                    "[(Path(p).read_bytes(), "
                    "not os.access(p, os.W_OK) or sys.exit(1)) for p in sys.argv[1:]]",
                    *map(str, paths),
                ],
                user=nobody.pw_uid,
                group=nobody.pw_gid,
                extra_groups=[],
                check=True,
                capture_output=True,
                timeout=10,
            )

    def test_both_worker_entrypoints_select_the_diagnostic_umask(self):
        for zone in ("blue", "yellow"):
            with (
                self.subTest(zone=zone),
                patch.dict(os.environ, {"LITE_ACTIONS_BROKER_SOCK": ""}),
                patch.object(
                    sys, "argv", ["unified_router", "--worker-id", zone, "--validate"]
                ),
                patch(
                    "unified_router.deployment",
                    return_value=(zone, {"state_dir": str(self.root / zone)}),
                ),
                patch("unified_router.load_active_pools"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                os.umask(0o077)
                unified_router.main()
                self.assertEqual(os.umask(0o077), 0o022)

    def test_new_diagnostic_state_is_readable(self):
        for zone in ("blue", "yellow"):
            for filename, writer in self.writers:
                with self.subTest(zone=zone, filename=filename):
                    path = self.root / zone / filename.removesuffix(".json") / filename
                    expected = writer(path)
                    self.assert_readable(path)
                    self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o755)
                    self.assertEqual(json.loads(path.read_text()), expected)

    def test_private_umask_and_replacement_do_not_hide_state(self):
        os.umask(0o077)
        for zone in ("blue", "yellow"):
            for filename, writer in self.writers:
                for existing in (False, True):
                    with self.subTest(zone=zone, filename=filename, existing=existing):
                        path = self.root / f"{zone}-{filename}.{existing}"
                        if existing:
                            path.write_text("{}")
                            path.chmod(0o600)
                        expected = writer(path)
                        self.assert_readable(path)
                        self.assertEqual(json.loads(path.read_text()), expected)

    def test_both_zones_publish_readable_state_and_management_logs(self):
        for zone in ("blue", "yellow"):
            with self.subTest(zone=zone):
                state = self.root / zone
                ready = state / "runtime" / "worker-ready.json"
                with activity_lock(ready, create=True):
                    self.write_ready(ready)
                self.assertEqual(stat.S_IMODE(ready.parent.stat().st_mode), 0o755)
                self.write_control(state / "control-outbox.json")
                policy = Egress(zone, {"state_dir": str(state)})
                policy.record_rejection("github", "test", 0)
                probe = IdleProbe(
                    zone,
                    lambda: ({}, {}),
                    state_dir=state,
                    lease_provider=lambda: set(),
                )
                probe.cycle()
                with patch.dict(os.environ, {"LITE_ACTIONS_STATE_DIR": str(state)}):
                    jobs = JobState(self.root)
                    jobs.update("diagnostic", phase="REPORTED")
                supervisor = TestSupervisor.__new__(TestSupervisor)
                supervisor.state_dir = state
                supervisor.ledger_path = state / "test-workers.json"
                supervisor._write_ledger({"workers": {}})
                updates = state / "management" / "updates.jsonl"
                previous = os.umask(0o077)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        record(updates, "NO_CHANGE")
                finally:
                    os.umask(previous)
                self.assertEqual(stat.S_IMODE(updates.parent.stat().st_mode), 0o755)
                self.assert_readable(
                    ready,
                    state / "control-outbox.json",
                    policy.path,
                    probe.state_path,
                    jobs.path,
                    supervisor.ledger_path,
                    updates,
                )

    def test_cancelled_cases_leave_readable_logs_and_runtime_in_both_zones(self):
        workspace = self.root / "workspace"
        write_workspace(workspace)
        service = Service([definition("smoke", kind="npu_smoke")], workspace)
        case = {
            "enabled": True,
            "pool": "pool_blue",
            "kind": "npu_smoke",
            "docker": "training",
            "resources": {"nodes": 1, "devices_per_node": 2},
            "allocation": [{"host_id": "192.0.2.10:22", "devices": [0, 1]}],
            "execution_timeout_seconds": 90,
            "verified_env": {"ASCEND_SET_ENV_PATH": "/cann"},
        }
        for zone in ("blue", "yellow"):
            for stage in ("before", "after-runtime"):
                with self.subTest(zone=zone, stage=stage):
                    router = Router.__new__(Router)
                    router.deployment = {"state_dir": str(self.root / zone)}
                    folder = router.case_folder(stage)
                    folder.chmod(0o700)
                    self.assertEqual(router.case_folder(stage), folder)
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)
                    cancel = threading.Event()
                    if stage == "before":
                        cancel.set()
                    run = {
                        **service.runs[20],
                        "_ci_test_id": "smoke",
                        "_ci_zone": zone,
                        "_ci_cancel_event": cancel,
                        "_ci_pipeline_folder": folder,
                        "_ci_runtime_callback": lambda value: cancel.set(),
                    }
                    with (
                        patch("ci_core.executor.remote_preflight") as preflight,
                        patch("ci_core.executor.subprocess.Popen") as launch,
                    ):
                        result = execute(case, run, root=workspace)
                    preflight.assert_not_called()
                    launch.assert_not_called()
                    self.assertEqual(result.exit_code, 130)
                    self.assertTrue(result.release_verified)
                    self.assert_readable(folder / "pipeline.log")
                    if stage == "after-runtime":
                        self.assert_readable(folder / "runtime-case.json")
