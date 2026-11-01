"""Fixed schema-3 wire fixture shared with the model producer; no source mirroring."""

import copy
import json
import unittest
from pathlib import Path

from ci_core.contract import TORCHTITAN_REPO, create_plan, parse_plan


class WireContractTests(unittest.TestCase):
    def test_model_fixture_is_accepted_and_digest_is_stable(self):
        plan = json.loads(
            (
                Path(__file__).parent / "fixtures/lite_actions/schema3-plan.json"
            ).read_text()
        )
        run = dict(id=19, run_attempt=2, head_sha="a" * 40)
        self.assertEqual(
            parse_plan(plan, run, plan["workflow"], repo=TORCHTITAN_REPO), plan["cases"]
        )
        self.assertEqual(
            create_plan(
                plan["cases"],
                run_id=19,
                attempt=2,
                sha="a" * 40,
                workflow=plan["workflow"],
                defaults=plan["defaults"],
                repo=TORCHTITAN_REPO,
            ),
            plan,
        )
        changed = copy.deepcopy(plan)
        changed["cases"][1]["execution_timeout_seconds"] += 1
        with self.assertRaisesRegex(ValueError, "digest"):
            parse_plan(changed, run, plan["workflow"], repo=TORCHTITAN_REPO)
