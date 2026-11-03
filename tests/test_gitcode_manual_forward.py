"""Manual hosted boundary tests, synthetic APIs only; no live dispatch or devices."""

import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.gitcode_checks import CheckRunChannel, CreationJournal
from ci_core.gitcode_manual import aggregate, capture, common, dispatch
from ci_core.gitcode_manual.common import ManualForwardError

CAPTURE_BOOTSTRAP_SCRIPT = (
    "python3 -I -B - <<'PY'\n# trusted SDK bootstrap fixture\nPY\n"
)
CAPTURE_BOOTSTRAP_SHA256 = hashlib.sha256(CAPTURE_BOOTSTRAP_SCRIPT.encode()).hexdigest()

S = "a" * 40
H = "b" * 40


def fake_plan_and_receipt(intent, run_id=42):
    case_ids = [dispatch_module_case_id(row["test_id"]) for row in intent["selectors"]]
    plan = {
        "schema": 4,
        "source_provider": "gitcode",
        "test_mode": "merge_result",
        "github": {
            "repo": common.GH_REPO,
            "repo_id": common.GH_ID,
            "workflow_id": common.GH_WORKFLOW_ID,
            "workflow_path": common.GH_WORKFLOW,
            "run_id": run_id,
            "run_attempt": 1,
            "prepare_job_id": 31,
        },
        "control": {"control_sha": intent["control_sha"]},
        "pr": intent["pr"],
        "merge": {
            "base_sha": S,
            "head_sha": H,
            "merge_sha": "c" * 40,
            "parents": [S, H],
        },
        "bridge": intent["bridge"],
        "pipeline": intent["pipeline"],
        "policy_version": "test-policy-v1",
        "environment_version": "test-env-v1",
        "deadline": intent["deadline"],
        "cases": [{"case_id": case_id, "kind": "smoke"} for case_id in case_ids],
    }
    spec_payload = {
        "pr": plan["pr"],
        "merge": plan["merge"],
        "control": plan["control"],
        "workflow_id": plan["github"]["workflow_id"],
        "workflow_path": plan["github"]["workflow_path"],
        "pipeline": plan["pipeline"],
        "policy_version": plan["policy_version"],
        "environment_version": plan["environment_version"],
        "cases": plan["cases"],
    }
    plan["request_spec_digest"] = hashlib.sha256(
        common.canonical(spec_payload)
    ).hexdigest()
    plan["result_slots"] = {}
    for index, case_id in enumerate(["aggregate", *case_ids]):
        slot = dispatch.expected_slot(plan, case_id)
        plan["result_slots"][case_id] = {"id": 7000 + index, **slot}
    unsigned = dict(plan)
    plan["plan_digest"] = hashlib.sha256(common.canonical(unsigned)).hexdigest()
    receipt = {
        "request_id": f"{common.GH_ID}/{run_id}/1",
        "plan_digest": plan["plan_digest"],
        "request_spec_digest": plan["request_spec_digest"],
        "source_snapshot_id": intent["bridge"]["source_snapshot_id"],
        "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
        "canonical_execution_id": "execution-1",
        "pr_generation": 1,
        "execution_generation": 2,
        "publication_revision": 3,
        "duplicate": False,
    }
    return plan, receipt


