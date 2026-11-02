"""Real Router + SQLite + Artifact/Checks fixtures; no hardware/platform claims."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.executor import Execution, ExternalNpuOccupancy
from ci_core.gitcode_admission import GitCodeAdmissionBroker
from ci_core.gitcode_runtime import GitCodeRouterAdapter
from ci_core.gitcode_state import STATE_VERSION, Authority, Coordinator, EventProof
from ci_core.gitcode_store import GitCodeRequestStore
from ci_core.gitcode_waiter import GitCodeWaiter
from ci_core.resources import ResourceBusy, reserve
from ci_core.router import Router
from tests.test_gitcode_pipeline import HostingAPI


class GitCodeRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            "os.environ",
            LITE_ACTIONS_STATE_DIR="",
            LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "locks"),
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        clock = patch("ci_core.router.time", SimpleNamespace(time=lambda: 1100))
        clock.start()
        self.addCleanup(clock.stop)
        local_probe = patch(
            "ci_core.gitcode_runtime.probe",
            return_value=SimpleNamespace(
                returncode=0, stdout="WORKER_HOSTNAME=unit-test\n", stderr=""
            ),
        )
        local_probe.start()
        self.addCleanup(local_probe.stop)
        forbidden = patch(
            "ci_core.router.model_archive",
            side_effect=AssertionError("schema 4 must not fetch GitHub source"),
        )
        forbidden.start()
        self.addCleanup(forbidden.stop)
        discovery = patch(
            "ci_core.router.verify_definition",
            side_effect=AssertionError("schema 4 must not discover PR Python"),
        )
        discovery.start()
        self.addCleanup(discovery.stop)
        config = self.root / "config"
        config.mkdir()
        config.joinpath("workers.json").write_text(
            json.dumps({"blue": {"enabled": True}})
        )
        nodes = []
        for index in (10, 11):
            nodes.append(
                {
                    "host_id": f"192.0.2.{index}:22",
                    "ssh": f"192.0.2.{index}:22",
                    "device_ids": list(range(16)),
                    "node_ip": f"192.0.2.{index}",
                    "allocation_groups": [
                        {"devices": list(range(start, start + 8)), "verified": True}
                        for start in (0, 8)
                    ],
                    "environments": {
                        "cann_master_torchtitan_master_xyj": {
                            "container_id": "a" * 64,
                            "image_id": "sha256:" + "b" * 64,
                            "image_ref": "trusted:test",
                        }
                    },
                }
            )
        pool = {
            "enabled": True,
            "owner": "blue",
            "type": "ascend-a3",
            "nodes": nodes,
            "max_jobs_per_host": 2,
            "verified_multinode_allocations": [
                [{"host_id": n["host_id"], "devices": list(range(8))} for n in nodes]
            ],
        }
        config.joinpath("pools.json").write_text(json.dumps({"a3_shared_blue": pool}))
        api_root = self.root / "hosting"
        api_root.mkdir()
        self.api = HostingAPI(api_root)
        self.plan = self.api.produce()
        self.api.job.update(status="completed", conclusion="success")
        self.api.running_workflows = lambda: [self.api.run]
        self.api.workflow_status = lambda ident: copy.deepcopy(self.api.run)
        self.api.ci_request = Mock(
            side_effect=AssertionError("schema-3 request parser used")
        )
        self.api.send_result = Mock(side_effect=AssertionError("comment fallback used"))
        original_request = self.api.request
        self.cancel_calls = []

        def request(method, path, data=None):
            if method == "POST" and path == "/actions/runs/100/cancel":
                self.cancel_calls.append(path)
                return {}
            return original_request(method, path, data)

        self.api.request = request
        self.coordinator = Coordinator(self.root / "schema4.sqlite3")
        self.authority = Authority(copy.deepcopy(self.plan["pr"]), 1)
        self.broker = self.new_broker()
        self.executed = []
        self.execute = self.execute_ok
        self.routers = []
        self.addCleanup(self.close_routers)

    def new_broker(self):
        return GitCodeAdmissionBroker(
            reader=self.api.reader,
            coordinator=self.coordinator,
            store=GitCodeRequestStore(self.root / "retained"),
            workspace=self.root / "admissions",
            read_authority=lambda repo, number: self.authority,
            verify_event=lambda intent: EventProof("event-1", "snapshot-1", 1, 1),
            clock=lambda: 1100,
        )

    def make_router(self):
        adapter = GitCodeRouterAdapter(
            [self.new_broker()],
            executor=lambda **kwargs: self.execute(**kwargs),
            clock=lambda: 1100,
        )
        router = Router(
            self.root, "blue", self.api, "reporter", gitcode_adapter=adapter
        )
        self.routers.append(router)
        return router

    def close_routers(self):
        for router in self.routers:
            router.close()

    def execute_ok(self, **kwargs):
        case, item = kwargs["case"], kwargs["item"]
        self.executed.append(item["test_id"])
        with self.assertRaises(ResourceBusy):
            with reserve(self.root / "locks/devices", case["allocation"]):
                pass
        captured = kwargs["capture"](
            {
                "lease_id": "lease-" + item["case_id"],
                "cleanup": {
                    "nodes": [
                        {
                            "host_id": node["host_id"],
                            "container_id": "a" * 64,
                            "image_id": "sha256:" + "b" * 64,
                            "task_marker": "marker-" + item["case_id"],
                            "device_ids": node["devices"],
                        }
                        for node in case["allocation"]
                    ]
                },
            }
        )
        self.assertGreater(captured["fencing_token"], 0)
        kwargs["before_launch"]()
        kwargs["phase"]("RUNNING")
        return Execution(
            0, ["CPU fixture, not NPU evidence"], self.root / "fixture.log", True
        )

    def drain(self, router):
        for _ in range(30):
            for future, _, _ in list(router.active.values()):
                future.result(timeout=15)
            router.pump()
            router.gitcode.flush()
            pending = [
                r
                for r in router.state.read().values()
                if r.get("phase") not in {"REPORTED"}
            ]
            if not router.active and not pending:
                return
        self.fail(
            [(r.get("phase"), r.get("report")) for r in router.state.read().values()]
        )

    def test_real_router_executes_full_plan_using_shared_lease_and_fixed_slots(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        self.assertCountEqual(
            self.executed, ["dispatch_smoke_8p", "dispatch_smoke_16p"]
        )
        self.assertEqual(len(router.state.read()), 3)
        self.assertTrue(
            all(r["report"]["status"] == "PASS" for r in router.state.read().values())
        )
        self.api.ci_request.assert_not_called()
        self.api.send_result.assert_not_called()
        stored = router.gitcode.resolve_request("1210038933/100/1")
        self.assertEqual(
            GitCodeWaiter(stored, self.api.channel, current=lambda plan: True).poll(
                now=1100
            )["test_result"],
            "PASS",
        )
        router.tick()
        self.drain(router)
        self.assertEqual(len(self.executed), 2)

    def test_failed_prepare_never_queues_or_takes_a_device_lease(self):
        self.api.job.update(conclusion="failure")
        router = self.make_router()
        router.tick()
        self.assertEqual(router.state.read(), {})
        self.assertEqual(self.executed, [])
        self.assertFalse((self.root / "locks/devices").exists())

    def test_old_pending_request_cancels_after_base_change_without_execution(self):
        router = self.make_router()
        router.receive([self.api.run])
        self.authority = Authority({**self.plan["pr"], "base_sha": "9" * 40}, 2)
        router.gitcode.maintain()
        self.drain(router)
        self.assertEqual(self.executed, [])
        self.assertTrue(
            all(
                r["report"]["status"] == "CANCELLED"
                for r in router.state.read().values()
            )
        )

    def test_generation_change_between_capture_and_launch_never_runs_payload(self):
        def race(**kwargs):
            self.authority = Authority({**self.plan["pr"], "head_sha": "9" * 40}, 2)
            return self.execute_ok(**kwargs)

        self.execute = race
        router = self.make_router()
        router.tick()
        self.drain(router)
        npu = [
            r
            for r in router.state.read().values()
            if r["item"]["kind"] != "worker_probe"
        ]
        self.assertTrue(all(r["report"]["status"] != "PASS" for r in npu))

    def test_cleanup_uncertainty_is_quarantined_and_never_green(self):
        def uncertain(**kwargs):
            result = self.execute_ok(**kwargs)
            result.release_verified = False
            return result

        self.execute = uncertain
        router = self.make_router()
        router.tick()
        # Both NPU cases may not be admissible after the first quarantine.
        for future, _, _ in list(router.active.values()):
            future.result(timeout=15)
        router.pump()
        records = [r for r in router.state.read().values() if r.get("quarantined")]
        self.assertTrue(records)
        self.assertTrue(all(r["report"]["status"] != "PASS" for r in records))
        for key, record in router.state.read().items():
            if record["phase"] == "QUEUED":
                router.finish(key, "CANCELLED", 130, ["end CPU test"])

    def test_restart_after_results_and_artifact_deletion_does_not_reexecute(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        router.close()
        self.api.zips.clear()
        self.api.metadata.clear()
        resumed = self.make_router()
        resumed.gitcode.flush()
        self.assertEqual(len(self.executed), 2)
        self.assertTrue(
            all(r["phase"] == "REPORTED" for r in resumed.state.read().values())
        )

    def test_no_adapter_cannot_start_with_persisted_schema4_records(self):
        router = self.make_router()
        router.receive([self.api.run])
        with self.assertRaisesRegex(ValueError, "compatible runtime"):
            Router(self.root, "blue", self.api, "reporter")
        for key in router.state.read():
            router.finish(key, "CANCELLED", 130, ["end CPU test"])

    def test_old_fence_cannot_acknowledge_another_lease(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        lease = self.coordinator.execution_snapshots()[0]["leases"][0]
        self.assertFalse(
            router.gitcode.cleanup_ack(
                {
                    "execution_id": lease["execution_id"],
                    "case_id": lease["case_id"],
                    "lease_id": lease["lease_id"],
                    "fencing_token": lease["id"] + 1,
                    "cleanup_verified": True,
                }
            )
        )

    def test_external_occupancy_before_capture_can_retry_but_never_replay_a_lease(self):
        def occupied(**kwargs):
            raise ExternalNpuOccupancy("foreign workload is not killed")

        self.execute = occupied
        router = self.make_router()
        router.tick()
        for future, _, _ in list(router.active.values()):
            future.result(timeout=15)
        self.assertFalse(self.coordinator.recoverable_leases())
        self.execute = self.execute_ok
        for key, record in router.state.read().items():
            if record["phase"] == "QUEUED":
                router.state.update(key, unavailable=[])
        self.drain(router)

    def test_interrupted_captured_execution_is_quarantined_never_replayed(self):
        router = self.make_router()
        router.receive([self.api.run])
        key, record = next(
            (k, r)
            for k, r in router.state.read().items()
            if r["item"]["kind"] != "worker_probe"
        )
        eid = record["gitcode"]["execution_id"]
        cid = record["item"]["case_id"]
        self.coordinator.claim_case(eid, cid, now=1100)
        binding = {
            "nodes": [
                {
                    "host_id": "192.0.2.10:22",
                    "container_id": "a" * 64,
                    "image_id": "sha256:" + "b" * 64,
                    "task_marker": "old-marker",
                    "device_ids": list(range(8)),
                }
            ]
        }
        token = self.coordinator.bind_lease(
            eid, cid, "captured-before-crash", binding, now=1100
        )
        router.state.update(
            key,
            phase="RUNNING",
            allocation=[{"host_id": "192.0.2.10:22", "devices": list(range(8))}],
            runtime={
                "lease_id": "captured-before-crash",
                "fencing_token": token,
                "cleanup": binding,
            },
        )
        router.close()
        resumed = self.make_router()
        retained = resumed.state.read()[key]
        self.assertTrue(retained["quarantined"])
        self.assertFalse(retained["report"]["cleanup_verified"])
        self.assertEqual(retained["report"]["status"], "INFRA_ERROR")
        self.assertEqual(
            self.coordinator.recoverable_leases()[0]["state"], "QUARANTINED"
        )
        self.assertEqual(self.executed, [])

    def test_cancelled_github_run_cannot_publish_new_aggregate_success(self):
        router = self.make_router()
        with patch.object(
            router.gitcode.delivery,
            "publish_complete",
            side_effect=TimeoutError("result API unavailable"),
        ):
            router.tick()
            for _ in range(10):
                for future, _, _ in list(router.active.values()):
                    future.result(timeout=15)
                router.pump()
                if not router.active:
                    break
        self.assertEqual(len(self.executed), 2)
        self.api.run.update(status="completed", conclusion="cancelled")
        router.gitcode.flush()
        envelope = self.api.channel.read_receipt(self.plan)
        self.assertEqual(envelope["outcome"]["test_result"], "STALE")
        self.assertTrue(
            all(r["phase"] == "REPORTED" for r in router.state.read().values())
        )

    def test_released_fence_cannot_launch_again(self):
        def released(**kwargs):
            original_capture = kwargs["capture"]

            def capture(runtime):
                captured = original_capture(runtime)
                self.coordinator.complete_lease(
                    kwargs["execution_id"],
                    kwargs["item"]["case_id"],
                    captured["lease_id"],
                    captured["fencing_token"],
                    cleanup_verified=True,
                )
                return captured

            kwargs["capture"] = capture
            return self.execute_ok(**kwargs)

        self.execute = released
        router = self.make_router()
        router.tick()
        for future, _, _ in list(router.active.values()):
            future.result(timeout=15)
        for key, record in router.state.read().items():
            if record["phase"] == "QUEUED":
                router.finish(
                    key, "CANCELLED", 130, ["released-fence negative fixture completed"]
                )
        self.drain(router)
        npu = [
            r
            for r in router.state.read().values()
            if r["item"]["kind"] != "worker_probe"
        ]
        self.assertTrue(all(r["report"]["status"] != "PASS" for r in npu))

    def test_unknown_persisted_protocol_refuses_without_changing_state(self):
        from ci_core.job_state import JobState

        state = JobState(self.root)
        state.update("future-version", protocol=5, phase="QUEUED")
        before = state.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unknown persisted"):
            Router(self.root, "blue", self.api, "reporter")
        self.assertEqual(state.path.read_bytes(), before)

    def test_schema4_maintenance_failure_does_not_block_healthy_poll(self):
        router = self.make_router()
        self.api.running_workflows = Mock(return_value=[])
        with patch.object(
            router.gitcode, "maintain", side_effect=OSError("isolated schema4 outage")
        ):
            router.tick()
        self.api.running_workflows.assert_called_once()

    def test_finished_history_is_not_read_back_from_checks_every_tick(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        self.assertEqual(self.coordinator.pending_execution_snapshots(), [])
        with patch.object(router.gitcode.delivery, "publish_complete") as publish:
            for _ in range(5):
                router.gitcode.flush()
            publish.assert_not_called()

    def test_known_run_is_not_redownloaded_after_admission(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        with (
            patch.object(
                self.api.reader,
                "early_intent",
                side_effect=AssertionError("unnecessary artifact scan"),
            ),
            patch.object(
                self.api.reader,
                "load",
                side_effect=AssertionError("unnecessary source download"),
            ),
        ):
            router.receive([self.api.run])
        self.assertEqual(len(self.executed), 2)

    def test_lost_json_projection_uses_ledger_not_reexecution_or_artifacts(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        router.close()
        router.state.path.write_text("{}")
        self.api.metadata.clear()
        self.api.zips.clear()
        resumed = self.make_router()
        resumed.receive([self.api.run])
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(len(resumed.state.read()), 3)
        self.assertTrue(
            all(r["phase"] == "REPORTED" for r in resumed.state.read().values())
        )

    def test_result_acknowledgement_rejects_incomplete_or_wrong_receipt(self):
        from ci_core.gitcode_state import StateError

        router = self.make_router()
        router.receive([self.api.run])
        snapshot = self.coordinator.execution_snapshots()[0]
        with self.assertRaisesRegex(StateError, "INCOMPLETE"):
            self.coordinator.mark_aggregate_delivered(
                snapshot["id"], snapshot["receipt"], now=1100
            )
        with self.assertRaisesRegex(StateError, "RECEIPT_MISMATCH"):
            self.coordinator.mark_aggregate_delivered(
                snapshot["id"],
                {**snapshot["receipt"], "publication_revision": 123},
                now=1100,
            )

    def test_v2_migration_preserves_requests_leases_and_outbox(self):
        import sqlite3

        router = self.make_router()
        router.receive([self.api.run])
        tables = (
            "pr_state",
            "intents",
            "deliveries",
            "executions",
            "requests",
            "cases",
            "leases",
            "outbox",
        )
        with sqlite3.connect(self.coordinator.path) as db:
            # Exact old-v2 structure; no production files are involved.
            db.execute("DROP TABLE publication_receipts")
            db.execute("PRAGMA user_version=2")
            present = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            tables = [table for table in tables if table in present]
            before = {
                table: db.execute("SELECT * FROM " + table).fetchall()
                for table in tables
            }
        migrated = Coordinator(self.coordinator.path)
        with sqlite3.connect(migrated.path) as db:
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], STATE_VERSION
            )
            self.assertEqual(
                {
                    table: db.execute("SELECT * FROM " + table).fetchall()
                    for table in tables
                },
                before,
            )
        self.assertEqual(len(migrated.execution_snapshots()), 1)

    def test_existing_schema3_quarantine_is_preserved_by_schema4_execution(self):
        from ci_core.job_state import JobState

        state = JobState(self.root)
        state.update(
            "existing-quarantine",
            phase="REPORTED",
            quarantined=True,
            repo="cann-framework/torchtitan-npu",
            item={"kind": "train"},
            allocation=[{"host_id": "192.0.2.11:22", "devices": list(range(8, 16))}],
        )
        before = state.read()["existing-quarantine"]
        router = self.make_router()
        router.tick()
        self.drain(router)
        self.assertEqual(router.state.read()["existing-quarantine"], before)

    def test_authority_requests_retain_delivered_owner_and_responder_uses_exact_receipt(
        self,
    ):
        router = self.make_router()
        router.tick()
        self.drain(router)
        references = list(
            self.coordinator.iter_authority_requests(now=1100, page_size=1)
        )
        self.assertEqual(len(references), 1)
        self.assertTrue(
            self.coordinator.execution_snapshot(references[0]["execution_id"])[
                "aggregate_delivered"
            ]
        )
        self.assertEqual(
            list(self.coordinator.iter_authority_requests(now=self.plan["deadline"])),
            [],
        )
        fake_plan = {**self.plan, "authority_protocol": "artifact-check-v1"}
        verified = Mock(plan=fake_plan)
        responder = Mock()
        responder.cleanup.return_value = False
        self.assertEqual(
            len(
                list(
                    self.coordinator.iter_authority_requests(
                        now=self.plan["deadline"], include_expired=True
                    )
                )
            ),
            1,
        )
        router.gitcode.responders[
            (
                self.plan["github"]["repo_id"],
                self.plan["control"]["control_sha"],
                self.plan["github"]["workflow_id"],
            )
        ] = responder
        with (
            patch.object(router.gitcode, "resolve_request", return_value=verified),
            patch.object(self.api.reader, "actual_run") as redundant_run,
        ):
            router.gitcode.respond_authority()
        redundant_run.assert_not_called()
        responder.respond.assert_called_once_with(
            verified, references[0]["receipt"], current=router.gitcode.current_authority
        )
        responder.reset_mock()
        responder.cleanup.return_value = True
        with patch.object(router.gitcode, "resolve_request", return_value=verified):
            router.gitcode.respond_authority()
        responder.cleanup.assert_called_once_with(verified, references[0]["receipt"])
        responder.respond.assert_not_called()

    def test_current_authority_rechecks_trusted_plan_and_publication_owner(self):
        router = self.make_router()
        router.tick()
        self.drain(router)
        reference = list(self.coordinator.iter_authority_requests(now=1100))[0]
        verified = router.gitcode.resolve_request(reference["request_id"])
        broker = router.gitcode.broker(self.plan)
        broker.verify_plan = Mock(
            side_effect=ValueError("new selector intent revoked old plan")
        )
        self.assertFalse(
            router.gitcode.current_authority(verified, reference["receipt"])
        )
        broker.verify_plan.assert_called_once_with(self.plan)

    def test_gitcode_publication_failure_retains_pending_without_reexecution(self):
        router = self.make_router()
        publisher = Mock()
        publisher.publish.side_effect = OSError("publication unavailable")
        router.gitcode.publishers[
            (
                self.plan["github"]["repo_id"],
                self.plan["control"]["control_sha"],
                self.plan["github"]["workflow_id"],
            )
        ] = publisher
        with self.assertLogs(level="WARNING"):
            router.tick()
            for _ in range(10):
                for future, _, _ in list(router.active.values()):
                    future.result(timeout=15)
                router.pump()
                router.gitcode.flush()
                if not router.active:
                    break
        self.assertEqual(len(self.executed), 2)
        self.assertTrue(self.coordinator.pending_execution_snapshots())
        publisher.publish.side_effect = None
        router.gitcode.flush()
        self.assertEqual(self.coordinator.pending_execution_snapshots(), [])
        self.assertEqual(len(self.executed), 2)
        self.assertGreaterEqual(publisher.publish.call_count, 2)

    def test_crash_after_remote_aggregate_before_local_ack_only_retries_delivery(self):
        router = self.make_router()
        with patch.object(
            self.coordinator,
            "mark_aggregate_delivered",
            side_effect=OSError("local ack interrupted"),
        ):
            router.tick()
            for _ in range(10):
                for future, _, _ in list(router.active.values()):
                    future.result(timeout=15)
                router.pump()
                router.gitcode.flush()
                if not router.active:
                    break
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(
            self.api.channel.read_receipt(self.plan)["outcome"]["test_result"], "PASS"
        )
        self.api.metadata.clear()
        self.api.zips.clear()
        router.gitcode.flush()
        self.assertEqual(self.coordinator.pending_execution_snapshots(), [])
        self.assertEqual(len(self.executed), 2)


if __name__ == "__main__":
    unittest.main()
