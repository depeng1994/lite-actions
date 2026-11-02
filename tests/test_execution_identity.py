"""Native execution paths depend on immutable identity, never a run title."""

import unittest
from pathlib import PurePosixPath

from ci_core.contract import TORCHTITAN_REPO, VLLM_REPO
from ci_core.executor import remote_task_name


class ExecutionIdentityTests(unittest.TestCase):
    def native(self, **overrides):
        return {
            "id": 41367562498,
            "run_number": 7,
            "run_attempt": 2,
            "created_at": "2026-11-02T00:30:56Z",
            "name": "lite-vllm-pr-4-" + "a" * 40 + "-" + "b" * 40,
            "_ci_repo": VLLM_REPO,
            "_ci_test_id": "dispatch_smoke_a3_8p",
            "_ci_source": {"provider": "github", "mode": "merge_result"},
            **overrides,
        }

    def test_real_long_pr_title_uses_immutable_run_attempt_identity(self):
        self.assertEqual(
            remote_task_name(self.native()),
            "vllm_2026-11-02_08-30-56_run-41367562498_attempt-2"
            "_test-dispatch_smoke_a3_8p",
        )

    def test_untrusted_display_title_is_never_used_as_a_path(self):
        expected = remote_task_name(self.native())
        for title in (
            None,
            "../../escape",
            "$(touch injected)",
            "a" * 1000,
            "中文标题",
        ):
            with self.subTest(title=title):
                name = remote_task_name(self.native(name=title))
                self.assertEqual(name, expected)
                self.assertEqual(PurePosixPath(name).name, name)

    def test_run_and_attempt_cannot_alias(self):
        names = {
            remote_task_name(self.native(id=run, run_attempt=attempt))
            for run in (41367562498, 41367562499)
            for attempt in (1, 2)
        }
        self.assertEqual(len(names), 4)

    def test_native_integer_identities_are_strict(self):
        for field in ("id", "run_attempt"):
            for value in (True, False, 0, -1, "2", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(
                        ValueError, "native execution identity"
                    ):
                        remote_task_name(self.native(**{field: value}))

    def test_native_source_does_not_expand_repository_admission(self):
        for repo in (TORCHTITAN_REPO, "intruder/model", None):
            with self.subTest(repo=repo):
                with self.assertRaisesRegex(ValueError, "native execution repository"):
                    remote_task_name(self.native(_ci_repo=repo))

    def test_native_path_still_rejects_bad_case_and_timestamp(self):
        with self.assertRaisesRegex(ValueError, "Test ID"):
            remote_task_name(self.native(_ci_test_id="../outside"))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            remote_task_name(self.native(created_at="2026-11-02T00:30:56"))

    def test_legacy_torchtitan_and_a5_paths_are_unchanged(self):
        run = self.native(name="Dispatcher-Smoke", _ci_repo=TORCHTITAN_REPO)
        del run["_ci_source"]
        self.assertEqual(
            remote_task_name(run),
            "2026-11-02_08-30-56_run_Dispatcher-Smoke_7_attempt-2"
            "_test-dispatch_smoke_a3_8p",
        )
        run.update(
            _ci_repo=VLLM_REPO,
            name="lite-a5-manual",
            run_attempt=1,
            _ci_test_id="dispatch_smoke_a5_8p",
        )
        self.assertEqual(
            remote_task_name(run),
            "vllm_2026-11-02_08-30-56_run_lite-a5-manual_7_test-dispatch_smoke_a5_8p",
        )
        run["name"] = "../bad"
        with self.assertRaisesRegex(ValueError, "workflow name"):
            remote_task_name(run)


if __name__ == "__main__":
    unittest.main()