def fake_check(plan, receipt, case_id, check_id):
    slot = plan["result_slots"][case_id]
    run_id = plan["github"]["run_id"]
    envelope = {
        "schema": 4,
        "phase": "ADMITTED" if case_id == "aggregate" else "CASE_RESULT",
        "plan_digest": plan["plan_digest"],
        "receipt": receipt,
    }
    conclusion = None
    status = "in_progress"
    if case_id != "aggregate":
        result = {
            "case_id": case_id,
            "receipt": receipt,
            "identity": {
                "pr": plan["pr"],
                "merge": plan["merge"],
                "control": plan["control"],
            },
            "status": "PASS",
            "exit_code": 0,
            "cleanup_verified": True,
            "summary": [case_id + " passed"],
        }
        envelope["result"] = result
        conclusion = "success"
        status = "completed"
    return {
        "id": check_id,
        "name": f"lite-v4-{run_id}-1-{case_id}-{plan['request_spec_digest'][:12]}",
        "head_sha": plan["control"]["control_sha"],
        "app": {"id": common.CHECKS_APP_ID},
        "external_id": slot["external_id"],
        "details_url": f"https://github.com/{common.GH_REPO}/actions/runs/{run_id}",
        "html_url": f"https://github.com/{common.GH_REPO}/runs/{check_id}",
        "url": f"https://api.github.com/repos/{common.GH_REPO}/check-runs/{check_id}",
        "status": status,
        "conclusion": conclusion,
        "output": {
            "summary": json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        },
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.run = {
            "workflow_run_id": "run1",
            "file_path": common.GC_WORKFLOW,
            "workflow_id": "wf1",
            "head_sha": S,
            "head_branch": "master",
            "event": "Manual",
            "attempt": 1,
            "exist_in_default_branch": True,
            "status": "RUNNING",
            "start_time": 990000,
        }
        self.pr = {
            "number": 2,
            "state": "open",
            "merged_at": None,
            "user": {"login": "depeng1994"},
            "head": {"ref": "feature", "sha": H, "repo": {"full_name": common.GC_REPO}},
            "base": {
                "ref": "master",
                "sha": "c" * 40,
                "repo": {"full_name": common.GC_REPO},
            },
        }
        script = CAPTURE_BOOTSTRAP_SCRIPT
        self.job = {
            "id": "job1",
            "identifier": "manual_capture",
            "status": "COMPLETED",
            "is_shadow": False,
            "steps": [
                {
                    "id": "step1",
                    "identifier": "1",
                    "name": "Capture manual request",
                    "job_run_id": "job1",
                    "status": "COMPLETED",
                    "task": "official_shell",
                    "start_time": 995000,
                    "end_time": 1001000,
                    "inputs": [
                        {"key": "script", "value": script},
                        {"key": "shell", "value": "bash"},
                        {"key": "working-directory", "value": "."},
                    ],
                }
            ],
        }
        self.log = ""
        self.gh_run = None
        self.posts = 0
        self.lose_response = False
        self.drop_dispatch = False
        self.intent = None
        self.plan = None
        self.receipt = None
        self.plan_archive = None
        self.artifact_metadata = None
        self.case_checks = []
        self.fixed_checks = {}
        self.extra_runs = {}
        self.extra_jobs = {}
        self.extra_artifacts = {}
        self.extra_archives = {}
        self.fail_cases = set()
        self.not_run_cases = set()
        self.github_run_conclusion = "success"
        self.completed_jobs = [
            {
                "id": 31 if name == "prepare-gitcode" else 32,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": 42,
                "run_attempt": 1,
            }
            for name in (
                "prepare-gitcode",
                "wait-gitcode",
                "Lite-Actions-GitCode-Smoke",
            )
        ]

    def gc(self, suffix="", *, version=5, body=None):
        self.calls.append(("gc", suffix, body))
        if suffix == "":
            return {
                "id": 9075802,
                "full_name": common.GC_REPO,
                "default_branch": "master",
            }
        if suffix == "/branches/master":
            return {"name": "master", "commit": {"id": S}}
        if suffix == "/branches/feature":
            return {"name": "feature", "commit": {"id": H}}
        if suffix == "/pulls/2":
            return copy.deepcopy(self.pr)
        if suffix == "/actions/runs/run1":
            return copy.deepcopy(self.run)
        if suffix == "/actions/runs/run1/jobs":
            return {"total_count": 1, "jobs": [copy.deepcopy(self.job)]}
        if suffix == "/actions/runs/run1/jobs/job1":
            return copy.deepcopy(self.job)
        if suffix == "/actions/runs/run1/jobs/job1/logs":
            return {
                "start_offset": 0,
                "end_offset": len(self.log),
                "has_more": False,
                "log": self.log,
            }
        raise AssertionError(suffix)

    def download_authenticated_artifact(self, artifact_id):
        if artifact_id in self.extra_archives:
            return self.extra_archives[artifact_id]
        assert artifact_id == 77
        return self.plan_archive

    def gh(self, suffix, *, body=None, dispatch=False, authenticated_read=False):
        self.calls.append(("gh", suffix, body))
        if suffix == "":
            return {
                "id": common.GH_ID,
                "full_name": common.GH_REPO,
                "default_branch": "master",
            }
        if suffix == "/branches/master":
            return {"name": "master", "commit": {"sha": S}}
        if suffix == f"/actions/workflows/{common.GH_WORKFLOW_ID}":
            return {
                "id": common.GH_WORKFLOW_ID,
                "path": common.GH_WORKFLOW,
                "state": "active",
            }
        if suffix.startswith(f"/actions/workflows/{common.GH_WORKFLOW_ID}/runs?"):
            rows = [] if self.gh_run is None else [copy.deepcopy(self.gh_run)]
            return {"total_count": len(rows), "workflow_runs": rows}
        if suffix.endswith("/dispatches"):
            assert dispatch
            self.posts += 1
            intent = json.loads(body["inputs"]["gitcode_request"])
            self.intent = copy.deepcopy(intent)
            self.plan, self.receipt = fake_plan_and_receipt(intent)
            raw_plan = common.canonical(self.plan)
            archive_io = io.BytesIO()
            with zipfile.ZipFile(archive_io, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ci-request.json", raw_plan)
            self.plan_archive = archive_io.getvalue()
            self.artifact_metadata = {
                "id": 77,
                "name": "lite-ci-request-1",
                "expired": False,
                "size_in_bytes": len(self.plan_archive),
                "digest": "sha256:" + hashlib.sha256(self.plan_archive).hexdigest(),
                "workflow_run": {
                    "id": 42,
                    "repository_id": common.GH_ID,
                    "head_sha": S,
                    "head_branch": "master",
                },
            }
            self.case_checks = [
                fake_check(self.plan, self.receipt, case_id, slot["id"])
                for case_id, slot in self.plan["result_slots"].items()
            ]
            for check in self.case_checks:
                if any(case_id in check.get("name", "") for case_id in self.fail_cases):
                    envelope = json.loads(check["output"]["summary"])
                    envelope["result"]["status"] = "FAIL"
                    envelope["result"]["exit_code"] = 1
                    check["output"]["summary"] = json.dumps(
                        envelope, sort_keys=True, separators=(",", ":")
                    )
                    check["conclusion"] = "failure"
                if any(
                    case_id in check.get("name", "") for case_id in self.not_run_cases
                ):
                    check["status"] = "in_progress"
                    check["conclusion"] = None
            if not self.drop_dispatch:
                self.gh_run = {
                    "id": 42,
                    "run_attempt": 1,
                    "event": "workflow_dispatch",
                    "head_sha": S,
                    "head_branch": "master",
                    "workflow_id": common.GH_WORKFLOW_ID,
                    "display_title": "lite-gitcode:" + intent["bridge"]["bridge_nonce"],
                    "repository": {"id": common.GH_ID, "full_name": common.GH_REPO},
                    "actor": {"login": common.ALLOWED_ACTOR},
                    "path": common.GH_WORKFLOW,
                    "status": "completed",
                    "conclusion": self.github_run_conclusion,
                }
            if self.lose_response:
                raise TimeoutError()
            return {
                "workflow_run_id": 42,
                "run_url": f"https://api.github.com/repos/{common.GH_REPO}/actions/runs/42",
                "html_url": f"https://github.com/{common.GH_REPO}/actions/runs/42",
            }
        if suffix.startswith(f"/commits/{S}/check-runs?"):
            return {
                "total_count": len(self.case_checks),
                "check_runs": copy.deepcopy(self.case_checks),
            }
        if suffix == "/actions/runs/42/jobs?filter=latest&per_page=100":
            return {
                "total_count": len(self.completed_jobs),
                "jobs": copy.deepcopy(self.completed_jobs),
            }
        if suffix.startswith("/actions/runs/42/artifacts?"):
            return {
                "total_count": 1,
                "artifacts": [{"id": 77, "name": "lite-ci-request-1"}],
            }
        if suffix == "/actions/artifacts/77":
            return copy.deepcopy(self.artifact_metadata)
        if suffix.startswith("/check-runs/"):
            check_id = int(suffix.rsplit("/", 1)[1])
            if check_id in self.fixed_checks:
                return copy.deepcopy(self.fixed_checks[check_id])
            for check in self.case_checks:
                if check["id"] == check_id:
                    return copy.deepcopy(check)
            raise AssertionError(suffix)
        if suffix.startswith("/actions/runs/") and "/artifacts?" in suffix:
            run_id = int(suffix.split("/actions/runs/", 1)[1].split("/", 1)[0])
            artifact = self.extra_artifacts.get(run_id)
            if artifact is None:
                raise AssertionError(suffix)
            return {
                "total_count": 1,
                "artifacts": [{"id": artifact["id"], "name": "lite-ci-request-1"}],
            }
        if suffix.startswith("/actions/artifacts/"):
            artifact_id = int(suffix.rsplit("/", 1)[1])
            for artifact in self.extra_artifacts.values():
                if artifact["id"] == artifact_id:
                    return copy.deepcopy(artifact)
            raise AssertionError(suffix)
        if suffix.startswith("/actions/runs/") and suffix.endswith(
            "/jobs?filter=latest&per_page=100"
        ):
            run_id = int(suffix.split("/actions/runs/", 1)[1].split("/", 1)[0])
            if run_id in self.extra_jobs:
                jobs = self.extra_jobs[run_id]
                return {"total_count": len(jobs), "jobs": copy.deepcopy(jobs)}
        if suffix == "/actions/runs/42":
            return copy.deepcopy(self.gh_run)
        if suffix.startswith("/actions/runs/"):
            run_id = int(suffix.rsplit("/", 1)[1])
            if run_id in self.extra_runs:
                return copy.deepcopy(self.extra_runs[run_id])
        if suffix.startswith("/actions/runs/42/jobs?"):
            return {
                "total_count": len(self.completed_jobs),
                "jobs": copy.deepcopy(self.completed_jobs),
            }
        raise AssertionError(suffix)


def dispatch_module_case_id(test_id):
    return {
        "worker_os_blue": "case_000_worker_os_blue",
        "dispatch_smoke_8p": "case_001_dispatch_smoke_8p",
        "dispatch_smoke_16p": "case_002_dispatch_smoke_16p",
    }[test_id]


class SDKCheckProducerAPI:
    repo_id = common.GH_ID
    repo = common.GH_REPO

    def __init__(self, start_id=9100):
        self.checks = {}
        self.next_id = start_id

    def request(self, method, path, data=None):
        if method == "POST" and path == "/check-runs":
            check_id = self.next_id
            self.next_id += 1
            check = copy.deepcopy(data)
            check.update(
                id=check_id,
                app={"id": 5575236},
                conclusion=None,
                html_url=f"https://github.com/{self.repo}/runs/{check_id}",
                url=f"https://api.github.com/repos/{self.repo}/check-runs/{check_id}",
            )
            self.checks[check_id] = check
            return copy.deepcopy(check)
        check_id = int(path.rsplit("/", 1)[1])
        if check_id not in self.checks:
            raise AssertionError(path)
        if method == "PATCH":
            self.checks[check_id].update(copy.deepcopy(data))
        elif method != "GET":
            raise AssertionError((method, path))
        return copy.deepcopy(self.checks[check_id])


class HostedManualTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.env = {
            "EVENT_NAME": "Manual",
            "ATOMGIT_EVENT_NAME": "Manual",
            "RUN_ATTEMPT": "1",
            "ATOMGIT_RUN_ID": "run1",
            "REPOSITORY": common.GC_REPO,
            "REPOSITORY_ID": common.GC_ID,
            "REF": "refs/heads/master",
            "ACTOR": "depeng1994",
            "CONTROL_SHA": S,
            "PR_NUMBER": "2",
            "TEST_CASE": "os_8p_16p",
        }
        self.now = 1000
        self.patch = patch.object(
            dispatch, "CAPTURE_SCRIPT_SHA256", CAPTURE_BOOTSTRAP_SHA256
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def sdk_plan_and_producer(self, intent, run_id, bridge=None, start_id=9100):
        from ci_core.gitcode_contract import canonical as sdk_canonical

        source_intent = copy.deepcopy(intent)
        if bridge is not None:
            source_intent["bridge"] = copy.deepcopy(bridge)
        source_intent["deadline"] = max(
            source_intent["deadline"], source_intent["bridge"]["event_created_at"] + 900
        )
        plan, _ = fake_plan_and_receipt(source_intent, run_id=run_id)
        producer_api = SDKCheckProducerAPI(start_id=start_id)
        journal_dir = tempfile.TemporaryDirectory()
        self.addCleanup(journal_dir.cleanup)
        channel = CheckRunChannel(
            producer_api,
            repo_id=common.GH_ID,
            app_id=5575236,
            journal=CreationJournal(Path(journal_dir.name) / "checks.sqlite3"),
        )
        plan["result_slots"] = channel.precreate(plan)
        unsigned = dict(plan)
        unsigned.pop("plan_digest", None)
        plan["plan_digest"] = hashlib.sha256(sdk_canonical(unsigned)).hexdigest()
        receipt = {
            "request_id": f"{common.GH_ID}/{run_id}/1",
            "plan_digest": plan["plan_digest"],
            "request_spec_digest": plan["request_spec_digest"],
            "source_snapshot_id": source_intent["bridge"]["source_snapshot_id"],
            "dispatch_intent_id": source_intent["bridge"]["dispatch_intent_id"],
            "canonical_execution_id": "sdk-canonical-execution-1",
            "pr_generation": 7,
            "execution_generation": 8,
            "publication_revision": 9,
            "duplicate": False,
        }
        return source_intent, plan, receipt, channel, producer_api

    def publish_sdk_canonical(self, intent, run_id=41):
        from ci_core.gitcode_contract import canonical as sdk_canonical
        from ci_core.gitcode_contract import source_identity

        canonical_bridge = {
            "bridge_event_id": "manual:canonical-capture-run",
            "bridge_action_run": "canonical-capture-run",
            "bridge_attempt": 1,
            "actor": common.ALLOWED_ACTOR,
            "bridge_nonce": "c" * 32,
            "source_snapshot_id": "manual-capture:" + "c" * 32,
            "dispatch_intent_id": "manual-intent:" + "c" * 32,
            "rerun_intent_id": "initial",
            "event_created_at": 900,
        }
        canonical_intent, plan, receipt, channel, producer_api = (
            self.sdk_plan_and_producer(intent, run_id, bridge=canonical_bridge)
        )
        channel.publish_receipt(plan, receipt)
        self.api.fixed_checks.update(producer_api.checks)
        admitted_receipt, admitted_pointer = dispatch.read_admission_record(
            self.api, plan, canonical_intent
        )
        self.assertEqual(admitted_receipt, receipt)
        self.assertIsNone(admitted_pointer)
        results = []
        for case in plan["cases"]:
            result = {
                "case_id": case["case_id"],
                "receipt": receipt,
                "identity": source_identity(plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": ["SDK CheckRunChannel producer PASS fixture"],
            }
            channel.publish_case(plan, receipt, result)
            results.append(result)
        channel.publish_aggregate(plan, receipt, results, current=True)
        self.api.fixed_checks.update(producer_api.checks)
        run = {
            "id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_sha": S,
            "head_branch": "master",
            "workflow_id": common.GH_WORKFLOW_ID,
            "display_title": "lite-gitcode:" + canonical_bridge["bridge_nonce"],
            "repository": {"id": common.GH_ID, "full_name": common.GH_REPO},
            "actor": {"login": common.ALLOWED_ACTOR},
            "path": common.GH_WORKFLOW,
            "status": "completed",
            "conclusion": "success",
        }
        jobs = [
            {
                "id": plan["github"]["prepare_job_id"],
                "name": "prepare-gitcode",
                "status": "completed",
                "conclusion": "success",
                "run_id": run_id,
                "run_attempt": 1,
            },
            {
                "id": 4102,
                "name": "wait-gitcode",
                "status": "completed",
                "conclusion": "success",
                "run_id": run_id,
                "run_attempt": 1,
            },
            {
                "id": 4103,
                "name": "Lite-Actions-GitCode-Smoke",
                "status": "completed",
                "conclusion": "success",
                "run_id": run_id,
                "run_attempt": 1,
            },
        ]
        archive_io = io.BytesIO()
        with zipfile.ZipFile(archive_io, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ci-request.json", sdk_canonical(plan))
        archive = archive_io.getvalue()
        artifact_id = 4400
        self.api.extra_runs[run_id] = run
        self.api.extra_jobs[run_id] = jobs
        self.api.extra_archives[artifact_id] = archive
        self.api.extra_artifacts[run_id] = {
            "id": artifact_id,
            "name": "lite-ci-request-1",
            "expired": False,
            "size_in_bytes": len(archive),
            "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            "workflow_run": {
                "id": run_id,
                "repository_id": common.GH_ID,
                "head_sha": S,
                "head_branch": "master",
            },
        }
        return canonical_intent, plan, receipt, channel, producer_api, run

    def emit(self):
        self.api.log = (
            capture.capture(
                self.env,
                self.api,
                clock=lambda: self.now,
                nonce_factory=lambda: "n" * 32,
            )
            + "\n"
        )
        return dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def render_case(self, intent, plan, receipt, result, workflow_conclusion):
        encoded = dispatch.encode_case_evidence(
            intent,
            plan,
            receipt,
            result,
            control_matches=result.get("current_control_matches", True),
            workflow_conclusion=workflow_conclusion,
        )
        environment = {
            "RESULT_CASE": result["test_id"],
            "CASE_EVIDENCE": encoded,
            "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
            "CAPTURE_ATTEMPT": "1",
            "CONTROL_SHA": intent["control_sha"],
            "CAPTURE_RESULT": "success",
        }
        with patch("sys.stdout", io.StringIO()) as output:
            exit_code = dispatch.display_case_main(environment)
            payload = json.loads(output.getvalue())
        return exit_code, payload

    def test_capture_matches_real_sdk_emission_and_verifier(self):
        from ci_core.gitcode_manual_capture import (
            ManualCapturePolicy,
            encode_capture,
            verify_manual_capture,
        )

        intent = self.emit()
        self.assertEqual(self.api.log.strip(), encode_capture(intent, self.now))
        job = copy.deepcopy(self.api.job)
        step = job["steps"][0]
        step.update(
            script_sha256=CAPTURE_BOOTSTRAP_SHA256, shell="bash", working_directory="."
        )
        del step["inputs"]
        client = SimpleNamespace(
            repo_id=common.GC_ID,
            action_run=lambda _: copy.deepcopy(self.api.run),
            action_jobs=lambda _: [copy.deepcopy(job)],
            action_job=lambda *a: copy.deepcopy(job),
            action_step_logs=lambda *a, **k: {"complete": True, "log": self.api.log},
        )
        policy = ManualCapturePolicy(
            control=SimpleNamespace(
                gitcode_repo_id=common.GC_ID, control_sha=S, enabled=True
            ),
            workflow_id="wf1",
            workflow_path=common.GC_WORKFLOW,
            job_identifier="manual_capture",
            step_identifier="1",
            step_name="Capture manual request",
            script_sha256=CAPTURE_BOOTSTRAP_SHA256,
            shell="bash",
            working_directory=".",
            enabled=True,
        )
        proof = verify_manual_capture(intent, policy, client, now=self.now)
        self.assertEqual(proof.intent, intent)
        self.assertEqual(proof.capture_kind, "manual_api")
        self.assertEqual(intent["pr"]["base_sha"], S)  # never PR's stale base.sha
        self.assertEqual(
            intent["selectors"],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dispatch_smoke_16p"},
            ],
        )

    def test_capture_uses_only_gitcode_and_forward_revalidates_github(self):
        with patch.object(self.api, "gh", side_effect=AssertionError("unexpected GitHub read")):
            self.api.log = capture.capture(
                self.env, self.api, clock=lambda: self.now, nonce_factory=lambda: "n" * 32
            ) + "\n"
        with patch.object(self.api, "gh", side_effect=ManualForwardError("GITHUB_CONTROL_CHANGED")):
            with self.assertRaisesRegex(ManualForwardError, "GITHUB_CONTROL_CHANGED"):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        self.assertEqual(self.api.posts, 0)

    def test_manual_only_attempt_repo_actor_and_case_boundaries(self):
        for field, value in (
            ("EVENT_NAME", "MR"),
            ("ATOMGIT_EVENT_NAME", "MR"),
            ("RUN_ATTEMPT", "2"),
            ("REPOSITORY", "cann/torchtitan-npu"),
            ("ACTOR", "other"),
            ("PR_NUMBER", "2;echo leak"),
            ("TEST_CASE", "dispatch_smoke_8p"),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(common.ManualForwardError),
            ):
                capture.capture({**self.env, field: value}, self.api)

    def test_default_test_case_preserves_current_os_only_route(self):
        environment = dict(self.env)
        environment.pop("TEST_CASE")
        raw = capture.capture(
            environment,
            self.api,
            clock=lambda: self.now,
            nonce_factory=lambda: "n" * 32,
        )
        payload = __import__("base64").b64decode(raw.rsplit(" ", 1)[1])
        intent = json.loads(payload)["intent"]
        self.assertEqual(intent["selectors"], [{"test_id": "worker_os_blue"}])
        self.assertEqual(
            capture.capture(
                {**self.env, "TEST_CASE": "worker_os_blue"},
                self.api,
                clock=lambda: self.now,
                nonce_factory=lambda: "n" * 32,
            ).split(" ", 1)[1],
            raw.split(" ", 1)[1],
        )

    def test_capture_is_one_exact_three_case_intent(self):
        intent = self.emit()
        self.assertEqual(
            intent["selectors"],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dispatch_smoke_16p"},
            ],
        )
        for invalid in (
            [{"test_id": "worker_os_blue"}, {"test_id": "dispatch_smoke_8p"}],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dispatch_smoke_8p"},
            ],
            [
                {"test_id": "worker_os_blue"},
                {"test_id": "dispatch_smoke_8p"},
                {"test_id": "dsv4_flash_a3_8p_example"},
            ],
        ):
            changed = copy.deepcopy(intent)
            changed["selectors"] = invalid
            payload = common.canonical(
                {
                    "schema": 1,
                    "kind": "manual-current-observation",
                    "captured_at": self.now,
                    "intent": changed,
                }
            )
            self.api.log = (
                common.PREFIX
                + hashlib.sha256(payload).hexdigest()
                + " "
                + __import__("base64").b64encode(payload).decode()
                + "\n"
            )
            with (
                self.subTest(selectors=invalid),
                self.assertRaisesRegex(common.ManualForwardError, "CASE_NOT_ALLOWED"),
            ):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_real_sdk_producer_slot_created_uses_exact_five_field_envelope(self):
        intent = self.emit()
        _, plan, _, channel, producer_api = self.sdk_plan_and_producer(intent, 42)
        slot = channel.create_slot(plan, "aggregate")
        plan["result_slots"]["aggregate"] = slot
        unsigned = dict(plan)
        unsigned.pop("plan_digest", None)
        plan["plan_digest"] = hashlib.sha256(common.canonical(unsigned)).hexdigest()
        self.api.fixed_checks.update(producer_api.checks)
        check = producer_api.checks[slot["id"]]
        envelope = json.loads(check["output"]["summary"])
        self.assertEqual(
            set(envelope),
            {"schema", "phase", "request_id", "case_id", "request_spec_digest"},
        )
        self.assertEqual(envelope["phase"], "SLOT_CREATED")
        self.assertIsNone(dispatch.read_admission_record(self.api, plan, intent)[0])

    def test_real_sdk_producer_duplicate_follows_canonical_fixed_slots_and_generation(
        self,
    ):
        intent = self.emit()
        canonical_intent, canonical_plan, canonical_receipt, _, _, canonical_run = (
            self.publish_sdk_canonical(intent)
        )
        _, duplicate_plan, _, duplicate_channel, duplicate_api = (
            self.sdk_plan_and_producer(intent, 42, start_id=9200)
        )
        duplicate_receipt = {
            "request_id": f"{common.GH_ID}/42/1",
            "plan_digest": duplicate_plan["plan_digest"],
            "request_spec_digest": duplicate_plan["request_spec_digest"],
            "source_snapshot_id": intent["bridge"]["source_snapshot_id"],
            "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
            "canonical_execution_id": canonical_receipt["canonical_execution_id"],
            "pr_generation": canonical_receipt["pr_generation"],
            "execution_generation": canonical_receipt["execution_generation"],
            "publication_revision": canonical_receipt["publication_revision"],
            "duplicate": True,
        }
        pointer = {
            "request_id": canonical_receipt["request_id"],
            "plan_digest": canonical_plan["plan_digest"],
            "github": canonical_plan["github"],
            "result_slots": canonical_plan["result_slots"],
        }
        duplicate_channel.publish_receipt(
            duplicate_plan, duplicate_receipt, canonical_execution=pointer
        )
        self.api.fixed_checks.update(duplicate_api.checks)

        observed_receipt, observed_pointer = dispatch.read_admission_record(
            self.api, duplicate_plan, intent
        )
        self.assertEqual(observed_receipt, duplicate_receipt)
        self.assertEqual(observed_pointer, pointer)
        followed_plan, followed_receipt, followed_intent, followed_run = (
            dispatch.resolve_canonical_execution(
                self.api, intent, duplicate_plan, observed_receipt, observed_pointer
            )
        )
        self.assertEqual(followed_plan["plan_digest"], canonical_plan["plan_digest"])
        self.assertEqual(followed_receipt, canonical_receipt)
        self.assertEqual(followed_run["id"], canonical_run["id"])
        self.assertEqual(followed_intent["bridge"], canonical_intent["bridge"])

        result = dispatch.case_check(
            self.api,
            followed_intent,
            followed_plan,
            followed_receipt,
            canonical_plan["github"]["run_id"],
            "worker_os_blue",
            run_conclusion=canonical_run["conclusion"],
            pr_matches=True,
        )
        encoded = dispatch.encode_case_evidence(
            intent,
            followed_plan,
            followed_receipt,
            result,
            control_matches=True,
            workflow_conclusion=canonical_run["conclusion"],
            current_plan=duplicate_plan,
            current_receipt=duplicate_receipt,
            canonical_pointer=pointer,
            canonical_receipt=canonical_receipt,
        )
        decoded = dispatch.decode_case_evidence(
            {
                "RESULT_CASE": "worker_os_blue",
                "CASE_EVIDENCE": encoded,
                "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
                "CAPTURE_ATTEMPT": "1",
                "CONTROL_SHA": intent["control_sha"],
            }
        )
        self.assertEqual(decoded["case_status"], "PASS")
        self.assertEqual(decoded["github_run_id"], canonical_plan["github"]["run_id"])
        self.assertEqual(
            decoded["identity"]["request_id"], duplicate_receipt["request_id"]
        )
        self.assertTrue(decoded["identity"]["receipt"]["duplicate"])
        self.assertEqual(
            decoded["check_id"],
            pointer["result_slots"][dispatch.CASE_ORDER["worker_os_blue"]]["id"],
        )

        bad_generation = {
            **duplicate_receipt,
            "execution_generation": duplicate_receipt["execution_generation"] + 1,
        }
        with self.assertRaisesRegex(
            common.ManualForwardError, "CANONICAL_RECEIPT_GENERATION_MISMATCH"
        ):
            dispatch.resolve_canonical_execution(
                self.api, intent, duplicate_plan, bad_generation, pointer
            )

    def test_duplicate_canonical_pointer_owner_and_midread_change_are_rejected(self):
        intent = self.emit()
        _, canonical_plan, canonical_receipt, _, _, _ = self.publish_sdk_canonical(
            intent
        )
        _, duplicate_plan, _, duplicate_channel, duplicate_api = (
            self.sdk_plan_and_producer(intent, 42, start_id=9200)
        )
        duplicate_receipt = {
            "request_id": f"{common.GH_ID}/42/1",
            "plan_digest": duplicate_plan["plan_digest"],
            "request_spec_digest": duplicate_plan["request_spec_digest"],
            "source_snapshot_id": intent["bridge"]["source_snapshot_id"],
            "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
            "canonical_execution_id": canonical_receipt["canonical_execution_id"],
            "pr_generation": canonical_receipt["pr_generation"],
            "execution_generation": canonical_receipt["execution_generation"],
            "publication_revision": canonical_receipt["publication_revision"],
            "duplicate": True,
        }
        pointer = {
            "request_id": canonical_receipt["request_id"],
            "plan_digest": canonical_plan["plan_digest"],
            "github": canonical_plan["github"],
            "result_slots": canonical_plan["result_slots"],
        }
        bad_owner = copy.deepcopy(pointer)
        bad_owner["github"]["repo_id"] = 999
        duplicate_channel.publish_receipt(
            duplicate_plan, duplicate_receipt, canonical_execution=bad_owner
        )
        self.api.fixed_checks.update(duplicate_api.checks)
        with self.assertRaisesRegex(
            common.ManualForwardError, "CANONICAL_POINTER_INVALID"
        ):
            dispatch.read_admission_record(self.api, duplicate_plan, intent)

        # Re-publish the correct pointer on a separate duplicate request fixture.
        _, duplicate_plan2, _, duplicate_channel2, duplicate_api2 = (
            self.sdk_plan_and_producer(intent, 43, start_id=9300)
        )
        duplicate_receipt2 = {
            **duplicate_receipt,
            "request_id": f"{common.GH_ID}/43/1",
            "plan_digest": duplicate_plan2["plan_digest"],
        }
        duplicate_channel2.publish_receipt(
            duplicate_plan2, duplicate_receipt2, canonical_execution=pointer
        )
        self.api.fixed_checks.update(duplicate_api2.checks)
        changed_pointer = copy.deepcopy(pointer)
        changed_pointer["github"]["run_id"] = 999
        with (
            patch.object(
                dispatch,
                "read_admission_record",
                side_effect=[
                    (canonical_receipt, None),
                    (duplicate_receipt2, changed_pointer),
                ],
            ),
            self.assertRaisesRegex(
                common.ManualForwardError, "CANONICAL_POINTER_CHANGED"
            ),
        ):
            dispatch.resolve_canonical_execution(
                self.api, intent, duplicate_plan2, duplicate_receipt2, pointer
            )

    def test_case_check_uses_immutable_plan_slot_and_full_receipt_identity(self):
        intent = self.emit()
        run_id = 42
        case_id = dispatch.CASE_ORDER["dispatch_smoke_16p"]
        spec = "d" * 64
        plan = {
            "github": {
                "repo": common.GH_REPO,
                "repo_id": common.GH_ID,
                "run_id": run_id,
                "run_attempt": 1,
            },
            "control": {"control_sha": intent["control_sha"]},
            "pr": intent["pr"],
            "merge": {"parents": ["a" * 40, "b" * 40]},
            "request_spec_digest": spec,
            "plan_digest": "e" * 64,
        }
        receipt = {
            "request_id": f"{common.GH_ID}/{run_id}/1",
            "plan_digest": plan["plan_digest"],
            "request_spec_digest": spec,
            "source_snapshot_id": intent["bridge"]["source_snapshot_id"],
            "dispatch_intent_id": intent["bridge"]["dispatch_intent_id"],
            "canonical_execution_id": "execution-1",
            "pr_generation": 3,
            "execution_generation": 4,
            "publication_revision": 7,
            "duplicate": False,
        }
        plan["result_slots"] = {
            case_id: {"id": 7654, **dispatch.expected_slot(plan, case_id)}
        }
        result = {
            "case_id": case_id,
            "receipt": receipt,
            "identity": {
                "pr": plan["pr"],
                "merge": plan["merge"],
                "control": plan["control"],
            },
            "status": "CANCELLED",
            "exit_code": 143,
            "cleanup_verified": True,
            "summary": ["cancelled by operator", "cleanup verified"],
        }
        envelope = {
            "schema": 4,
            "phase": "CASE_RESULT",
            "plan_digest": plan["plan_digest"],
            "receipt": receipt,
            "result": result,
        }
        check = {
            "id": 7654,
            "name": f"lite-v4-{run_id}-1-{case_id}-{spec[:12]}",
            "head_sha": intent["control_sha"],
            "app": {"id": common.CHECKS_APP_ID},
            "external_id": dispatch.expected_slot(plan, case_id)["external_id"],
            "details_url": f"https://github.com/{common.GH_REPO}/actions/runs/{run_id}",
            "html_url": f"https://github.com/{common.GH_REPO}/runs/7654",
            "url": f"https://api.github.com/repos/{common.GH_REPO}/check-runs/7654",
            "status": "completed",
            "conclusion": "cancelled",
            "output": {
                "summary": json.dumps(envelope, sort_keys=True, separators=(",", ":"))
            },
        }
        original = self.api.gh
        queried = []

        def by_fixed_id(suffix, **kwargs):
            queried.append(suffix)
            if suffix == "/check-runs/7654":
                return copy.deepcopy(check)
            return original(suffix, **kwargs)

        self.api.gh = by_fixed_id
        observed = dispatch.case_check(
            self.api,
            intent,
            plan,
            receipt,
            run_id,
            "dispatch_smoke_16p",
            pr_matches=False,
        )
        self.assertEqual(observed["case_status"], "CANCELLED")
        self.assertIn("cancelled by operator", observed["reason"])
        self.assertEqual(observed["execution_generation"], 4)
        self.assertFalse(observed["current_pr_matches"])
        self.assertTrue(observed["historical_only"])
        self.assertEqual(observed["current_authority"], "NOT_PROVIDED")
        self.assertEqual(
            observed["github_run_url"],
            f"https://github.com/{common.GH_REPO}/actions/runs/{run_id}",
        )
        self.assertEqual(observed["case_log_url"], check["html_url"])
        self.assertEqual(queried, ["/check-runs/7654"])

        check["app"] = {"id": 999}
        with self.assertRaisesRegex(common.ManualForwardError, "FULL_IDENTITY"):
            dispatch.case_check(
                self.api, intent, plan, receipt, run_id, "dispatch_smoke_16p"
            )
        check["app"] = {"id": common.CHECKS_APP_ID}

        result["status"] = "PASS"
        result["exit_code"] = 0
        result["cleanup_verified"] = False
        check["conclusion"] = "failure"
        check["output"]["summary"] = json.dumps(
            envelope, sort_keys=True, separators=(",", ":")
        )
        with self.assertRaisesRegex(
            common.ManualForwardError, "PASS_EVIDENCE_MISMATCH"
        ):
            dispatch.case_check(
                self.api, intent, plan, receipt, run_id, "dispatch_smoke_16p"
            )

        result["cleanup_verified"] = True
        check["conclusion"] = "failure"
        check["output"]["summary"] = json.dumps(
            envelope, sort_keys=True, separators=(",", ":")
        )
        with self.assertRaisesRegex(
            common.ManualForwardError, "PASS_EVIDENCE_MISMATCH"
        ):
            dispatch.case_check(
                self.api, intent, plan, receipt, run_id, "dispatch_smoke_16p"
            )

        result["cleanup_verified"] = False
        result["status"] = "CANCELLED"
        result["identity"]["merge"] = {"parents": ["wrong"]}
        check["conclusion"] = "cancelled"
        check["output"]["summary"] = json.dumps(
            envelope, sort_keys=True, separators=(",", ":")
        )
        with self.assertRaisesRegex(common.ManualForwardError, "EXECUTION_IDENTITY"):
            dispatch.case_check(
                self.api, intent, plan, receipt, run_id, "dispatch_smoke_16p"
            )

        result["identity"] = {
            "pr": plan["pr"],
            "merge": plan["merge"],
            "control": plan["control"],
        }
        result["receipt"] = {
            **receipt,
            "execution_generation": receipt["execution_generation"] + 1,
        }
        check["output"]["summary"] = json.dumps(
            envelope, sort_keys=True, separators=(",", ":")
        )
        with self.assertRaisesRegex(common.ManualForwardError, "EXECUTION_IDENTITY"):
            dispatch.case_check(
                self.api, intent, plan, receipt, run_id, "dispatch_smoke_16p"
            )

    def test_plan_artifact_nonce_tampering_is_rejected_even_with_resealed_digest(self):
        intent = self.emit()
        self.api.gh(
            f"/actions/workflows/{common.GH_WORKFLOW_ID}/dispatches",
            body={"inputs": {"gitcode_request": common.canonical(intent).decode()}},
            dispatch=True,
        )
        tampered = copy.deepcopy(self.api.plan)
        tampered["bridge"]["bridge_nonce"] = "z" * 32
        unsigned = dict(tampered)
        unsigned.pop("plan_digest")
        tampered["plan_digest"] = hashlib.sha256(common.canonical(unsigned)).hexdigest()
        archive_io = io.BytesIO()
        with zipfile.ZipFile(archive_io, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ci-request.json", common.canonical(tampered))
        self.api.plan_archive = archive_io.getvalue()
        self.api.artifact_metadata["size_in_bytes"] = len(self.api.plan_archive)
        self.api.artifact_metadata["digest"] = (
            "sha256:" + hashlib.sha256(self.api.plan_archive).hexdigest()
        )
        with self.assertRaisesRegex(
            common.ManualForwardError, "RUN_OR_CAPTURE_MISMATCH"
        ):
            dispatch.read_plan_artifact(self.api, intent, 42)

    def test_legacy_os_only_plan_marks_unselected_cases_not_run_without_failing_os(
        self,
    ):
        intent = self.emit()
        legacy = copy.deepcopy(intent)
        legacy["selectors"] = [{"test_id": "worker_os_blue"}]
        plan, receipt = fake_plan_and_receipt(legacy)
        observed = dispatch.case_check(
            self.api,
            legacy,
            plan,
            receipt,
            42,
            "dispatch_smoke_16p",
            pr_matches=True,
        )
        self.assertEqual(observed["case_status"], "NOT_RUN")
        self.assertIs(observed["selected"], False)
        self.assertEqual(dispatch.result_exit_code(observed), 0)
        self.assertEqual(
            dispatch.result_exit_code({"case_status": "CANCELLED", "selected": True}), 1
        )

    def test_result_job_marks_cancelled_as_failure_with_explicit_case_status(self):
        import io
        from contextlib import redirect_stdout

        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "EVENT_NAME": "Manual",
                    "ATOMGIT_EVENT_NAME": "Manual",
                    "RUN_ATTEMPT": "1",
                    "ATOMGIT_RUN_ID": "run1",
                    "REPOSITORY": common.GC_REPO,
                    "REPOSITORY_ID": common.GC_ID,
                    "REF": "refs/heads/master",
                    "ACTOR": common.ALLOWED_ACTOR,
                    "CONTROL_SHA": S,
                    "RESULT_CASE": "dispatch_smoke_16p",
                    "CAPTURE_RESULT": "failure",
                },
                clear=True,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(dispatch.result_main(), 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["case_status"], "NOT_RUN")
        self.assertIsNone(payload["github_run_url"])
        self.assertIn("Capture did not complete", payload["reason"])

    def test_historical_capture_read_does_not_reapply_enrollment_300s_freshness(self):
        intent = self.emit()
        self.now = 1400
        with self.assertRaisesRegex(common.ManualForwardError, "CAPTURE_AGE_INVALID"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        retained = dispatch.retrieve_intent(
            self.api, self.env, clock=lambda: self.now, historical=True
        )
        self.assertEqual(retained, intent)
        self.assertEqual(self.api.posts, 0)

    def test_historical_result_records_parent_cancellation_and_control_revocation(self):
        intent = self.emit()
        self.api.run["status"] = "COMPLETED"
        self.api.run["conclusion"] = "cancelled"
        self.assertEqual(
            dispatch.run_identity(self.api, intent["bridge"]["bridge_action_run"], S)[
                "conclusion"
            ],
            "cancelled",
        )
        self.api.pr["state"] = "closed"
        self.assertFalse(dispatch.current_pr_matches(self.api, intent))
        self.api.pr["state"] = "open"
        self.api.gc = lambda suffix="", **kwargs: (
            {"name": "master", "commit": {"id": "f" * 40}}
            if suffix == "/branches/master"
            else FakeAPI.gc(self.api, suffix, **kwargs)
        )
        self.assertFalse(dispatch.current_control_matches(self.api, S))

    def test_current_pr_observation_marks_closed_or_retargeted_as_historical(self):
        intent = self.emit()
        self.assertTrue(dispatch.current_pr_matches(self.api, intent))
        self.api.pr["state"] = "closed"
        self.assertFalse(dispatch.current_pr_matches(self.api, intent))
        self.api.pr["state"] = "open"
        self.api.pr["base"]["ref"] = "release"
        self.assertFalse(dispatch.current_pr_matches(self.api, intent))

    def test_immutable_plan_artifact_and_admission_receipt_are_run_nonce_bound(self):
        intent = self.emit()
        self.api.gh(
            f"/actions/workflows/{common.GH_WORKFLOW_ID}/dispatches",
            body={"inputs": {"gitcode_request": common.canonical(intent).decode()}},
            dispatch=True,
        )
        plan = dispatch.read_plan_artifact(self.api, intent, 42)
        receipt = dispatch.read_admission_receipt(self.api, plan, intent)
        self.assertEqual(
            plan["bridge"]["bridge_nonce"], intent["bridge"]["bridge_nonce"]
        )
        self.assertEqual(receipt["execution_generation"], 2)
        self.assertEqual(
            set(plan["result_slots"]),
            {
                "aggregate",
                "case_000_worker_os_blue",
                "case_001_dispatch_smoke_8p",
                "case_002_dispatch_smoke_16p",
            },
        )
        self.api.artifact_metadata["workflow_run"]["id"] = 43
        with self.assertRaisesRegex(common.ManualForwardError, "ARTIFACT_OWNER"):
            dispatch.read_plan_artifact(self.api, intent, 42)

    def test_capture_rejects_external_source_and_unstable_head(self):
        self.api.pr["head"]["repo"]["full_name"] = "other/fork"
        with self.assertRaisesRegex(
            common.ManualForwardError, "PERSONAL_SOURCE_TARGET"
        ):
            self.emit()
        self.api.pr["head"]["repo"]["full_name"] = common.GC_REPO
        self.api.pr["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(common.ManualForwardError, "CHANGED_DURING"):
            self.emit()

    def platform_steps(self):
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures/gitcode_b472_platform_steps.json"
            ).read_text()
        )
        steps = []
        for row in fixture["steps"]:
            step = dict.fromkeys(row["keys"])
            step.update(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"keys", "inputs_type", "env_type"}
                }
            )
            step["job_run_id"] = "job1"
            step["env"] = None
            if step["task"] == "official_shell":
                step["inputs"] = copy.deepcopy(self.api.job["steps"][0]["inputs"])
                step["start_time"], step["end_time"] = 995000, 1001000
            steps.append(step)
        return steps

    def test_real_platform_pre_user_post_envelope_selects_only_fixed_user_step(self):
        expected = self.emit()
        self.api.job["steps"] = self.platform_steps()
        self.assertEqual(
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now),
            expected,
        )

    def test_platform_step_envelope_rejects_payload_reorder_extra_and_wrong_owner(self):
        self.emit()
        original = self.platform_steps()
        for field, value in (
            ("inputs", [{"key": "script", "value": "evil"}]),
            ("inputs", []),
            ("env", []),
            ("condition", "true"),
            ("task", "official_shell"),
            ("job_run_id", "other"),
            ("name", "custom"),
            ("identifier", "0"),
            ("status", "FAILED"),
            ("continue-on-error", True),
            ("continue-on-error", 0),
            ("continue-on-error", "false"),
            ("script", "evil"),
        ):
            self.api.job["steps"] = copy.deepcopy(original)
            self.api.job["steps"][0][field] = value
            with (
                self.subTest(field=field),
                self.assertRaises(common.ManualForwardError),
            ):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        for missing in ("identifier", "inputs", "env", "condition", "icon_url"):
            self.api.job["steps"] = copy.deepcopy(original)
            del self.api.job["steps"][0][missing]
            with (
                self.subTest(missing=missing),
                self.assertRaises(common.ManualForwardError),
            ):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        for rows in (
            original[::-1],
            [*original, original[0]],
            [original[0], original[1], original[1]],
        ):
            self.api.job["steps"] = rows
            with self.assertRaises(common.ManualForwardError):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_user_step_continue_on_error_requires_exact_null_or_false(self):
        self.emit()
        for value in (0, "false", True):
            self.api.job["steps"][0]["continue-on-error"] = value
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(common.ManualForwardError, "IDENTITY_MISMATCH"),
            ):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_duplicate_marker_extra_step_or_command_change_rejected(self):
        self.emit()
        self.api.log *= 2
        with self.assertRaisesRegex(common.ManualForwardError, "DUPLICATE"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        self.emit()
        self.api.job["steps"].append(copy.deepcopy(self.api.job["steps"][0]))
        with self.assertRaisesRegex(common.ManualForwardError, "SINGLE_STEP"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        self.api.job["steps"].pop()
        self.api.job["steps"][0]["inputs"][0]["value"] += "#changed"
        with self.assertRaisesRegex(common.ManualForwardError, "NOT_PINNED"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_realistic_capture_dispatch_wait_uses_one_post_and_exact_jobs(self):
        intent = self.emit()
        result = dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(result["historical_execution_result"], "PASS")
        self.assertEqual(result["current_authority"], "NOT_PROVIDED")
        self.assertIs(result["not_current_merge_authorization"], True)
        self.assertNotIn("test_result", result)
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(result["merge_gate"], "NOT_PROVIDED")
        # Same immutable capture recovers same run rather than another POST.
        self.assertEqual(
            dispatch.forward(self.api, intent, clock=lambda: self.now), result
        )
        self.assertEqual(self.api.posts, 1)

    def test_exact_platform_timestamp_wrapper_is_supported_once_only(self):
        intent = self.emit()
        original = self.api.log
        stamp = "[2026-11-02 09:00:00.123+00:00] "
        self.api.log = stamp + original
        self.assertEqual(
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now), intent
        )
        for prefix in (stamp + stamp, "unexpected ", "unexpected " + stamp):
            self.api.log = prefix + original
            with self.assertRaisesRegex(
                common.ManualForwardError, "MISSING_OR_DUPLICATE"
            ):
                dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        self.api.log = stamp + original + original
        with self.assertRaisesRegex(common.ManualForwardError, "MISSING_OR_DUPLICATE"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_reassigned_github_repository_is_rejected_before_dispatch(self):
        intent = self.emit()
        original = self.api.gh

        def changed(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if suffix == "":
                result["id"] += 1
            return result

        self.api.gh = changed
        with self.assertRaises(common.ManualForwardError):
            dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(self.api.posts, 0)

    def test_slow_nonce_lookup_cannot_dispatch_expired_capture(self):
        intent = self.emit()
        original = self.api.gh

        def delayed(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if "/runs?" in suffix:
                self.now += 301
            return result

        self.api.gh = delayed
        with self.assertRaisesRegex(
            common.ManualForwardError, "EXPIRED_BEFORE_DISPATCH"
        ):
            dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(self.api.posts, 0)

    def test_failed_github_run_exposes_verified_link_without_false_success(self):
        intent = self.emit()
        original = self.api.gh
        events = []

        def failed(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if suffix == "/actions/runs/42":
                result["conclusion"] = "failure"
            return result

        self.api.gh = failed
        case_id = dispatch.CASE_ORDER["dispatch_smoke_16p"]
        self.api.fail_cases.add(case_id)
        result = dispatch.forward(
            self.api, intent, clock=lambda: self.now, progress=events.append
        )
        self.assertFalse(result["selected_cases_pass"])
        self.assertEqual(
            next(
                row
                for row in result["case_results"]
                if row["test_id"] == "dispatch_smoke_16p"
            )["case_status"],
            "FAIL",
        )
        self.assertEqual(
            set(result["case_job_outputs"]),
            set(dispatch.CASE_OUTPUTS.values())
            | {"forward_gate", "forward_gate_reason"},
        )
        self.assertEqual(
            events,
            [
                {
                    "event": "GITHUB_RUN_BOUND",
                    "test_id": "worker_os_blue",
                    "github_run_id": 42,
                    "github_run_url": "https://github.com/cann-framework/torchtitan-npu/actions/runs/42",
                    "not_current_merge_authorization": True,
                }
            ],
        )
        self.assertEqual(self.api.posts, 1)

    def test_github_identity_errors_identify_fixed_field_not_untrusted_value(self):
        intent = self.emit()
        dispatch.forward(self.api, intent, clock=lambda: self.now)
        original = copy.deepcopy(self.api.gh_run)
        for field in (
            "run_attempt",
            "event",
            "head_sha",
            "head_branch",
            "workflow_id",
            "display_title",
        ):
            missing = copy.deepcopy(original)
            del missing[field]
            with self.assertRaisesRegex(
                common.ManualForwardError, "^GITHUB_RUN_MISSING_" + field.upper() + "$"
            ):
                dispatch.validate_github_run(missing, intent)
            wrong_type = copy.deepcopy(original)
            wrong_type[field] = None
            with self.assertRaisesRegex(
                common.ManualForwardError,
                "^GITHUB_RUN_TYPE_MISMATCH_" + field.upper() + "$",
            ):
                dispatch.validate_github_run(wrong_type, intent)
            wrong_value = copy.deepcopy(original)
            wrong_value[field] = (
                "sensitive-untrusted-value"
                if isinstance(original[field], str)
                else original[field] + 1
            )
            with self.assertRaisesRegex(
                common.ManualForwardError,
                "^GITHUB_RUN_VALUE_MISMATCH_" + field.upper() + "$",
            ):
                dispatch.validate_github_run(wrong_value, intent)

    def test_actual_post_failure_github_identity_snapshot_is_exact_not_first_get_proof(
        self,
    ):
        row = json.loads(
            (
                Path(__file__).parent / "fixtures/github_41438604990_post_failure.json"
            ).read_text()
        )
        intent = self.emit()
        intent["control_sha"] = row["head_sha"]
        intent["bridge"]["bridge_nonce"] = row["display_title"].removeprefix(
            "lite-gitcode:"
        )
        self.assertEqual(row["scope"], "post_failure_observation_not_first_get")
        self.assertEqual(dispatch.validate_github_run(row, intent), 41438604990)

    def initial_run_sequence(self, transform):
        original = self.api.gh
        reads = []

        def controlled(suffix, **kwargs):
            row = original(suffix, **kwargs)
            if suffix == "/actions/runs/42":
                reads.append(suffix)
                row = transform(row, len(reads))
            return row

        self.api.gh = controlled
        return reads

    def test_initial_non_nonce_title_pending_then_exact_one_post_no_early_binding(self):
        intent = self.emit()
        events = []

        def first(row, count):
            if count == 1:
                row.update(
                    display_title="Dispatcher-Smoke", status="queued", conclusion=None
                )
            return row

        reads = self.initial_run_sequence(first)
        result = dispatch.forward(
            self.api,
            intent,
            clock=lambda: self.now,
            sleep=lambda seconds: setattr(self, "now", self.now + seconds),
            progress=events.append,
        )
        self.assertEqual(result["historical_execution_result"], "PASS")
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(len(reads), 2)
        self.assertEqual(events[0], {"event": "INITIAL_TITLE_PENDING", "attempt": 1})
        self.assertEqual(events[1]["event"], "GITHUB_RUN_BOUND")
        self.assertNotIn("Dispatcher-Smoke", json.dumps(events))

    def test_initial_title_other_nonce_control_or_oversize_rejected_without_retry(self):
        for title in (
            "lite-gitcode:other_nonce",
            "lite-gitcode:",
            "bad\nvalue",
            "x" * 201,
        ):
            self.api = FakeAPI()
            intent = self.emit()
            reads = self.initial_run_sequence(
                lambda row, _, title=title: {
                    **row,
                    "display_title": title,
                    "status": "queued",
                }
            )
            with (
                self.subTest(title=title[:20]),
                self.assertRaisesRegex(
                    common.ManualForwardError, "VALUE_MISMATCH_DISPLAY_TITLE"
                ),
            ):
                dispatch.forward(
                    self.api, intent, clock=lambda: self.now, sleep=lambda _: None
                )
            self.assertEqual(len(reads), 1)
            self.assertEqual(self.api.posts, 1)

    def test_initial_title_pending_does_not_hide_other_identity_errors(self):
        for field, value in (
            ("head_sha", "c" * 40),
            ("run_attempt", "1"),
            ("id", 43),
            ("actor", {"login": "other"}),
        ):
            self.api = FakeAPI()
            intent = self.emit()
            reads = self.initial_run_sequence(
                lambda row, _, field=field, value=value: {
                    **row,
                    "display_title": "initial",
                    "status": "queued",
                    field: value,
                }
            )
            with (
                self.subTest(field=field),
                self.assertRaises(common.ManualForwardError),
            ):
                dispatch.forward(
                    self.api, intent, clock=lambda: self.now, sleep=lambda _: None
                )
            self.assertEqual(len(reads), 1)

    def test_initial_title_has_six_get_limit_even_if_clock_does_not_move(self):
        intent = self.emit()
        reads = self.initial_run_sequence(
            lambda row, _: {**row, "display_title": "", "status": "queued"}
        )
        with self.assertRaisesRegex(common.ManualForwardError, "INITIAL_TITLE_TIMEOUT"):
            dispatch.forward(
                self.api, intent, clock=lambda: self.now, sleep=lambda _: None
            )
        self.assertEqual(len(reads), 6)
        self.assertEqual(self.api.posts, 1)

    def test_slow_initial_get_crossing_30_seconds_deadline_or_capture_age_cannot_bind(
        self,
    ):
        for budget in ("window", "deadline", "age"):
            self.api = FakeAPI()
            self.now = 1000
            intent = self.emit()
            if budget == "deadline":
                intent["deadline"] = 1001
            if budget == "age":
                self.now = 1299

            def slow(row, _, budget=budget):
                self.now += 30 if budget == "window" else 1
                return row

            self.initial_run_sequence(slow)
            events = []
            with (
                self.subTest(budget=budget),
                self.assertRaisesRegex(
                    common.ManualForwardError, "INITIAL_TITLE_TIMEOUT"
                ),
            ):
                dispatch.forward(
                    self.api,
                    intent,
                    clock=lambda: self.now,
                    sleep=lambda _: None,
                    progress=events.append,
                )
            self.assertEqual(events, [])

    def test_bound_title_change_is_revocation_not_new_pending_window(self):
        intent = self.emit()

        def mutate(row, count):
            row.update(status="in_progress", conclusion=None)
            if count == 2:
                row["display_title"] = "initial again"
            return row

        reads = self.initial_run_sequence(mutate)
        events = []
        with self.assertRaisesRegex(
            common.ManualForwardError, "VALUE_MISMATCH_DISPLAY_TITLE"
        ):
            dispatch.forward(
                self.api,
                intent,
                clock=lambda: self.now,
                sleep=lambda _: None,
                progress=events.append,
            )
        self.assertEqual(len(reads), 2)
        self.assertEqual([event["event"] for event in events], ["GITHUB_RUN_BOUND"])

    def test_lost_dispatch_response_recovers_nonce_without_repost(self):
        intent = self.emit()
        self.api.lose_response = True
        self.assertEqual(
            dispatch.forward(self.api, intent, clock=lambda: self.now)["github_run_id"],
            42,
        )
        self.assertEqual(self.api.posts, 1)

    def test_unknown_dispatch_does_not_retry_post(self):
        intent = self.emit()
        self.api.lose_response = self.api.drop_dispatch = True
        with self.assertRaisesRegex(common.ManualForwardError, "UNKNOWN_NO_RETRY"):
            dispatch.forward(
                self.api, intent, clock=lambda: self.now, sleep=lambda _: None
            )
        self.assertEqual(self.api.posts, 1)

    def test_changed_actor_or_skipped_result_never_returns_pass(self):
        intent = self.emit()
        dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.api.gh_run["actor"] = {"login": "other"}
        with self.assertRaisesRegex(common.ManualForwardError, "OWNER_MISMATCH"):
            dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.api.gh_run["actor"] = {"login": "depeng1994"}
        self.api.completed_jobs[-1]["conclusion"] = "skipped"
        with self.assertRaisesRegex(
            common.ManualForwardError, "FIXED_RESULT_JOB_IDENTITY_INVALID"
        ):
            dispatch.forward(self.api, intent, clock=lambda: self.now)

    def test_public_github_reads_never_receive_dispatch_credential(self):
        api = common.API(
            {
                "ATOMGIT_TOKEN": "synthetic-gitcode",
                "LITE_ACTIONS_GITHUB_TOKEN": "synthetic-github",
            }
        )
        response = Mock()
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *a: False
        response.read.return_value = b"{}"
        api.opener.open = Mock(return_value=response)
        api.gh("/branches/master")
        request = api.opener.open.call_args.args[0]
        self.assertNotIn("Authorization", dict(request.header_items()))
        api.gh(
            f"/actions/workflows/{common.GH_WORKFLOW_ID}/dispatches",
            body={},
            dispatch=True,
        )
        self.assertEqual(
            api.opener.open.call_args.args[0].get_header("Authorization"),
            "Bearer synthetic-github",
        )
        with self.assertRaisesRegex(common.ManualForwardError, "CANNOT_WRITE"):
            api.gh("/issues", body={})

    def test_exhausted_public_quota_retries_fixed_run_read_once_with_token(self):
        api = common.API({"LITE_ACTIONS_GITHUB_TOKEN": "synthetic-github"})
        rate_error = urllib.error.HTTPError("https://api.github.com/", 403, "limit", {"X-RateLimit-Remaining": "0"}, None)
        response = Mock()
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *a: False
        response.read.return_value = b"{}"
        api.opener.open = Mock(side_effect=[rate_error, response])
        self.assertEqual(api.gh("/actions/runs/42"), {})
        first, second = [call.args[0] for call in api.opener.open.call_args_list]
        self.assertIsNone(first.get_header("Authorization"))
        self.assertEqual(second.get_header("Authorization"), "Bearer synthetic-github")
        self.assertEqual(first.full_url, second.full_url)
        self.assertEqual(second.get_method(), "GET")

    def test_rate_limit_fallback_is_closed_for_untrusted_or_missing_identity(self):
        for suffix, headers, token in (
            ("/issues/1", {"X-RateLimit-Remaining": "0"}, "synthetic-github"),
            ("/actions/runs/42", {"X-RateLimit-Remaining": "1"}, "synthetic-github"),
            ("/actions/runs/42", {"X-RateLimit-Remaining": "0"}, ""),
            ("/actions/runs/42/jobs?filter=all", {"X-RateLimit-Remaining": "0"}, "synthetic-github"),
        ):
            with self.subTest(suffix=suffix, headers=headers, token=bool(token)):
                api = common.API({"LITE_ACTIONS_GITHUB_TOKEN": token})
                api.opener.open = Mock(side_effect=urllib.error.HTTPError("https://api.github.com/", 403, "forbidden", headers, None))
                with self.assertRaises(common.ManualForwardError):
                    api.gh(suffix)
                self.assertEqual(api.opener.open.call_count, 1)
                self.assertIsNone(api.opener.open.call_args.args[0].get_header("Authorization"))

    def test_rate_limit_fallback_scope_and_single_retry(self):
        root = "/repos/" + common.GH_REPO
        runs = root + f"/actions/workflows/{common.GH_WORKFLOW_ID}/runs?"
        good_query = (
            "event=workflow_dispatch&branch=master&head_sha=" + "a" * 40
            + "&actor=" + common.ALLOWED_ACTOR + "&per_page=100&page=1"
        )
        for path in (
            root, root + "/branches/master",
            root + f"/actions/workflows/{common.GH_WORKFLOW_ID}",
            root + "/actions/runs/42", root + "/actions/runs/42/jobs?filter=latest&per_page=100",
            runs + good_query,
        ):
            with self.subTest(path=path):
                self.assertTrue(common._github_rate_limit_fallback_route(path))
        for path in (
            root + "/actions/runs/42/jobs?filter=all",
            root + "/actions/runs/42/../../secrets",
            runs + good_query + "&page=2", runs + "invalid",
        ):
            with self.subTest(path=path):
                self.assertFalse(common._github_rate_limit_fallback_route(path))
        api = common.API({"LITE_ACTIONS_GITHUB_TOKEN": "synthetic-github"})
        error = urllib.error.HTTPError(
            "https://api.github.com/", 403, "limit", {"X-RateLimit-Remaining": "0"}, None
        )
        api.opener.open = Mock(side_effect=[error, error])
        with self.assertRaisesRegex(common.ManualForwardError, "HTTP_403"):
            api.gh("/actions/runs/42")
        self.assertEqual(api.opener.open.call_count, 2)

    def test_authenticated_plan_reads_and_signed_artifact_redirect_are_forward_only(
        self,
    ):
        api = common.API(
            {
                "ATOMGIT_TOKEN": "synthetic-gitcode",
                "LITE_ACTIONS_GITHUB_TOKEN": "synthetic-github",
            }
        )
        response = Mock()
        response.__enter__ = lambda _: response
        response.__exit__ = lambda *a: False
        response.read.return_value = b"{}"
        api.opener.open = Mock(return_value=response)
        api.gh("/branches/master")
        self.assertNotIn(
            "Authorization", dict(api.opener.open.call_args.args[0].header_items())
        )
        api.gh("/actions/artifacts/77", authenticated_read=True)
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-github")
        with self.assertRaisesRegex(common.ManualForwardError, "ROUTE_NOT_ALLOWED"):
            api.gh("/issues/1", authenticated_read=True)
        with self.assertRaisesRegex(common.ManualForwardError, "CANNOT_WRITE"):
            api.gh("/check-runs/77", body={}, authenticated_read=True)

        import email.message

        blob = b"synthetic-bounded-plan-zip"
        redirect_headers = email.message.Message()
        redirect_headers["Location"] = (
            "https://productionresults.blob.core.windows.net/container/plan.zip?sig=synthetic-secret"
        )
        redirect = urllib.error.HTTPError(
            "https://api.github.com/repos/"
            + common.GH_REPO
            + "/actions/artifacts/77/zip",
            302,
            "Found",
            redirect_headers,
            None,
        )
        blob_response = Mock()
        blob_response.__enter__ = lambda _: blob_response
        blob_response.__exit__ = lambda *a: False
        blob_response.read.return_value = blob
        api.opener.open = Mock(side_effect=[redirect, blob_response])
        with patch("sys.stdout", io.StringIO()) as stdout:
            self.assertEqual(api.download_authenticated_artifact(77), blob)
            self.assertNotIn("synthetic-secret", stdout.getvalue())
        first, second = [call.args[0] for call in api.opener.open.call_args_list]
        self.assertEqual(first.get_header("Authorization"), "Bearer synthetic-github")
        self.assertIsNone(second.get_header("Authorization"))
        self.assertEqual(
            second.full_url.split("?", 1)[0],
            "https://productionresults.blob.core.windows.net/container/plan.zip",
        )

    def test_no_admission_receipt_is_published_as_authenticated_not_run_without_generation(
        self,
    ):
        intent = self.emit()
        plan, receipt = fake_plan_and_receipt(intent)
        self.api.case_checks = [
            fake_check(plan, receipt, case_id, slot["id"])
            for case_id, slot in plan["result_slots"].items()
        ]
        aggregate = next(
            check
            for check in self.api.case_checks
            if check["id"] == plan["result_slots"]["aggregate"]["id"]
        )
        aggregate["status"] = "in_progress"
        aggregate["conclusion"] = None
        aggregate["output"]["summary"] = json.dumps(
            {
                "schema": 4,
                "phase": "SLOT_CREATED",
                "request_id": f"{common.GH_ID}/42/1",
                "case_id": "aggregate",
                "request_spec_digest": plan["request_spec_digest"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        observed_receipt = dispatch.read_admission_receipt(self.api, plan, intent)
        self.assertIsNone(observed_receipt)
        check = next(
            check
            for check in self.api.case_checks
            if check["id"] == plan["result_slots"]["case_000_worker_os_blue"]["id"]
        )
        result = {
            "test_id": "worker_os_blue",
            "case_id": "case_000_worker_os_blue",
            "case_status": "NOT_RUN",
            "reason": "No Admission Receipt was published; no Case execution was authorized",
            "selected": True,
            "check_id": check["id"],
            "check_conclusion": None,
            "cleanup_verified": None,
            "exit_code": None,
            "case_check_url": check["html_url"],
            "case_log_url": check["html_url"],
            "current_pr_matches": True,
        }
        encoded = dispatch.encode_case_evidence(
            intent,
            plan,
            None,
            result,
            control_matches=True,
            workflow_conclusion="failure",
        )
        body = dispatch.decode_case_evidence(
            {
                "RESULT_CASE": "worker_os_blue",
                "CASE_EVIDENCE": encoded,
                "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
                "CAPTURE_ATTEMPT": "1",
                "CONTROL_SHA": intent["control_sha"],
            }
        )
        self.assertIsNone(body["identity"]["receipt"])
        self.assertEqual(body["case_status"], "NOT_RUN")

    def test_case_evidence_job_output_is_fixed_bounded_and_self_binding(self):
        intent = self.emit()
        plan, receipt = fake_plan_and_receipt(intent)
        self.api.case_checks = [
            fake_check(plan, receipt, case_id, slot["id"])
            for case_id, slot in plan["result_slots"].items()
        ]
        result = dispatch.case_check(
            self.api, intent, plan, receipt, 42, "worker_os_blue", pr_matches=True
        )
        encoded = dispatch.encode_case_evidence(
            intent,
            plan,
            receipt,
            result,
            control_matches=True,
            workflow_conclusion="success",
        )
        environment = {
            "RESULT_CASE": "worker_os_blue",
            "CASE_EVIDENCE": encoded,
            "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
            "CAPTURE_ATTEMPT": "1",
            "CONTROL_SHA": intent["control_sha"],
            "CAPTURE_RESULT": "success",
        }
        body = dispatch.decode_case_evidence(environment)
        self.assertEqual(body["case_status"], "PASS")
        self.assertEqual(body["identity"]["receipt"]["execution_generation"], 2)
        self.assertTrue(body["observation"]["historical_only"])

        changed = dict(environment)
        changed["RESULT_CASE"] = "dispatch_smoke_8p"
        with self.assertRaisesRegex(common.ManualForwardError, "BODY_INVALID"):
            dispatch.decode_case_evidence(changed)
        changed = dict(environment)
        changed["CAPTURE_RUN_ID"] = "other-run"
        with self.assertRaisesRegex(common.ManualForwardError, "IDENTITY_INVALID"):
            dispatch.decode_case_evidence(changed)
        changed = dict(environment)
        changed["CASE_EVIDENCE"] = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
        with self.assertRaisesRegex(
            common.ManualForwardError, "DIGEST_MISMATCH|INVALID"
        ):
            dispatch.decode_case_evidence(changed)

        with tempfile.NamedTemporaryFile() as output:
            dispatch.write_case_job_outputs(
                {
                    **dict.fromkeys(dispatch.CASE_OUTPUTS.values(), encoded),
                    "forward_gate": "success",
                    "forward_gate_reason": "OK",
                },
                output_path=output.name,
            )
            output.seek(0)
            rows = output.read().decode().splitlines()
        self.assertEqual(
            [row.split("=", 1)[0] for row in rows],
            [
                "case_os_result",
                "case_8p_result",
                "case_16p_result",
                "forward_gate",
                "forward_gate_reason",
            ],
        )
        self.assertTrue(all(len(row.split("=", 1)[1]) <= 32 * 1024 for row in rows))

    def test_case_matrix_os_pass_8p_fail_16p_not_run_keeps_independent_statuses(self):
        intent = self.emit()
        self.api.fail_cases.add(dispatch.CASE_ORDER["dispatch_smoke_8p"])
        self.api.not_run_cases.add(dispatch.CASE_ORDER["dispatch_smoke_16p"])
        self.api.github_run_conclusion = "failure"
        result = dispatch.forward(self.api, intent, clock=lambda: self.now)
        statuses = {
            row["test_id"]: row["case_status"] for row in result["case_results"]
        }
        self.assertEqual(
            statuses,
            {
                "worker_os_blue": "PASS",
                "dispatch_smoke_8p": "FAIL",
                "dispatch_smoke_16p": "NOT_RUN",
            },
        )
        self.assertEqual(result["forward_gate"], "failure")
        self.assertEqual(result["forward_gate_reason"], "GITHUB_RUN_NOT_SUCCESS")
        plan = self.api.plan
        receipt = self.api.receipt
        by_test = {row["test_id"]: row for row in result["case_results"]}
        exits = {}
        for test_id in dispatch.ALLOWED_CASES:
            exit_code, payload = self.render_case(
                intent, plan, receipt, by_test[test_id], "failure"
            )
            exits[test_id] = exit_code
            self.assertEqual(payload["case_status"], statuses[test_id])
            self.assertTrue(payload["historical_only"])
            self.assertEqual(payload["current_authority"], "NOT_PROVIDED")
        self.assertEqual(
            exits,
            {
                "worker_os_blue": 0,
                "dispatch_smoke_8p": 1,
                "dispatch_smoke_16p": 1,
            },
        )

    def test_three_case_pass_with_authority_failure_keeps_cases_green_and_aggregate_red(
        self,
    ):
        intent = self.emit()
        with patch.object(dispatch, "current_pr_matches", return_value=False):
            result = dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(
            [row["case_status"] for row in result["case_results"]],
            ["PASS", "PASS", "PASS"],
        )
        self.assertEqual(result["forward_gate"], "failure")
        self.assertEqual(result["forward_gate_reason"], "PR_AUTHORITY_MISMATCH")
        for row in result["case_results"]:
            exit_code, payload = self.render_case(
                intent, self.api.plan, self.api.receipt, row, "success"
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["case_status"], "PASS")
            self.assertTrue(payload["historical_only"])
            self.assertFalse(payload["current_pr_matches"])

    def test_three_case_pass_with_github_total_failure_keeps_case_jobs_green(self):
        intent = self.emit()
        self.api.github_run_conclusion = "failure"
        result = dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(
            [row["case_status"] for row in result["case_results"]],
            ["PASS", "PASS", "PASS"],
        )
        self.assertEqual(result["forward_gate"], "failure")
        self.assertEqual(result["forward_gate_reason"], "GITHUB_RUN_NOT_SUCCESS")
        for row in result["case_results"]:
            exit_code, payload = self.render_case(
                intent, self.api.plan, self.api.receipt, row, "failure"
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["case_status"], "PASS")

    def test_legacy_os_only_selector_marks_8p_16p_not_run_without_failing_them(self):
        self.env["TEST_CASE"] = "worker_os_blue"
        intent = self.emit()
        result = dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(
            [row["case_status"] for row in result["case_results"]],
            ["PASS", "NOT_RUN", "NOT_RUN"],
        )
        self.assertEqual(
            [row["selected"] for row in result["case_results"]],
            [True, False, False],
        )
        self.assertEqual(result["forward_gate"], "success")
        for row in result["case_results"]:
            exit_code, payload = self.render_case(
                intent, self.api.plan, self.api.receipt, row, "success"
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["case_status"], row["case_status"])

    def test_missing_or_identity_mismatched_case_output_fails_with_fixed_reason(self):
        missing = {
            "RESULT_CASE": "worker_os_blue",
            "CAPTURE_RESULT": "success",
        }
        with patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(dispatch.display_case_main(missing), 1)
            payload = json.loads(output.getvalue())
        self.assertEqual(payload["case_status"], "INFRA_ERROR")
        self.assertIn("CASE_EVIDENCE_MISSING_OR_OVERSIZED", payload["reason"])

        intent = self.emit()
        plan, receipt = fake_plan_and_receipt(intent)
        self.api.case_checks = [
            fake_check(plan, receipt, case_id, slot["id"])
            for case_id, slot in plan["result_slots"].items()
        ]
        result = dispatch.case_check(
            self.api, intent, plan, receipt, 42, "worker_os_blue", pr_matches=True
        )
        encoded = dispatch.encode_case_evidence(
            intent,
            plan,
            receipt,
            result,
            control_matches=True,
            workflow_conclusion="success",
        )
        wrong = {
            "RESULT_CASE": "worker_os_blue",
            "CASE_EVIDENCE": encoded,
            "CAPTURE_RUN_ID": "wrong-capture-run",
            "CAPTURE_ATTEMPT": "1",
            "CONTROL_SHA": intent["control_sha"],
            "CAPTURE_RESULT": "success",
        }
        with patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(dispatch.display_case_main(wrong), 1)
            payload = json.loads(output.getvalue())
        self.assertEqual(payload["case_status"], "INFRA_ERROR")
        self.assertIn(
            "CASE_EVIDENCE_REJECTED:CASE_EVIDENCE_IDENTITY_INVALID", payload["reason"]
        )

    def test_selected_case_check_read_error_keeps_verified_run_link_in_bounded_evidence(
        self,
    ):
        intent = self.emit()
        plan, receipt = fake_plan_and_receipt(intent)
        result = {
            "test_id": "dispatch_smoke_8p",
            "case_id": dispatch.CASE_ORDER["dispatch_smoke_8p"],
            "case_status": "INFRA_ERROR",
            "reason": "CASE_EVIDENCE_READ_FAILED:API_GITHUB_CHECK_HTTP_403",
            "selected": True,
            "check_id": None,
            "check_conclusion": None,
            "cleanup_verified": None,
            "exit_code": None,
            "case_check_url": None,
            "case_log_url": f"https://github.com/{common.GH_REPO}/actions/runs/42",
            "current_pr_matches": True,
        }
        encoded = dispatch.encode_case_evidence(
            intent,
            plan,
            receipt,
            result,
            control_matches=True,
            workflow_conclusion="success",
        )
        body = dispatch.decode_case_evidence(
            {
                "RESULT_CASE": "dispatch_smoke_8p",
                "CASE_EVIDENCE": encoded,
                "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
                "CAPTURE_ATTEMPT": "1",
                "CONTROL_SHA": intent["control_sha"],
            }
        )
        self.assertEqual(body["case_status"], "INFRA_ERROR")
        self.assertEqual(body["case_log_url"], result["case_log_url"])
        self.assertIsNone(body["case_check_url"])

    def test_cancelled_case_evidence_keeps_cancelled_status_and_display_job_fails(self):
        intent = self.emit()
        plan, receipt = fake_plan_and_receipt(intent)
        self.api.case_checks = [
            fake_check(plan, receipt, case_id, slot["id"])
            for case_id, slot in plan["result_slots"].items()
        ]
        result = dispatch.case_check(
            self.api, intent, plan, receipt, 42, "worker_os_blue", pr_matches=True
        )
        body = dispatch.decode_case_evidence(
            {
                "RESULT_CASE": "worker_os_blue",
                "CASE_EVIDENCE": dispatch.encode_case_evidence(
                    intent,
                    plan,
                    receipt,
                    result,
                    control_matches=True,
                    workflow_conclusion="cancelled",
                ),
                "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
                "CAPTURE_ATTEMPT": "1",
                "CONTROL_SHA": intent["control_sha"],
            }
        )
        body["case_status"] = "CANCELLED"
        body["reason"] = "GitHub cancelled the case before completion"
        body["check_conclusion"] = "cancelled"
        raw = common.canonical(body)
        encoded = (
            __import__("base64")
            .urlsafe_b64encode(
                common.canonical(
                    {"body": body, "evidence_digest": hashlib.sha256(raw).hexdigest()}
                )
            )
            .decode()
            .rstrip("=")
        )
        environment = {
            "RESULT_CASE": "worker_os_blue",
            "CASE_EVIDENCE": encoded,
            "CAPTURE_RUN_ID": intent["bridge"]["bridge_action_run"],
            "CAPTURE_ATTEMPT": "1",
            "CONTROL_SHA": intent["control_sha"],
            "CAPTURE_RESULT": "success",
        }
        self.assertEqual(
            dispatch.decode_case_evidence(environment)["case_status"], "CANCELLED"
        )
        with patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(dispatch.display_case_main(environment), 1)
            rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["case_status"], "CANCELLED")
        self.assertEqual(rendered["case_log_url"], result["case_log_url"])

    def test_slow_capture_evidence_read_cannot_cross_freshness_window(self):
        self.emit()
        original = self.api.gh
        calls = [0]

        def slow(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if suffix.endswith(str(common.GH_WORKFLOW_ID)):
                calls[0] += 1
                self.now = 1301
            return result

        self.api.gh = slow
        with self.assertRaisesRegex(common.ManualForwardError, "EXPIRED_DURING_READ"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)
        self.assertEqual(self.api.posts, 0)

    def test_slow_final_run_read_cannot_return_pass_after_deadline(self):
        intent = self.emit()
        original = self.api.gh
        reads = []

        def slow(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if suffix == "/actions/runs/42":
                reads.append(suffix)
                if len(reads) == 1:
                    result.update(status="in_progress", conclusion=None)
                else:
                    self.now = intent["deadline"]
            return result

        self.api.gh = slow
        with self.assertRaisesRegex(common.ManualForwardError, "DEADLINE_EXCEEDED"):
            dispatch.forward(
                self.api, intent, clock=lambda: self.now, sleep=lambda _: None
            )

    def test_changed_completed_capture_job_is_rejected(self):
        self.emit()
        original = self.api.gc
        count = [0]

        def changed(suffix="", **kwargs):
            result = original(suffix, **kwargs)
            if suffix == "/actions/runs/run1/jobs/job1":
                count[0] += 1
                result["exec_id"] = "first" if count[0] == 1 else "second"
            return result

        self.api.gc = changed
        with self.assertRaisesRegex(common.ManualForwardError, "EVIDENCE_CHANGED"):
            dispatch.retrieve_intent(self.api, self.env, clock=lambda: self.now)

    def test_ambiguous_nonce_never_dispatches_again(self):
        intent = self.emit()
        dispatch.forward(self.api, intent, clock=lambda: self.now)
        original = self.api.gh

        def duplicate(suffix, **kwargs):
            result = original(suffix, **kwargs)
            if "/runs?" in suffix:
                result["workflow_runs"] *= 2
                result["total_count"] = 2
            return result

        self.api.gh = duplicate
        with self.assertRaisesRegex(common.ManualForwardError, "NONCE_AMBIGUOUS"):
            dispatch.forward(self.api, intent, clock=lambda: self.now)
        self.assertEqual(self.api.posts, 1)

    def test_redirects_and_unapproved_api_targets_are_refused(self):
        self.assertIsNone(
            common.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil.invalid"
            )
        )
        api = common.API(
            {"ATOMGIT_TOKEN": "synthetic", "LITE_ACTIONS_GITHUB_TOKEN": "synthetic"}
        )
        api.opener.open = Mock()
        for service, path in (
            ("github", "/repos/other/project/branches/master"),
            ("gitcode", "/api/v8/repos/cann/torchtitan-npu/actions/runs/1"),
            ("gitcode", "//evil.invalid"),
        ):
            with (
                self.subTest(service=service, path=path),
                self.assertRaises(common.ManualForwardError),
            ):
                api.request(service, path)
        with self.assertRaises(common.ManualForwardError):
            api.gc("/pulls/2/comments", body={"body": "not allowed"})
        api.opener.open.assert_not_called()

    def test_http_failure_labels_service_route_without_secret_or_raw_target(self):
        import urllib.error

        api = common.API({"ATOMGIT_TOKEN": "sensitive-token"})
        paths = {
            "/actions/runs/run1": "GITCODE_ACTION_RUN",
            "/actions/runs/run1/jobs": "GITCODE_ACTION_JOBS",
            "/actions/runs/run1/jobs/job1": "GITCODE_ACTION_JOB",
            "/actions/runs/run1/jobs/job1/logs": "GITCODE_ACTION_STEP_LOGS",
        }
        for suffix, label in paths.items():
            api.opener.open = Mock(
                side_effect=urllib.error.HTTPError(
                    "sensitive-url", 403, "sensitive-body", {}, None
                )
            )
            with self.assertRaisesRegex(
                common.ManualForwardError, "^API_" + label + "_HTTP_403$"
            ):
                api.gc(suffix, version=8)

    def test_error_output_does_not_dump_api_body_or_credentials(self):
        import io
        from contextlib import redirect_stderr

        output = io.StringIO()
        with redirect_stderr(output):
            common.emit_failure(ValueError("synthetic-secret"))
        self.assertNotIn("synthetic-secret", output.getvalue())


class ManualAggregateGateTests(unittest.TestCase):
    def setUp(self):
        self.good = {
            "CAPTURE_RESULT": "success",
            "FORWARD_RESULT": "success",
            "FORWARD_GATE": "success",
            "FORWARD_GATE_REASON": "OK",
            "OS_RESULT": "success",
            "EIGHT_P_RESULT": "success",
            "SIXTEEN_P_RESULT": "success",
        }

    def test_three_case_success_is_historical_only_not_merge_authority(self):
        result = aggregate.evaluate(self.good)
        self.assertEqual(result["aggregate_status"], "PASS")
        self.assertIs(result["historical_only"], True)
        self.assertEqual(result["current_authority"], "NOT_PROVIDED")
        self.assertEqual(result["merge_gate"], "NOT_PROVIDED")

    def test_missing_forward_authority_or_case_result_fails_closed(self):
        cases = (
            ({"FORWARD_RESULT": "failure"}, "FORWARD_FAILED_OR_MISSING_OUTPUT"),
            ({"FORWARD_GATE": ""}, "FORWARD_GATE_MISSING_OR_INVALID"),
            (
                {
                    "FORWARD_GATE": "success",
                    "FORWARD_GATE_REASON": "PR_AUTHORITY_MISMATCH",
                },
                "FORWARD_GATE_REASON_MISMATCH",
            ),
            (
                {
                    "FORWARD_GATE": "failure",
                    "FORWARD_GATE_REASON": "PR_AUTHORITY_MISMATCH",
                },
                "PR_AUTHORITY_MISMATCH",
            ),
            ({"FORWARD_GATE_REASON": "UNTRUSTED"}, "FORWARD_GATE_REASON_INVALID"),
            ({"OS_RESULT": "failure"}, "OS_CASE_DISPLAY_FAILED"),
            ({"EIGHT_P_RESULT": "failure"}, "8P_CASE_DISPLAY_FAILED"),
            ({"SIXTEEN_P_RESULT": "failure"}, "16P_CASE_DISPLAY_FAILED"),
        )
        for changes, expected in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaises(ManualForwardError) as caught,
            ):
                aggregate.evaluate({**self.good, **changes})
            self.assertEqual(str(caught.exception), expected)

    def test_cli_dispatches_capture_forward_result_and_aggregate_only(self):
        from ci_core.gitcode_manual import cli

        cases = (
            ("capture", {}, capture, "main"),
            ("forward", {}, dispatch, "main"),
            ("result", {"RESULT_CASE": "dispatch_smoke_8p"}, dispatch, "main"),
            ("aggregate", {}, aggregate, "main"),
        )
        for phase, extra, module, function in cases:
            with self.subTest(phase=phase):
                with (
                    patch.dict(
                        os.environ, {"LITE_GITCODE_MANUAL_PHASE": phase, **extra}
                    ),
                    patch.object(module, function, return_value=0) as called,
                ):
                    self.assertEqual(cli.main(), 0)
                    called.assert_called_once()

    def test_cli_rejects_result_phase_without_case_and_forward_with_case(self):
        from ci_core.gitcode_manual import cli

        for phase, extra in (("result", {}), ("forward", {"RESULT_CASE": "x"})):
            with self.subTest(phase=phase):
                with (
                    patch.dict(
                        os.environ, {"LITE_GITCODE_MANUAL_PHASE": phase, **extra}
                    ),
                    patch("sys.stderr", io.StringIO()) as error_output,
                ):
                    self.assertEqual(cli.main(), 1)
                    self.assertTrue(error_output.getvalue())

    def test_cli_dispatches_only_fixed_phases(self):
        from ci_core.gitcode_manual import cli

        with (
            patch.dict(os.environ, {"LITE_GITCODE_MANUAL_PHASE": "aggregate"}),
            patch.object(aggregate, "main", return_value=0) as run,
        ):
            self.assertEqual(cli.main(), 0)
            run.assert_called_once()
        with (
            patch.dict(os.environ, {"LITE_GITCODE_MANUAL_PHASE": "unknown"}),
            patch("sys.stderr", io.StringIO()) as error_output,
        ):
            self.assertEqual(cli.main(), 1)
            self.assertIn("MANUAL_PHASE_NOT_ALLOWED", error_output.getvalue())
