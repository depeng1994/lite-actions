"""Worker-local opt-in for disabling automatic quarantine during the pilot."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.executor import Execution
from ci_core.job_state import JobState
from ci_core.resources import reserve, resource_keys
from ci_core.router import Router, task_key
from tests.scenarios import Service, definition, write_workspace


class QuarantinePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_workspace(self.root)
        self.environment = patch.dict(
            "os.environ",
            LITE_ACTIONS_ALLOWED_ACTORS="maintainer",
            LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "leases"),
            LITE_ACTIONS_STATE_DIR="",
            LITE_ACTIONS_DISABLE_QUARANTINE_WORKERS="blue",
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = Service([definition("smoke", kind="npu_smoke")], self.root)

    def router(self, worker="blue"):
        router = Router(self.root, worker, self.service, "reporter")
        self.addCleanup(router.close)
        return router

    def test_unverified_cleanup_is_reported_and_resources_are_reusable(self):
        router = self.router()
        log = self.root / "result.log"
        log.write_text("CASE_RESULT smoke PASS 0\n")
        failed_cleanup = Execution(0, ["cleanup unverified"], log, False)
        with (
            self.service.connected(),
            patch("ci_core.router.verify_definition"),
            patch("ci_core.router.execute", return_value=failed_cleanup),
        ):
            router.receive([self.service.runs[20]])
            router.pump()
            for future, _, _ in list(router.active.values()):
                future.result(10)
        key = task_key(self.service.runs[20], self.service.plans[20]["cases"][0])
        record = router.state.read()[key]
        self.assertEqual(record["report"]["status"], "INFRA_ERROR")
        self.assertFalse(record["report"]["cleanup_verified"])
        self.assertFalse(record["quarantined"])

        previously_used = set(resource_keys(record["allocation"]))
        other_devices = [
            {
                "host_id": node["host_id"],
                "devices": [
                    device
                    for device in node["device_ids"]
                    if (node["host_id"], device) not in previously_used
                ],
            }
            for node in router.pools["pool_blue"]["nodes"]
        ]
        with reserve(self.root / "leases/devices", other_devices):
            lease, selected = router.claim(record["runtime"])
            try:
                self.assertEqual(
                    set(resource_keys(selected["allocation"])), previously_used
                )
                self.assertIsNone(router.claim(record["runtime"]))
            finally:
                lease.close()

    def test_restart_clears_only_blue_guards_and_keeps_other_worker_policy(self):
        path = self.root / "config/pools.json"
        pools = json.loads(path.read_text())
        yellow_node = {
            "host_id": "192.0.2.20:22",
            "node_ip": "192.0.2.20",
            "device_ids": [0, 1],
            "allocation_groups": [{"devices": [0, 1], "verified": True}],
        }
        pools["pool_yellow"] = {
            "owner": "yellow",
            "enabled": True,
            "nodes": [yellow_node],
        }
        path.write_text(json.dumps(pools))
        blue_allocation = [{"host_id": "192.0.2.10:22", "devices": [0, 1]}]
        yellow_allocation = [{"host_id": yellow_node["host_id"], "devices": [0, 1]}]
        state = JobState(self.root)
        state.update(
            "old-blue",
            phase="REPORTED",
            item={"worker": "blue"},
            quarantined=True,
            allocation=blue_allocation,
        )
        state.update(
            "legacy-blue",
            phase="REPORTED",
            quarantined=True,
            allocation=blue_allocation,
        )
        state.update("unscoped", phase="REPORTED", quarantined=True, allocation=[])
        state.update(
            "removed-blue-host",
            phase="REPORTED",
            item={"worker": "blue"},
            quarantined=True,
            allocation=[{"host_id": "192.0.2.99:22", "devices": [0]}],
        )
        state.update(
            "old-yellow",
            phase="REPORTED",
            item={"worker": "yellow"},
            quarantined=True,
            allocation=yellow_allocation,
        )
        state.update(
            "conflicting-owner",
            phase="REPORTED",
            item={"worker": "blue"},
            quarantined=True,
            allocation=yellow_allocation,
        )
        state.update(
            "interrupted-blue",
            phase="RUNNING",
            run=self.service.runs[20],
            item=self.service.plans[20]["cases"][0],
            digest=self.service.plans[20]["plan_digest"],
            allocation=blue_allocation,
        )

        blue = self.router()
        records = blue.state.read()
        for key in (
            "old-blue",
            "legacy-blue",
            "unscoped",
            "removed-blue-host",
            "interrupted-blue",
        ):
            self.assertFalse(records[key]["quarantined"])
        self.assertEqual(records["interrupted-blue"]["report"]["status"], "INFRA_ERROR")
        self.assertFalse(records["interrupted-blue"]["report"]["cleanup_verified"])
        self.assertTrue(records["old-yellow"]["quarantined"])
        self.assertTrue(records["conflicting-owner"]["quarantined"])
        case = {"pool": "pool_blue", "resources": {"nodes": 1, "devices_per_node": 2}}
        lease, _ = blue.claim(case)
        lease.close()

        yellow = self.router("yellow")
        self.assertTrue(yellow.quarantine_enabled)
        self.assertTrue(yellow.state.read()["old-yellow"]["quarantined"])
        self.assertIsNone(yellow.claim({**case, "pool": "pool_yellow"}))
