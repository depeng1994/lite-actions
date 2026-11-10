"""Repeated smoke cases share a definition but never execution identity."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from ci_core.contract import (
    TORCHTITAN_REPO,
    VLLM_REPO,
    create_plan,
    normalize,
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
        "_ci_test_id": "smoke_alpha",
    }


class SmokePlanIdentityTests(unittest.TestCase):
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
    def test_long_pr_run_title_has_a_bounded_unique_directory(self):
        run = {
            **run_identity(),
            "_ci_repo": VLLM_REPO,
            "name": "lite-vllm-recheck-23-" + "a" * 40 + "-" + "b" * 32,
            "_ci_case_id": "case_000_smoke_alpha",
        }
        first = remote_task_name(run)
        second = remote_task_name({**run, "_ci_case_id": "case_002_smoke_alpha"})
        self.assertNotEqual(first, second)
        self.assertTrue(STAMP.fullmatch(first))
        self.assertLessEqual(len(first.encode()), 255)

    def test_legacy_paths_remain_unchanged_without_instance(self):
        github = run_identity()
        self.assertEqual(
            task_dir(github, Path("/tmp/ci")).name,
            "2026-11-04_19-12-13_run-123_attempt-1_test-smoke_alpha",
        )
        self.assertEqual(
            remote_task_name(github),
            "2026-11-04_19-12-13_run_Dispatcher-Smoke_4_test-smoke_alpha",
        )
        gitcode = run_identity("gitcode")
        self.assertEqual(
            remote_task_name(gitcode),
            "gitcode_depeng1994_torchtitan-npu_run-123_attempt-1_test-smoke_alpha",
        )

    def test_instances_do_not_share_local_or_remote_paths(self):
        github = {**run_identity(), "id": "123", "_ci_case_id": "case_000_smoke_alpha"}
        gitcode = {
            **run_identity("gitcode"),
            "id": "123",
            "_ci_case_id": "case_000_smoke_alpha",
        }
        variants = [
            github,
            {**github, "_ci_case_id": "case_002_smoke_alpha"},
            {**github, "run_attempt": 2},
            gitcode,
            {**gitcode, "_ci_case_id": "case_002_smoke_alpha"},
            {**gitcode, "id": "a" * 120 + "one"},
            {**gitcode, "id": "a" * 120 + "two"},
        ]
        for make_name in (
            lambda r: task_dir(r, Path("/tmp/ci")).name,
            remote_task_name,
        ):
            names = [make_name(run) for run in variants]
            self.assertEqual(len(set(names)), len(variants))
            self.assertTrue(all(STAMP.fullmatch(name) for name in names))

    def test_invalid_or_mismatched_case_identity_fails_closed(self):
        base = run_identity()
        for case_id in (
            None,
            "../case_000_smoke_alpha",
            "case_999_smoke_alpha",
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
