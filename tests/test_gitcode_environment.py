"""Container identity checks never grant a second physical resource namespace."""

import copy
import unittest

from ci_core.gitcode_environment import ContainerBinding, EnvironmentError
from ci_core.resources import resource_keys


class ContainerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.node = {"host_id": "192.0.2.1:153", "environments": {"vllm_hccl_smoke": {
            "container_id": "a" * 64, "image_id": "sha256:" + "b" * 64,
            "image_ref": "registry.example.invalid/vllm:test",
        }}}
        self.inspect = {"Id": "a" * 64, "Name": "/vllm_hccl_smoke", "Image": "sha256:" + "b" * 64, "State": {"Running": True}}
        self.binding = ContainerBinding.from_node(self.node)

    def test_matching_name_id_image_and_running(self):
        self.assertEqual(self.binding.verify_inspection(self.inspect, resolved_image_id=self.inspect["Image"]), self.binding)

    def test_old_torchtitan_container_cannot_be_relabeled_by_config(self):
        self.inspect["Name"] = "/cann_master_torchtitan_master_xyj"
        with self.assertRaisesRegex(EnvironmentError, "IDENTITY_MISMATCH"):
            self.binding.verify_inspection(self.inspect, resolved_image_id=self.inspect["Image"])

    def test_same_name_different_id_or_image_or_stopped_is_rejected(self):
        for patch in ({"Id": "c" * 64}, {"Image": "sha256:" + "d" * 64}, {"State": {"Running": False}}):
            inspection = {**self.inspect, **patch}
            with self.subTest(patch=patch), self.assertRaises(EnvironmentError):
                self.binding.verify_inspection(inspection, resolved_image_id=self.inspect["Image"])
        with self.assertRaises(EnvironmentError):
            self.binding.verify_inspection(self.inspect, resolved_image_id="sha256:" + "e" * 64)

    def test_recovery_never_switches_to_recreated_same_name(self):
        recreated = {**self.inspect, "Id": "c" * 64}
        with self.assertRaisesRegex(EnvironmentError, "REPLACED_OR_MISSING"):
            self.binding.verify_recovery(self.binding.metadata(), recreated)
        altered = {**self.binding.metadata(), "container_id": "c" * 64}
        with self.assertRaisesRegex(EnvironmentError, "BINDING_CHANGED"):
            self.binding.verify_recovery(altered, recreated)
        stopped_original = {**self.inspect, "State": {"Running": False}}
        self.assertEqual(self.binding.verify_recovery(self.binding.metadata(), stopped_original), self.binding)

    def test_missing_or_short_binding_does_not_fall_back(self):
        node = copy.deepcopy(self.node)
        node["environments"]["vllm_hccl_smoke"]["container_id"] = "short-id"
        with self.assertRaisesRegex(EnvironmentError, "ID_NOT_PINNED"):
            ContainerBinding.from_node(node)
        with self.assertRaisesRegex(EnvironmentError, "BINDING_MISSING"):
            ContainerBinding.from_node({"host_id": self.node["host_id"], "environments": {}})

    def test_same_host_devices_share_lock_across_containers(self):
        a = [{"host_id": self.node["host_id"], "device_ids": list(range(8)), "container_id": "a" * 64}]
        b = [{"host_id": self.node["host_id"], "device_ids": list(range(8)), "container_id": "b" * 64}]
        self.assertEqual(resource_keys(a), resource_keys(b))


if __name__ == "__main__":
    unittest.main()
