"""Production deployment probes must not request NPU resources."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core import self_smoke


class CpuSelfSmoke(unittest.TestCase):
    def test_prepare_matches_worker_plan_without_changing_full_profile(self):
        run = dict(id=22, run_attempt=1, head_sha="a" * 40, path=self_smoke.SELF_WORKFLOW)
        env = dict(
            GITHUB_REPOSITORY=self_smoke.SELF_REPO,
            GITHUB_RUN_ID="22",
            GITHUB_RUN_ATTEMPT="1",
            GITHUB_SHA="a" * 40,
            SELF_SMOKE_PROFILE="cpu-only",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self_smoke, "PLAN", Path(tmp) / "plan.json"):
                cpu = self_smoke.prepare(env)
        self.assertEqual(cpu, self_smoke.expected_plan(dict(run, display_title=self_smoke.CPU_TITLE)))
        self.assertEqual(len(cpu["cases"]), 1)
        case = cpu["cases"][0]
        self.assertEqual((case["test_id"], case["kind"]), ("worker_os_blue", "worker_probe"))
        self.assertEqual(case["resources"]["nodes"], 0)
        self.assertEqual(case["resources"]["devices_per_node"], 0)
        full = self_smoke.expected_plan(run)
        self.assertTrue(any(c["kind"] == "npu_smoke" for c in full["cases"]))
        self.assertNotEqual(cpu["plan_digest"], full["plan_digest"])
