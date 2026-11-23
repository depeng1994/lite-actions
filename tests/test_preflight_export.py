import argparse
import unittest
from unittest.mock import patch

from scripts.preflight import device_list, node_snippet


class ExportTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(
            host_id="198.51.100.5:22",
            ssh="fixture-host",
            host_ssh=None,
            node_ip="192.0.2.8",
            nic="eth0",
            devices="0-15",
            groups="0-7;8-15",
            environment="fixture",
            container="test",
        )

    def test_exports_canonical_groups_and_existing_binding(self):
        binding = {
            "container_id": "b" * 64,
            "image_id": "sha256:" + "c" * 64,
            "image_ref": "registry/ci:latest",
        }
        with patch(
            "scripts.preflight.probe_node",
            return_value={"machine_fingerprint": "a" * 64, "binding": binding},
        ) as probe:
            value = node_snippet(self.args())
        self.assertEqual(value["allocation_groups"][1]["devices"], list(range(8, 16)))
        self.assertEqual(value["environments"]["fixture"], binding)
        self.assertEqual(value["broadcast_ssh"], ["fixture-host"])
        self.assertEqual(probe.call_args.kwargs["container_ref"], "test")

    def test_rejects_outside_inventory_without_ssh(self):
        args = self.args()
        args.groups = "0-16"
        with (
            patch("scripts.preflight.probe_node") as probe,
            self.assertRaises(ValueError),
        ):
            node_snippet(args)
        probe.assert_not_called()
        with self.assertRaises(ValueError):
            device_list("0-1,1")
