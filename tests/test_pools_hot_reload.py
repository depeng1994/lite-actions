import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ci_core.config import execution_pools, load_active_pools
from ci_core.router import Router
from ci_core.scheduler import candidates, validate_allocation

HOST = "198.51.100.11:22"
HOST2 = "198.51.100.12:22"
DEVICES = list(range(8))


def config():
    first = {
        "host_id": HOST,
        "node_ip": "192.0.2.1",
        "ssh": "first",
        "allocation_groups": [
            {"devices": DEVICES, "verified": True},
            {"devices": list(range(8, 16)), "verified": True},
        ],
    }
    second = {
        "host_id": HOST2,
        "node_ip": "192.0.2.2",
        "ssh": "second",
        "allocation_groups": [
            {"devices": DEVICES, "verified": True},
        ],
    }
    return {
        "test": {
            "owner": "blue",
            "enabled": True,
            "type": "ascend-a3",
            "nodes": [first, second],
            "verified_multinode_allocations": [
                [
                    {"host_id": HOST, "devices": DEVICES},
                    {"host_id": HOST2, "devices": DEVICES},
                ]
            ],
        }
    }


def topology(nodes=1):
    return {"pool": "test", "resources": {"nodes": nodes, "devices_per_node": 8}}


class PoolReloadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "config").mkdir()
        self.pools = config()
        self.write_checkout(self.root, self.pools)
        self.router = Router(self.root, "blue", MagicMock(), "bot")
        self.router.control = MagicMock(wake=threading.Event())
        self.addCleanup(self.router.close)

    def write_checkout(self, root, pools):
        (root / "config/workers.json").write_text('{"blue":{"enabled":true}}')
        (root / "config/pools.json").write_text(json.dumps(pools))

    def request(self, pools, revision=1):
        config_json = json.dumps(pools, ensure_ascii=False, separators=(",", ":"))
        sha = hashlib.sha256(config_json.encode()).hexdigest()
        return {
            "cmd_id": f"cmd-{revision}",
            "cmd_type": "SYNC_CONFIG",
            "payload": {"revision": revision, "sha256": sha},
            "config": {
                "revision": revision,
                "sha256": sha,
                "config": pools,
                "config_json": config_json,
            },
        }

    def verify(self, command):
        self.assertFalse(self.router.sync_pools(command))
        self.router.config_verification.result(3)

    def test_half_node_pause_filters_combinations_and_persists_across_checkouts(self):
        old_lease = candidates(topology(2), self.pools)[0]
        newer = copy.deepcopy(self.pools)
        newer["test"]["nodes"][0]["allocation_groups"][0]["paused"] = True
        command = self.request(newer)
        self.verify(command)
        self.assertTrue(self.router.sync_pools(command))
        self.assertEqual(candidates(topology(2), self.router.pools), [])
        groups = candidates(topology(), self.router.pools)
        self.assertIn([{"host_id": HOST, "devices": list(range(8, 16))}], groups)
        self.assertIsNone(self.router.claim(topology(2)))
        snapshot = {"pools_snapshot": self.pools}
        validate_allocation(
            topology(2),
            execution_pools(self.root, snapshot),
            old_lease,
            include_paused=True,
        )
        checkout = self.root / "updated-release"
        (checkout / "config").mkdir(parents=True)
        self.write_checkout(checkout, {})
        _, after_update, revision = load_active_pools(
            checkout, "blue", self.router.state.path.parent
        )
        self.assertEqual(after_update, newer)
        self.assertEqual(revision, 1)
        self.router.control.complete.assert_called_once_with("cmd-1", "APPLIED", rev=1)

    def test_disabled_pool_keeps_cases_queued_without_pool_disabled_failure(self):
        newer = copy.deepcopy(self.pools)
        newer["test"]["enabled"] = False
        command = self.request(newer)
        self.verify(command)
        self.assertTrue(self.router.sync_pools(command))
        case = {
            "pool": "test",
            "resources": {"nodes": 1, "devices_per_node": 8},
            "kind": "npu_smoke",
            "execution_timeout_seconds": 60,
            "docker": {},
        }
        self.assertEqual(candidates(case, self.router.pools), [])
        self.assertIsNone(self.router.claim(case))
        self.assertTrue(self.router.pools["test"]["enabled"] is False)

    def test_failed_fingerprint_stays_at_last_valid_configuration(self):
        new = copy.deepcopy(self.pools)
        new["test"]["nodes"][0]["machine_fingerprint"] = "a" * 64
        command = self.request(new)
        self.router.control.commands.return_value = [command]
        with patch(
            "scripts.preflight.probe_machine",
            side_effect=ValueError("PHYSICAL_NODE_IDENTITY_CHANGED"),
        ):
            self.router.consume_control()
            try:
                self.router.config_verification.result(3)
            except ValueError:
                pass
            self.router.consume_control()
        self.assertEqual(self.router.pools_revision, 0)
        self.router.control.complete.assert_called_once_with(
            "cmd-1", "FAILED", err="PHYSICAL_NODE_IDENTITY_CHANGED"
        )

    def test_failed_probe_isolates_node_and_keeps_verified_nodes(self):
        newer = copy.deepcopy(self.pools)
        for node in newer["test"]["nodes"]:
            node["machine_fingerprint"] = "a" * 64
            node["environments"] = {
                "env": {
                    "container_id": "b" * 64,
                    "image_id": "sha256:image",
                    "image_ref": "image:tag",
                }
            }
        command = self.request(newer)
        with patch(
            "scripts.preflight.probe_node",
            side_effect=[RuntimeError("Permission denied (publickey)"), {"ok": True}],
        ):
            from ci_core.pools_sync import verify

            applied = verify(
                "blue",
                {"blue": {"enabled": True}},
                self.pools,
                command["config"],
                command["payload"],
            )
        failed_node, verified_node = applied["pools"]["test"]["nodes"]
        self.assertTrue(failed_node["paused"])
        self.assertTrue(failed_node["probe_unavailable"])
        self.assertIn("Permission denied", failed_node["probe_reason"])
        self.assertFalse(failed_node["allocation_groups"][0]["verified"])
        self.assertFalse(verified_node.get("paused", False))
        self.assertFalse(verified_node.get("probe_unavailable", False))
        self.assertEqual(applied["source_sha256"], command["payload"]["sha256"])
        self.assertEqual(
            hashlib.sha256(applied["config_json"].encode()).hexdigest(),
            applied["sha256"],
        )
        self.assertEqual(
            candidates(topology(), applied["pools"]),
            [[{"host_id": HOST2, "devices": DEVICES}]],
        )

    def test_probe_unavailable_nodes_are_never_candidates(self):
        pools = copy.deepcopy(self.pools)
        node = pools["test"]["nodes"][0]
        node["probe_unavailable"] = True
        node["paused"] = True
        node["allocation_groups"][0]["probe_unavailable"] = True
        node["allocation_groups"][0]["verified"] = False
        self.assertEqual(
            candidates(topology(), pools),
            [[{"host_id": HOST2, "devices": DEVICES}]],
        )

    def test_deletion_waits_for_active_lease_and_refuses_quarantine(self):
        new = copy.deepcopy(self.pools)
        new["test"]["nodes"].pop(0)
        new["test"]["verified_multinode_allocations"] = []
        allocation = candidates(topology(2), self.pools)[0]
        self.router.state.update("active", phase="RUNNING", allocation=allocation)
        command = self.request(new)
        self.verify(command)
        self.assertFalse(self.router.sync_pools(command))
        self.assertEqual(self.router.pools_revision, 0)
        self.assertIsNone(self.router.claim(topology(2)))
        self.router.state.update("active", phase="REPORTED", quarantined=True)
        with self.assertRaisesRegex(ValueError, "QUARANTINED_RESOURCE_CANNOT_DELETE"):
            self.router.sync_pools(command)
        self.router.state.update("active", quarantined=False)
        self.assertTrue(self.router.sync_pools(command))
        self.assertEqual(self.router.pools_revision, 1)
