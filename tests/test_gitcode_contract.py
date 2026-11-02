"""Admission fixtures represent API observations, not hardware/platform evidence."""

import copy
import unittest
from dataclasses import replace

from ci_core.gitcode_contract import (
    ContractError,
    ControlPolicy,
    aggregate_results,
    canonical,
    digest,
    external_id,
    load_json,
    request_id,
    seal_plan,
    select_cases,
    source_identity,
    spec_digest,
    verify_request,
    vllm_blue_catalog,
)


def fixture():
    policy = ControlPolicy(
        github_repo_id=1438625957,
        gitcode_repo_id="42",
        upstream_repo_id="43",
        upstream_default_ref="main",
        control_sha="c" * 40,
        workflow_id=91,
        workflow_path=".github/workflows/lite_actions_dispatcher_smoke.yml",
        app_id=81,
        allowed_actors=("depeng1994",),
        allowed_base_refs=("main",),
        policy_version="v1",
        environment_version="a3-v1",
        git_builder_version="git-2.43.0",
        runner_sha256="d" * 64,
        enabled=True,
    )
    pr = {
        "repo_id": "42",
        "number": 1,
        "head_repo_id": "42",
        "head_ref": "change",
        "head_sha": "a" * 40,
        "base_repo_id": "42",
        "base_ref": "main",
        "base_sha": "b" * 40,
    }
    control = {
        "upstream_repo_id": "43",
        "upstream_default_ref": "main",
        "upstream_control_sha": "c" * 40,
        "github_repo_id": 1438625957,
        "default_ref": "main",
        "control_sha": "c" * 40,
        "runner_sha256": "d" * 64,
    }
    gh = {
        "repo": "cann-framework/vllm-ascend",
        "repo_id": 1438625957,
        "workflow_id": 91,
        "workflow_path": policy.workflow_path,
        "run_id": 100,
        "run_attempt": 1,
        "prepare_job_id": 101,
    }
    bridge = {
        "bridge_event_id": "event-1",
        "bridge_action_run": "action-1",
        "bridge_attempt": 1,
        "actor": "depeng1994",
        "bridge_nonce": "nonce-1",
        "source_snapshot_id": "snapshot-1",
        "dispatch_intent_id": "intent-1",
        "rerun_intent_id": "initial-1",
        "event_created_at": 1000,
    }
    selectors = [
        {"test_id": "vllm_worker_os_blue"},
        {"test_id": "dispatch_smoke_a3_8p"},
    ]
    plan = {
        "schema": 4,
        "source_provider": "gitcode",
        "test_mode": "merge_result",
        "pr": pr,
        "control": control,
        "github": gh,
        "bridge": bridge,
        "pipeline": "smoke",
        "policy_version": "v1",
        "environment_version": "a3-v1",
        "deadline": 5000,
        "merge": {
            "policy": "git-ort-v1",
            "builder_version": "git-2.43.0",
            "tested_merge_sha": "e" * 40,
            "tested_tree_sha": "f" * 40,
        },
        "cases": select_cases(selectors, vllm_blue_catalog()),
        "result_slots": {},
        "artifacts": {},
    }
    intent = {
        "schema": 1,
        "source_provider": "gitcode",
        "pr": pr,
        "control_repo_id": gh["repo_id"],
        "control_sha": control["control_sha"],
        "workflow_path": gh["workflow_path"],
        "pipeline": "smoke",
        "selectors": selectors,
        "bridge": bridge,
        "deadline": 5000,
    }
    manifest = {
        "schema": 2,
        "source_provider": "gitcode",
        "test_mode": "merge_result",
        **source_identity(plan),
        "github_run_id": 100,
        "run_attempt": 1,
        "source_artifact_id": 202,
        "bundle_sha256": "1" * 64,
        "dependencies": [],
    }
    prefixes = {
        "intent": "dispatch-intent",
        "source": "gitcode-merge-source",
        "runner": "trusted-runner",
        "manifest": "source-manifest",
    }
    hashes = {
        "intent": digest(intent),
        "source": manifest["bundle_sha256"],
        "runner": control["runner_sha256"],
        "manifest": digest(manifest),
    }
    observations = {}
    for index, role in enumerate(prefixes, 201):
        ref = {
            "id": index,
            "name": prefixes[role] + "-100-1",
            "size": 1000,
            "zip_sha256": str(index % 10) * 64,
            "content_sha256": hashes[role],
        }
        plan["artifacts"][role] = ref
        observations[role] = {
            **ref,
            "repo_id": gh["repo_id"],
            "run_id": 100,
            "run_attempt": 1,
            "expired": False,
        }
    plan = seal_plan(plan)
    for index, case_id in enumerate(
        ["aggregate"] + [c["case_id"] for c in plan["cases"]], 301
    ):
        plan["result_slots"][case_id] = {
            "id": index,
            "app_id": 81,
            "repo_id": gh["repo_id"],
            "control_sha": control["control_sha"],
            "run_id": 100,
            "run_attempt": 1,
            "case_id": case_id,
            "external_id": external_id(plan, case_id),
        }
    plan = seal_plan(plan)
    context = {
        "policy": policy,
        "catalog": vllm_blue_catalog(),
        "run": {
            "id": 100,
            "run_attempt": 1,
            "repository_id": gh["repo_id"],
            "head_sha": control["control_sha"],
            "head_branch": "main",
            "workflow_id": 91,
            "path": gh["workflow_path"],
            "event": "workflow_dispatch",
        },
        "prepare_job": {
            "id": 101,
            "run_id": 100,
            "run_attempt": 1,
            "name": "prepare-gitcode",
            "status": "completed",
            "conclusion": "success",
        },
        "artifacts": observations,
        "slots": copy.deepcopy(plan["result_slots"]),
        "manifest": manifest,
        "intent": intent,
        "current_pr": {"identity": pr, "state": "open", "authorized": True},
        "now": 1100,
    }
    return plan, context


