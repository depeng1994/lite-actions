"""Lite Actions maintains only the dispatcher side of the contract.

The authoritative workflow and waiter tests live in the model repository.
"""
from __future__ import annotations
import json
from pathlib import Path
import unittest

import unified_agent
from ci_core import reporting
from ci_core.config import load_config
from ci_core.protocol import parse_plan, plan_digest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowContractTests(unittest.TestCase):
    def test_authorized_pipeline_names_are_stable(self):
        expected = {
            "a3-8p": "A3-8p-CI-Example",
            "a3-16p": "A3-16p-CI-Example",
            "a5-64p": "A5-64p-CI",
        }
        cfg = load_config(ROOT)
        self.assertEqual(cfg["dispatcher-smoke"]["runner"],
                         {"kind": "worker", "worker_id": "depeng"})
        self.assertEqual(cfg["dispatcher-smoke"]["pipeline_id"], "npu-smi")
        self.assertTrue(cfg["dispatcher-smoke"]["enabled"])
        for key, value in expected.items():
            self.assertEqual(unified_agent.STATUSES[key], value)
            self.assertEqual(reporting.NAMES[key][0], value)
            self.assertEqual(cfg[key]["runner"], {"kind": "ssh"})

    def test_no_static_npu_allocation_or_asset_override_in_pipelines(self):
        cfg = load_config(ROOT)
        self.assertEqual(cfg["a3-8p"]["resources"],
                         {"nodes": 1, "devices_per_node": 8})
        self.assertEqual(cfg["a3-16p"]["resources"],
                         {"nodes": 2, "devices_per_node": 8})
        self.assertEqual(cfg["a5-64p"]["resources"],
                         {"nodes": 8, "devices_per_node": 8})
        for key in ("a3-8p", "a3-16p", "a5-64p"):
            spec = cfg[key]
            self.assertNotIn("allocation", spec)
            self.assertNotIn("env_vars", spec["runner"])
        self.assertFalse(cfg["a5-64p"]["enabled"])

    def test_matrix_digest_and_identity_validation(self):
        run = {"id": 42, "run_attempt": 1, "head_sha": "a"*40}
        plan = {"schema": 2, "repo": "torchtitan-npu/torchtitan-npu",
                "run_id": 42, "attempt": 1, "sha": "a"*40,
                "pipeline": "a3-8p",
                "cases": [{"test_id": "dsv4_flash_a3_8p_example",
                           "nnodes": 1, "ngpu": 8, "label": "Muon 8P"}]}
        plan["plan_digest"] = plan_digest(plan)
        self.assertEqual(len(parse_plan(plan, run, "a3-8p")), 1)
        for mutation in ({"attempt": 2}, {"sha": "b"*40},
                         {"plan_digest": "0"*64}, {"repo": "untrusted/repo"}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_plan({**plan, **mutation}, run, "a3-8p")
        with self.assertRaises(ValueError):
            parse_plan({**plan, "cases": [*plan["cases"], *plan["cases"]]},
                       run, "a3-8p")

    def test_more_than_twelve_matrix_cases_are_accepted(self):
        run = {"id": 42, "run_attempt": 1, "head_sha": "a"*40}
        cases = [{"test_id": f"trusted_case_{i:03d}", "nnodes": 1,
                  "ngpu": 8, "label": f"Case {i}"} for i in range(20)]
        plan = {"schema": 2, "repo": "torchtitan-npu/torchtitan-npu",
                "run_id": 42, "attempt": 1, "sha": "a"*40,
                "pipeline": "a3-8p", "cases": cases}
        plan["plan_digest"] = plan_digest(plan)
        self.assertEqual(parse_plan(plan, run, "a3-8p"), cases)

    def test_source_configuration_is_upstream_only(self):
        from ci_core.source_repo import REPO
        self.assertEqual(REPO, "torchtitan-npu/torchtitan-npu")

    def test_dispatcher_denies_untrusted_source(self):
        cfg=load_config(ROOT)
        run=dict(id=91,run_number=1,run_attempt=1,
                 head_sha="a"*40,head_branch="master",
                 event="workflow_dispatch",status="in_progress",
                 actor={"login":"ci-maintainer"},
                 path=".github/workflows/a3_8p_lite_actions.yml",
                 name="A3-8p-CI-Example",display_title="A3-8p-CI-Example")
        from unittest.mock import patch
        with patch.object(unified_agent,"ALLOWED_ACTORS",frozenset({"ci-maintainer"})):
            self.assertTrue(unified_agent.eligible(run,cfg["a3-8p"]))
            self.assertFalse(unified_agent.eligible({**run,"head_branch":"untrusted-pr"},
                                                   cfg["a3-8p"]))
            self.assertFalse(unified_agent.eligible({**run,"actor":{"login":"attacker"}},
                                                   cfg["a3-8p"]))


if __name__ == "__main__":
    unittest.main()
