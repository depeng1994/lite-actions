"""Scheduler contracts for independently configured communication addresses."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.container_binding import ContainerBinding
from ci_core.scheduler import candidates, validate_allocation
from ci_core.ssh_runner import allocations, run


class CommunicationTopologyTests(unittest.TestCase):
    def test_addresses_are_unique_only_within_each_multinode_allocation(self):
        devices = list(range(8))

        def node(host_id, node_ip):
            return {
                "host_id": host_id,
                "node_ip": node_ip,
                "allocation_groups": [{"devices": devices, "verified": True}],
            }

        def group(host_id):
            return {"host_id": host_id, "devices": devices}

        shared_ip = "192.0.2.1"
        pools = {
            "one": {
                "nodes": [
                    node("one-a", shared_ip),
                    node("one-b", shared_ip),
                    node("one-c", "192.0.2.3"),
                ],
                "verified_multinode_allocations": [
                    [group("one-a"), group("one-b")],
                    [group("one-a"), group("one-c")],
                ],
            },
            "independent": {
                "nodes": [node("two-a", shared_ip), node("two-b", "192.0.2.4")],
                "verified_multinode_allocations": [[group("two-a"), group("two-b")]],
            },
        }
        case = {"pool": "one", "resources": {"nodes": 2, "devices_per_node": 8}}
        invalid, valid = pools["one"]["verified_multinode_allocations"]
        self.assertEqual(candidates(case, pools), [valid])
        validate_allocation(case, pools, valid)
        with self.assertRaisesRegex(ValueError, "outside verified topology"):
            validate_allocation(case, pools, invalid)

        single_node = {**case, "resources": {"nodes": 1, "devices_per_node": 8}}
        self.assertEqual(len(candidates(single_node, pools)), 3)
        self.assertEqual(len(candidates({**case, "pool": "independent"}, pools)), 1)

        container_id = "a" * 64
        environment = {
            "container_id": container_id,
            "image_id": "sha256:" + "b" * 64,
            "image_ref": "fixture:tag",
        }
        bound = []
        for position, name in enumerate(("one-a", "one-c")):
            host = next(
                node for node in pools["one"]["nodes"] if node["host_id"] == name
            )
            captured = {
                **host,
                "position": position,
                "devices": devices,
                "host_ssh": name,
                "ssh": f"{name}/{container_id}",
                "environments": {"vllm_hccl_smoke": environment},
            }
            captured["container_binding"] = ContainerBinding.from_node(
                captured
            ).metadata()
            bound.append(captured)
        bound_case = {
            **case,
            "allocation": valid,
            "docker": "vllm_hccl_smoke",
            "bound_nodes": bound,
        }
        validate_allocation(bound_case, pools, valid)
        self.assertIs(allocations(bound_case, pools), bound)
        bound[1]["node_ip"] = shared_ip
        with self.assertRaisesRegex(ValueError, "invalid bound node communication"):
            validate_allocation(bound_case, pools, valid)

        # Re-read configuration immediately before binding, even if an earlier
        # scheduler check used a different snapshot.
        with (
            patch.object(Path, "read_text", return_value=json.dumps(pools)),
            patch("ci_core.ssh_runner.allocations") as bind,
            self.assertRaisesRegex(ValueError, "outside verified topology"),
        ):
            run({**case, "allocation": invalid}, "a" * 40, "fixture", test_case="smoke")
        bind.assert_not_called()

        pools["one"]["nodes"][0]["node_ip"] = "2001:db8::1"
        pools["one"]["nodes"][1]["node_ip"] = "2001:0db8:0:0:0:0:0:1"
        pools["one"]["nodes"][2]["node_ip"] = "2001:db8::3"
        self.assertEqual(candidates(case, pools), [valid])
