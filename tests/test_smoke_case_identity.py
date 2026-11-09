"""Repeated smoke cases share a definition but never execution identity."""

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from ci_core.contract import (
    TORCHTITAN_REPO,
    create_plan,
    digest,
    normalize,
    parse_plan,
)
from ci_core.executor import remote_task_name, task_dir
from ci_core.ssh_runner import STAMP

DEFAULTS = {
    "worker": "blue",
    "target_pool": "a3_shared_blue",
    "docker": "image:tag",
}


def definition(name, nodes=1, kind="npu_smoke"):
    return SimpleNamespace(
        test_name=name,
        test_descr=name,
        nnodes=nodes if kind != "worker_probe" else 0,
        ngpu=8 if kind != "worker_probe" else 0,
        ci_kind=kind,
        ci_overrides={},
        timeout=75,
    )


def run_identity(provider="github"):
    return {
        "id": 123 if provider == "github" else "run-123",
        "run_number": 4,
        "run_attempt": 1,
        "name": "Dispatcher-Smoke",
        "created_at": "2026-11-04T11:12:13Z",
        "_ci_provider": provider,
        "_ci_repo": TORCHTITAN_REPO
        if provider == "github"
        else "depeng1994/torchtitan-npu",
        "_ci_test_id": "dispatch_smoke_8p",
    }


class SmokePlanIdentityTests(unittest.TestCase):
    def test_existing_fixture_still_parses(self):
        fixture = Path(__file__).parent / "fixtures/lite_actions/schema3-plan.json"
        plan = json.loads(fixture.read_text())
        run = {
            "id": plan["run_id"],
            "run_attempt": plan["attempt"],
            "head_sha": plan["sha"],
        }
        self.assertEqual(
            parse_plan(plan, run, plan["workflow"], repo=TORCHTITAN_REPO),
            plan["cases"],
        )

    def test_8p_16p_8p_has_distinct_case_ids_for_both_providers(self):
        eight = definition("dispatch_smoke_8p")
        sixteen = definition("dispatch_smoke_16p", nodes=2)
        cases = [
            normalize(item, DEFAULTS, i)
            for i, item in enumerate([eight, sixteen, eight])
        ]
        plan = create_plan(
            cases,
            run_id=123,
            attempt=1,
            sha="a" * 40,
            workflow="lite_actions_dispatcher_smoke.yml",
            defaults=DEFAULTS,
        )
        github = {"id": 123, "run_attempt": 1, "head_sha": "a" * 40}
        self.assertEqual(parse_plan(plan, github, plan["workflow"]), cases)
        self.assertEqual(len({case["case_id"] for case in cases}), 3)

        gitcode_plan = {
            **plan,
            "repo": "depeng1994/torchtitan-npu",
            "workflow": "lite-actions.yml",
            "run_id": "run-123",
        }
        gitcode_plan["plan_digest"] = digest(
            {k: v for k, v in gitcode_plan.items() if k != "plan_digest"}
        )
        gitcode = {
            "_ci_provider": "gitcode",
            "id": "run-123",
            "run_attempt": 1,
            "head_sha": "a" * 40,
        }
        self.assertEqual(
            parse_plan(
                gitcode_plan,
                gitcode,
                gitcode_plan["workflow"],
                repo=gitcode_plan["repo"],
            ),
            cases,
        )

        broken = copy.deepcopy(gitcode_plan)
        broken["cases"][2]["case_id"] = broken["cases"][0]["case_id"]
        broken["plan_digest"] = digest(
            {k: v for k, v in broken.items() if k != "plan_digest"}
        )
        with self.assertRaisesRegex(ValueError, "case order/ID"):
            parse_plan(broken, gitcode, broken["workflow"], repo=broken["repo"])

        altered = copy.deepcopy(gitcode_plan)
        altered["cases"][2]["resources"]["nodes"] = 2
        altered["plan_digest"] = digest(
            {k: v for k, v in altered.items() if k != "plan_digest"}
        )
        with self.assertRaisesRegex(ValueError, "duplicate/invalid test"):
            parse_plan(altered, gitcode, altered["workflow"], repo=altered["repo"])

    def test_repeated_non_smoke_test_is_still_rejected(self):
        for kind in ("train", "worker_probe"):
            test = definition("repeated", kind=kind)
            cases = [normalize(test, DEFAULTS, i) for i in range(2)]
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(ValueError, "duplicate/invalid test"),
            ):
                create_plan(
                    cases,
                    run_id=123,
                    attempt=1,
                    sha="a" * 40,
                    workflow="lite_actions_dispatcher_smoke.yml",
                    defaults=DEFAULTS,
                )


