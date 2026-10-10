"""A5 resource registration remains safe even with unreachable hardware."""
from pathlib import Path
import unittest
from unittest.mock import patch

from ci_core.config import load_config
from scripts.preflight_pool import check_node, main

ROOT = Path(__file__).resolve().parents[1]


class A5OnboardingTests(unittest.TestCase):
    def test_a5_real_pool_inventory_and_disabled_dispatch(self):
        cases = load_config(ROOT)
        from json import loads
        pool = loads((ROOT / "config/pools.json").read_text())["a5-64p-lab"]
        self.assertEqual(pool["type"], "ascend-a5")
        self.assertEqual(len(pool["nodes"]), 8)
        self.assertEqual([n["ssh"] for n in pool["nodes"]],
                         [f"root@7.246.125.{i}" for i in range(19, 27)])
        self.assertEqual(len({n["host_id"] for n in pool["nodes"]}), 8)
        self.assertTrue(all(n["device_ids"] == list(range(8)) for n in pool["nodes"]))
        case = cases["a5-64p"]
        self.assertEqual(case["pool"], "a5-64p-lab")
        self.assertFalse(case["enabled"])
        self.assertEqual(case["runner"]["kind"], "ssh")
        self.assertEqual(sum(len(item["devices"]) for item in case["allocation"]), 64)

    def test_a5_cannot_enable_before_assets_are_configured(self):
        from tempfile import TemporaryDirectory
        from shutil import copyfile
        import json
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            copyfile(ROOT / "config/pools.json", root / "config/pools.json")
            cases = json.loads((ROOT / "config/pipelines.json").read_text())
            cases["a5-64p"]["enabled"] = True
            (root / "config/pipelines.json").write_text(json.dumps(cases))
            with self.assertRaisesRegex(ValueError, "unconfigured deployment|verified HCCL node_ip"):
                load_config(root)

    def test_preflight_never_executes_training_and_does_not_auto_accept_keys(self):
        with patch("scripts.preflight_pool.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "a5-lab\n"
            check_node({"host_id": "a5-01", "ssh": "root@7.246.125.19"}, npu=True)
            args = run.call_args.args[0]
            self.assertEqual(args[-1], "npu-smi info")
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertNotIn("bash", args)

    def test_preflight_rejects_unknown_pool(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--pool", "fake-pool"])


if __name__ == "__main__":
    unittest.main()
