"""Artifact-to-result lifecycle scenarios; GitHub and model execution are external fixtures."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.executor import ExternalNpuOccupancy
from ci_core.logs import select_lines
from ci_core.resources import ResourceBusy, reserve
from ci_core.router import Router, task_key
from tests.scenarios import Service, completed_execution, definition, write_workspace


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_workspace(self.root)
        self.routers = []
        self.env = patch.dict(
            "os.environ",
            LITE_ACTIONS_ALLOWED_ACTORS="maintainer",
            LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "leases"),
            LITE_ACTIONS_STATE_DIR="",
        )
        self.env.start()

    def tearDown(self):
        for router in self.routers:
            router.close()
        self.env.stop()
        self.temp.cleanup()

    def router(self, service, worker="blue", *, existing=None):
        root = existing or self.root / worker
        if not existing:
            write_workspace(root)
        router = Router(root, worker, service, "reporter")
        self.routers.append(router)
        return router

    def drain(self, router):
        for _ in range(20):
            for future, _, _ in list(router.active.values()):
                future.result(10)
            router.pump()
            router.flush()
            if not router.active:
                return
        self.fail("workflow did not settle")

    def test_artifact_routes_to_independent_workers_and_reports_once(self):
        service = Service(
            [
                definition("blue_os", kind="worker_probe"),
                definition("yellow_os", kind="worker_probe", worker="yellow"),
            ],
            self.root,
        )
        blue = self.router(service)
        yellow = self.router(service, "yellow")
        with service.connected():
            for router in [blue, yellow]:
                router.tick()
                self.drain(router)
                router.tick()
        self.assertEqual(
            {(x["test_id"], x["worker"], x["status"]) for x in service.results()},
            {("blue_os", "blue", "PASS"), ("yellow_os", "yellow", "PASS")},
        )
        self.assertEqual(len(service.results()), 2)

    def test_parallel_cases_hold_disjoint_devices_and_ports_until_report(self):
        service = Service([definition("first"), definition("second")], self.root)
        router = self.router(service)
        router.pools["pool_blue"]["nodes"] = router.pools["pool_blue"]["nodes"][:1]
        entered = threading.Barrier(3)
        release = threading.Event()
        cases = []

        def execute(case, run, **kwargs):
            cases.append(case)
            entered.wait(10)
            release.wait(10)
            return completed_execution(router.root, case, run)

        try:
            with (
                service.connected(),
                patch("ci_core.router.execute", side_effect=execute),
            ):
                router.tick()
                entered.wait(10)
                self.assertTrue(
                    set(cases[0]["allocation"][0]["devices"]).isdisjoint(
                        cases[1]["allocation"][0]["devices"]
                    )
                )
                self.assertNotEqual(cases[0]["master_port"], cases[1]["master_port"])
                for case in cases:
                    with self.assertRaises(ResourceBusy):
                        with reserve(self.root / "leases/devices", case["allocation"]):
                            pass
                release.set()
                self.drain(router)
        finally:
            release.set()
        self.assertEqual([x["status"] for x in service.results()], ["PASS", "PASS"])
        for case in cases:
            with reserve(self.root / "leases/devices", case["allocation"]):
                pass

    def test_blocked_distributed_head_waits_without_partial_lease_or_backfill(self):
        service = Service(
            [definition("distributed", nodes=2), definition("single")], self.root
        )
        router = self.router(service)
        busy = [dict(host_id="192.0.2.11:22", devices=[0, 1])]
        with service.connected(), patch("ci_core.router.execute") as execute:
            with reserve(self.root / "leases/devices", busy):
                router.tick()
                self.assertFalse(router.active)
                execute.assert_not_called()
                with reserve(
                    self.root / "leases/devices",
                    [dict(host_id="192.0.2.10:22", devices=[0, 1])],
                ):
                    pass
            execute.side_effect = lambda case, run, **kw: completed_execution(
                router.root, case, run
            )
            router.tick()
            self.drain(router)
        self.assertEqual(
            {x["test_id"]: x["status"] for x in service.results()},
            {"distributed": "PASS", "single": "PASS"},
        )

    def test_unready_training_node_still_accepts_smoke_work(self):
        service = Service(
            [
                definition("training"),
                definition("communication", kind="npu_smoke", nodes=2),
            ],
            self.root,
        )
        router = self.router(service)
        router.pools["pool_blue"]["nodes"][1]["capabilities"] = ["npu_smoke"]
        seen = {}

        def execute(case, run, **kwargs):
            seen[run["_ci_test_id"]] = case["allocation"]
            return completed_execution(router.root, case, run)

        with service.connected(), patch("ci_core.router.execute", side_effect=execute):
            router.tick()
            self.drain(router)
        self.assertEqual({n["host_id"] for n in seen["training"]}, {"192.0.2.10:22"})
        self.assertEqual(len(seen["communication"]), 2)
        self.assertTrue(all(x["status"] == "PASS" for x in service.results()))


    def test_queued_run_with_artifact_is_admitted_once(self):
        service = Service([definition("training")], self.root)
        service.runs[20]["status"] = "queued"
        router = self.router(service)
        with service.connected(), patch("ci_core.router.execute", side_effect=lambda case, run, **kw: completed_execution(router.root, case, run)) as execution:
            router.tick()
            router.tick()
            self.drain(router)
        self.assertEqual(execution.call_count, 1)
        self.assertEqual(service.results()[0]["status"], "PASS")

    def test_queued_run_without_artifact_retries_next_poll(self):
        service = Service([definition("training")], self.root)
        service.runs[20]["status"] = "queued"
        router = self.router(service)
        original = service.ci_request
        with patch.object(service, "ci_request", side_effect=[None, original(service.runs[20])]):
            router.receive(service.running_workflows())
            self.assertFalse(router.state.read())
            router.receive(service.running_workflows())
            self.assertEqual(len(router.state.read()), 1)

    def test_running_workflows_deduplicates_queued_and_in_progress(self):
        service = Service([definition("training")], self.root)
        run = dict(service.runs[20])
        with patch.object(service, "request", side_effect=lambda method, path, data=None: {"workflow_runs": [dict(run, status="queued")] if "status=queued" in path else [dict(run, status="in_progress")] if "status=in_progress" in path else []}):
            runs = service.running_workflows()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "in_progress")

    def test_busy_external_devices_retry_without_quarantine_or_false_failure(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        calls = []

        def execute(case, run, **kwargs):
            calls.append(case["allocation"])
            if len(calls) == 1:
                raise ExternalNpuOccupancy("fixture external task")
            return completed_execution(router.root, case, run)

        with service.connected(), patch("ci_core.router.execute", side_effect=execute):
            router.tick()
            self.drain(router)
            self.drain(router)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(service.results()[0]["status"], "PASS")
        self.assertFalse(
            any(x.get("quarantined") for x in router.state.read().values())
        )

    def test_new_attempt_cancels_waiting_attempt_before_launch(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        with service.connected(), patch("ci_core.router.execute") as execute:
            router.receive(service.running_workflows())
            service.add_run(20, attempt=2)
            execute.side_effect = lambda case, run, **kw: completed_execution(
                router.root, case, run
            )
            router.tick()
            self.drain(router)
        self.assertEqual(
            {x["attempt"]: x["status"] for x in service.results()},
            {1: "CANCELLED", 2: "PASS"},
        )
        self.assertEqual(execute.call_count, 1)

    def test_active_cancel_waits_for_cleanup_before_result(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        entered = threading.Event()

        def execute(case, run, **kwargs):
            entered.set()
            self.assertTrue(run["_ci_cancel_event"].wait(10))
            return completed_execution(router.root, case, run, rc=130)

        with service.connected(), patch("ci_core.router.execute", side_effect=execute):
            router.tick()
            self.assertTrue(entered.wait(10))
            service.runs[20]["status"] = "completed"
            router.tick()
            self.drain(router)
        self.assertEqual(service.results()[0]["status"], "CANCELLED")
        self.assertTrue(service.results()[0]["cleanup_verified"])

    def test_report_outage_recovers_after_restart_without_reexecution(self):
        with patch.dict(
            "os.environ", LITE_ACTIONS_STATE_DIR=str(self.root / "durable-state")
        ):
            service = Service([definition("training")], self.root)
            router = self.router(service)

            def execute(case, run, **kwargs):
                service.offline = True
                return completed_execution(router.root, case, run)

            with (
                service.connected(),
                patch("ci_core.router.execute", side_effect=execute) as execution,
            ):
                router.tick()
                self.drain(router)
                self.assertEqual(
                    next(iter(router.state.read().values()))["phase"], "PENDING_REPORT"
                )
                service.offline = False
                service.runs[20]["status"] = "completed"
                release = self.root / "replacement-checkout"
                write_workspace(release)
                recovered = self.router(service, existing=release)
                recovered.tick()
                recovered.tick()
            self.assertEqual(execution.call_count, 1)
            self.assertEqual(len(service.results()), 1)

    def test_admin_transaction_preserves_task_and_quarantine_during_workflow(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        ready, release = self.root / "admin-ready", self.root / "admin-release"
        script = """import sys,time