def receipt_for(plan):
    return {
        "request_id": request_id(plan),
        "plan_digest": plan["plan_digest"],
        "request_spec_digest": plan["request_spec_digest"],
        "source_snapshot_id": plan["bridge"]["source_snapshot_id"],
        "dispatch_intent_id": plan["bridge"]["dispatch_intent_id"],
        "canonical_execution_id": "execution-1",
        "pr_generation": 1,
        "execution_generation": 1,
        "publication_revision": 1,
        "duplicate": False,
    }


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.ctx = fixture()

    def verify(self):
        return verify_request(self.plan, **self.ctx)

    def test_complete_request_and_immutable_snapshot(self):
        verified = self.verify()
        self.plan["pr"]["head_sha"] = "0" * 40
        self.assertEqual(verified.plan["pr"]["head_sha"], "a" * 40)

    def test_feature_disabled_by_default(self):
        self.ctx["policy"] = replace(self.ctx["policy"], enabled=False)
        with self.assertRaisesRegex(ContractError, "PROVIDER_DISABLED"):
            self.verify()

    def test_prepare_failure_cancel_pending_or_wrong_run_is_zero_admission(self):
        for patch in (
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
            {"status": "in_progress"},
            {"run_id": 99},
            {"run_attempt": 2},
        ):
            self.plan, self.ctx = fixture()
            self.ctx["prepare_job"].update(patch)
            with (
                self.subTest(patch=patch),
                self.assertRaisesRegex(ContractError, "PREPARE_NOT_SUCCESSFUL"),
            ):
                self.verify()

    def test_partial_expired_wrong_repo_or_corrupt_artifact_is_rejected(self):
        for patch in (
            {"expired": True},
            {"repo_id": 1210038933},
            {"run_attempt": 2},
            {"zip_sha256": "0" * 64},
            {"content_sha256": "0" * 64},
        ):
            self.plan, self.ctx = fixture()
            self.ctx["artifacts"]["source"].update(patch)
            with (
                self.subTest(patch=patch),
                self.assertRaisesRegex(ContractError, "ARTIFACT_NOT_READY"),
            ):
                self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["artifacts"].pop("runner")
        with self.assertRaises(ContractError):
            self.verify()

    def test_manifest_cannot_invent_head_base_tree_or_plan_digest(self):
        for field in ("head_sha", "base_sha"):
            self.plan, self.ctx = fixture()
            self.ctx["manifest"] = copy.deepcopy(self.ctx["manifest"])
            self.ctx["manifest"]["pr"][field] = "0" * 40
            with self.assertRaisesRegex(ContractError, "SOURCE_MANIFEST_MISMATCH"):
                self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["manifest"]["plan_digest"] = self.plan["plan_digest"]
        with self.assertRaises(ContractError):
            self.verify()

    def test_current_head_base_retarget_and_closed_pr_rejected(self):
        for field, value in (
            ("head_sha", "0" * 40),
            ("base_sha", "0" * 40),
            ("base_ref", "release"),
        ):
            self.plan, self.ctx = fixture()
            self.ctx["current_pr"] = copy.deepcopy(self.ctx["current_pr"])
            self.ctx["current_pr"]["identity"][field] = value
            with self.assertRaisesRegex(ContractError, "PR_VERSION_STALE"):
                self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["current_pr"]["state"] = "closed"
        with self.assertRaisesRegex(ContractError, "PR_VERSION_STALE"):
            self.verify()

    def test_default_branch_actual_control_and_native_rerun_are_enforced(self):
        for patch in (
            {"head_sha": "0" * 40},
            {"head_branch": "ci-branch"},
            {"repository_id": 1210038933},
            {"event": "pull_request"},
        ):
            self.plan, self.ctx = fixture()
            self.ctx["run"].update(patch)
            with self.assertRaisesRegex(ContractError, "ACTUAL_WORKFLOW_RUN_MISMATCH"):
                self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["run"]["run_attempt"] = 2
        with self.assertRaisesRegex(ContractError, "USE_TRUSTED_BRIDGE"):
            self.verify()

    def test_fixed_container_cannot_be_overridden_even_with_resealed_plan(self):
        self.plan["cases"][1]["docker"] = "cann_master_torchtitan_master_xyj"
        self.plan = seal_plan(self.plan)
        with self.assertRaisesRegex(ContractError, "UNTRUSTED_CASE_RESOURCES"):
            self.verify()

    def test_probe_is_resource_free_and_a5_is_not_a_gitcode_case(self):
        catalog = vllm_blue_catalog()
        probe = select_cases([{"test_id": "vllm_worker_os_blue"}], catalog)[0]
        self.assertIsNone(probe["docker"])
        self.assertIsNone(probe["target_pool"])
        self.assertEqual(probe["resources"], {"nodes": 0, "devices_per_node": 0})
        with self.assertRaisesRegex(ContractError, "UNKNOWN_CASE"):
            select_cases([{"test_id": "dispatch_smoke_a5_8p"}], catalog)
        with self.assertRaisesRegex(ContractError, "OVERRIDES_FORBIDDEN"):
            select_cases(
                [{"test_id": "dispatch_smoke_a3_8p", "docker": "other"}], catalog
            )

    def test_slot_wrong_app_deleted_or_swapped_is_rejected(self):
        for patch in (
            {"app_id": 999},
            {"repo_id": 1210038933},
            {"control_sha": "0" * 40},
            {"id": 999},
            {"external_id": "other"},
        ):
            self.plan, self.ctx = fixture()
            self.ctx["slots"]["aggregate"].update(patch)
            with self.assertRaisesRegex(ContractError, "RESULT_SLOT_IDENTITY_MISMATCH"):
                self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["slots"].pop("aggregate")
        with self.assertRaises(ContractError):
            self.verify()

    def test_deadline_actor_and_intent_identity(self):
        self.ctx["now"] = 5000
        with self.assertRaisesRegex(ContractError, "DEADLINE"):
            self.verify()
        self.plan, self.ctx = fixture()
        self.ctx["intent"] = copy.deepcopy(self.ctx["intent"])
        self.ctx["intent"]["bridge"]["bridge_nonce"] = "another"
        with self.assertRaisesRegex(ContractError, "DISPATCH_INTENT_MISMATCH"):
            self.verify()

    def test_spec_digest_ignores_delivery_but_includes_selection_and_base(self):
        original = spec_digest(self.plan)
        changed = copy.deepcopy(self.plan)
        changed["github"]["run_id"] = 999
        changed["bridge"]["bridge_nonce"] = "new"
        changed["artifacts"] = {}
        self.assertEqual(original, spec_digest(changed))
        changed["pr"]["base_sha"] = "0" * 40
        self.assertNotEqual(original, spec_digest(changed))
        changed = copy.deepcopy(self.plan)
        changed["cases"] = select_cases(
            [{"test_id": "dispatch_smoke_a3_16p"}], vllm_blue_catalog()
        )
        self.assertNotEqual(original, spec_digest(changed))

    def test_runner_digest_must_match_the_reviewed_control_release(self):
        self.plan["control"]["runner_sha256"] = "0" * 64
        self.plan = seal_plan(self.plan)
        with self.assertRaisesRegex(ContractError, "CONTROL_RELEASE_MISMATCH"):
            self.verify()

    def test_bool_cannot_masquerade_as_run_id(self):
        self.plan["github"]["run_id"] = True
        self.plan = seal_plan(self.plan)
        with self.assertRaisesRegex(ContractError, "INVALID_INTEGER"):
            self.verify()

    def test_json_duplicates_nonfinite_and_budget(self):
        for payload in (b'{"a":1,"a":2}', b'{"x":NaN}', b"[]"):
            with self.assertRaises(ContractError):
                load_json(payload)
        with self.assertRaisesRegex(ContractError, "JSON_SIZE_LIMIT"):
            canonical({"x": "a" * 300_000})

    def test_all_cases_cleanup_and_version_required_for_pass(self):
        receipt = receipt_for(self.plan)
        results = [
            {
                "case_id": c["case_id"],
                "receipt": receipt,
                "identity": source_identity(self.plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": [],
            }
            for c in self.plan["cases"]
        ]
        output = aggregate_results(self.plan, receipt, results, current=True)
        self.assertEqual(output["test_result"], "PASS")
        self.assertEqual(output["merge_gate"], "NOT_PROVIDED")
        self.assertEqual(
            aggregate_results(self.plan, receipt, results[:-1], current=True)[
                "test_result"
            ],
            "PENDING",
        )
        self.assertEqual(
            aggregate_results(self.plan, receipt, results, current=False)[
                "test_result"
            ],
            "STALE",
        )
        results[0]["cleanup_verified"] = False
        self.assertEqual(
            aggregate_results(self.plan, receipt, results, current=True)["test_result"],
            "FAIL",
        )

    def test_conflicting_receipts_duplicate_case_and_skip_cannot_pass(self):
        receipt = receipt_for(self.plan)
        result = {
            "case_id": self.plan["cases"][0]["case_id"],
            "receipt": receipt,
            "identity": source_identity(self.plan),
            "status": "PASS",
            "exit_code": 0,
            "cleanup_verified": True,
            "summary": [],
        }
        with self.assertRaisesRegex(ContractError, "CONFLICTING"):
            aggregate_results(self.plan, receipt, [result, result], current=True)
        result["status"] = "SKIPPED"
        with self.assertRaisesRegex(ContractError, "INVALID_CASE_RESULT"):
            aggregate_results(self.plan, receipt, [result], current=True)
        result["status"] = "PASS"
        result["receipt"] = {**receipt, "pr_generation": 99}
        with self.assertRaisesRegex(ContractError, "EXECUTION_IDENTITY_MISMATCH"):
            aggregate_results(self.plan, receipt, [result], current=True)

    def test_duplicate_receipt_requires_following_canonical(self):
        receipt = receipt_for(self.plan)
        receipt["duplicate"] = True
        with self.assertRaisesRegex(ContractError, "FOLLOW_CANONICAL"):
            aggregate_results(self.plan, receipt, [], current=True)


if __name__ == "__main__":
    unittest.main()