class SmokeExecutionIdentityTests(unittest.TestCase):
    def test_legacy_paths_remain_unchanged_without_instance(self):
        github = run_identity()
        self.assertEqual(
            task_dir(github, Path("/tmp/ci")).name,
            "2026-11-04_19-12-13_run-123_attempt-1_test-dispatch_smoke_8p",
        )
        self.assertEqual(
            remote_task_name(github),
            "2026-11-04_19-12-13_run_Dispatcher-Smoke_4_test-dispatch_smoke_8p",
        )
        gitcode = run_identity("gitcode")
        self.assertEqual(
            remote_task_name(gitcode),
            "gitcode_depeng1994_torchtitan-npu_run-123_attempt-1_test-dispatch_smoke_8p",
        )

    def test_same_test_has_distinct_local_remote_and_marker_names(self):
        for provider in ("github", "gitcode"):
            first = {
                **run_identity(provider),
                "_ci_case_id": "case_000_dispatch_smoke_8p",
            }
            last = {
                **run_identity(provider),
                "_ci_case_id": "case_002_dispatch_smoke_8p",
            }
            with self.subTest(provider=provider):
                first_local = task_dir(first, Path("/tmp/ci"))
                last_local = task_dir(last, Path("/tmp/ci"))
                self.assertNotEqual(first_local, last_local)
                self.assertNotEqual(first_local.name, last_local.name)
                self.assertNotEqual(remote_task_name(first), remote_task_name(last))
                self.assertTrue(STAMP.fullmatch(first_local.name))
                self.assertTrue(STAMP.fullmatch(remote_task_name(first)))

    def test_same_run_and_case_do_not_collide_across_sources_or_attempts(self):
        github = {
            **run_identity(),
            "id": "123",
            "_ci_case_id": "case_000_dispatch_smoke_8p",
        }
        gitcode = {
            **run_identity("gitcode"),
            "id": "123",
            "_ci_case_id": "case_000_dispatch_smoke_8p",
        }
        retry = {**github, "run_attempt": 2}
        names = {task_dir(r, Path("/tmp/ci")).name for r in (github, gitcode, retry)}
        self.assertEqual(len(names), 3)
        self.assertEqual(
            len({remote_task_name(r) for r in (github, gitcode, retry)}), 3
        )

    def test_run_ids_with_same_display_prefix_do_not_collide(self):
        common = {
            **run_identity("gitcode"),
            "_ci_case_id": "case_000_dispatch_smoke_8p",
        }
        first = {**common, "id": "a" * 120 + "one"}
        second = {**common, "id": "a" * 120 + "two"}
        self.assertNotEqual(
            task_dir(first, Path("/tmp/ci")), task_dir(second, Path("/tmp/ci"))
        )
        self.assertNotEqual(remote_task_name(first), remote_task_name(second))
        self.assertEqual(remote_task_name(first), remote_task_name(dict(first)))

    def test_invalid_or_mismatched_case_identity_fails_closed(self):
        base = run_identity()
        for case_id in (
            None,
            "../case_000_dispatch_smoke_8p",
            "case_999_dispatch_smoke_8p",
            "case_000_another_test",
        ):
            with self.subTest(case_id=case_id), self.assertRaises(ValueError):
                task_dir({**base, "_ci_case_id": case_id}, Path("/tmp/ci"))
            with self.subTest(remote=case_id), self.assertRaises(ValueError):
                remote_task_name({**base, "_ci_case_id": case_id})

    def test_maximum_ids_have_bounded_safe_path_components(self):
        test_id = "x" * 80
        for provider in ("github", "gitcode"):
            run = {
                **run_identity(provider),
                "id": "a" * 128,
                "run_number": 10**100,
                "run_attempt": 10**100,
                "name": "n" * 64,
                "_ci_test_id": test_id,
                "_ci_case_id": "case_255_" + test_id,
            }
            for name in (task_dir(run, Path("/tmp/ci")).name, remote_task_name(run)):
                with self.subTest(provider=provider, name=name):
                    self.assertLessEqual(len(name.encode()), 255)
                    self.assertTrue(STAMP.fullmatch(name))


if __name__ == "__main__":
    unittest.main()