from pathlib import Path
from ci_core.job_state import JobState
class AdminState(JobState):
    def read(self):
        prior=super().read()
        Path(sys.argv[2]).touch()
        deadline=time.monotonic()+10
        while not Path(sys.argv[3]).exists():
            if time.monotonic()>deadline: raise TimeoutError('admin transaction')
            time.sleep(.01)
        return prior
AdminState(Path(sys.argv[1])).update('operator_guard',phase='REPORTED',
    quarantined=True,allocation=[{'host_id':'192.0.2.10:22','devices':[0,1]}])
"""
        admin = subprocess.Popen(
            [sys.executable, "-c", script, str(router.root), str(ready), str(release)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        executed = threading.Event()
        errors = []

        def execute(case, run, **kwargs):
            executed.set()
            return completed_execution(router.root, case, run)

        def tick():
            try:
                router.tick()
            except Exception as exc:
                errors.append(exc)

        poller = threading.Thread(target=tick)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() > deadline:
                    self.fail("admin did not reach its state transaction")
                time.sleep(0.01)
            with (
                service.connected(),
                patch("ci_core.router.execute", side_effect=execute),
            ):
                poller.start()
                executed.wait(0.5)
                release.touch()
                poller.join(10)
                self.assertFalse(poller.is_alive())
                self.assertEqual(errors, [])
                self.drain(router)
            _, stderr = admin.communicate(timeout=5)
            self.assertEqual(admin.returncode, 0, stderr)
            records = router.state.read()
            key = task_key(service.runs[20], service.plans[20]["cases"][0])
            self.assertEqual(records[key]["phase"], "REPORTED")
            self.assertTrue(records["operator_guard"]["quarantined"])
            self.assertEqual([r["status"] for r in service.results()], ["PASS"])
        finally:
            release.touch()
            if poller.ident:
                poller.join(10)
            if admin.poll() is None:
                admin.kill()
            admin.communicate()

    def test_crash_recovery_quarantines_allocation_and_prevents_relaunch(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        with service.connected(), patch("ci_core.router.execute") as execute:
            router.receive(service.running_workflows())
            key = next(iter(router.state.read()))
            router.state.update(
                key,
                phase="RUNNING",
                allocation=[
                    dict(host_id=n["host_id"], devices=n["device_ids"])
                    for n in router.pools["pool_blue"]["nodes"]
                ],
            )
            recovered = self.router(service, existing=router.root)
            recovered.tick()
            service.add_run(21)
            recovered.tick()
            execute.assert_not_called()
        self.assertEqual(service.results()[0]["status"], "INFRA_ERROR")
        self.assertFalse(service.results()[0]["cleanup_verified"])
        self.assertTrue(recovered.state.read()[key]["quarantined"])
        self.assertEqual(
            recovered.state.read()[
                task_key(service.runs[21], service.plans[21]["cases"][0])
            ]["phase"],
            "QUEUED",
        )

    def test_noisy_training_result_keeps_identity_and_upload_budget(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        with (
            service.connected(),
            patch(
                "ci_core.router.execute",
                side_effect=lambda case, run, **kw: completed_execution(
                    router.root,
                    case,
                    run,
                    lines=select_lines(
                        "\n".join(
                            "[rank0] "
                            + "中文\t\x1b[31m" * 100
                            + f" step: {i} loss: 1.0 tps: 2.0"
                            for i in range(100)
                        ),
                        0,
                        require_loss=True,
                    ),
                ),
            ),
        ):
            router.tick()
            self.drain(router)
        body = next(
            c["body"] for c in service.comments if c["body"].startswith("Lite-CI-V3 ")
        )
        result = service.results()[0]
        self.assertEqual(
            (result["run_id"], result["attempt"], result["sha"], result["plan_digest"]),
            (20, 1, service.runs[20]["head_sha"], service.plans[20]["plan_digest"]),
        )
        self.assertLessEqual(len(result["last_20_lines"]), 20)
        self.assertTrue(result["last_20_lines"])
        self.assertTrue(all("loss:" in line for line in result["last_20_lines"]))
        self.assertTrue(
            all(
                len(line) <= 180 and "\t" not in line
                for line in result["last_20_lines"]
            )
        )
        self.assertLessEqual(len(body.encode()), 6100)
        self.assertLessEqual(
            len(json.dumps({"body": body}, separators=(",", ":")).encode()), 7000
        )

    def test_failed_training_reports_root_error_within_log_budget(self):
        service = Service([definition("training")], self.root)
        router = self.router(service)
        diagnostic = (
            "[rank0] ModuleNotFoundError: missing_training_dependency\n"
            + "shutdown noise\n" * 50
            + "RuntimeError: 1 integration test(s) failed:\n"
        )
        with (
            service.connected(),
            patch(
                "ci_core.router.execute",
                side_effect=lambda case, run, **kw: completed_execution(
                    router.root, case, run, rc=1, lines=select_lines(diagnostic, 1)
                ),
            ),
        ):
            router.tick()
            self.drain(router)
        result = service.results()[0]
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(
            any(
                "ModuleNotFoundError: missing_training_dependency" in line
                for line in result["last_20_lines"]
            )
        )
        self.assertLessEqual(len(result["last_20_lines"]), 20)
