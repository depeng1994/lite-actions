"""Native Blue admission, source identity and immutable-container regressions."""

import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.contract import VLLM_REPO, create_plan, normalize, parse_plan
from ci_core.gitcode_environment import ContainerBinding
from ci_core.reporting import report
from ci_core.ssh_runner import allocations, remote_command, verify
from ci_core.vllm_ascend import definition
from ci_core.vllm_source import check_current, read_source, validate_source


def fixture():
    defaults = dict(
        worker="blue", target_pool="a3_shared_blue", docker="vllm_hccl_smoke"
    )
    ids = ["vllm_worker_os_blue", "dispatch_smoke_a3_8p"]
    cases = []
    for name in ids:
        d = definition(name)
        cases.append(
            normalize(
                SimpleNamespace(
                    test_name=name,
                    test_descr=d["label"],
                    nnodes=d["nnodes"],
                    ngpu=d["ngpu"],
                    ci_kind=d["kind"],
                    ci_overrides=d["ci_overrides"],
                    timeout=d["timeout"],
                ),
                defaults,
                len(cases),
            )
        )
    run = dict(
        id=123,
        run_attempt=1,
        head_sha="a" * 40,
        event="pull_request_target",
        _ci_repo=VLLM_REPO,
        _ci_pr_number=7,
        _ci_control_sha="c" * 40,
        path=".github/workflows/lite_actions_dispatcher_smoke.yml",
    )
    plan = create_plan(
        cases,
        run_id=123,
        attempt=1,
        sha=run["head_sha"],
        workflow="lite_actions_dispatcher_smoke.yml",
        defaults=defaults,
        repo=VLLM_REPO,
    )
    source = dict(
        schema=1,
        provider="github",
        repository=VLLM_REPO,
        repository_id=1438625957,
        run_id=123,
        attempt=1,
        plan_digest=plan["plan_digest"],
        control_sha="c" * 40,
        head_sha="a" * 40,
        base_ref="main",
        test_ids=ids,
        mode="merge_result",
        pr_number=7,
        head_repo_id=1440390070,
        base_sha="b" * 40,
        source_sha="d" * 40,
        tree_sha="e" * 40,
    )
    pr = dict(
        number=7,
        state="open",
        draft=False,
        merged=False,
        user={"login": "depeng1994"},
        labels=[{"name": "lite-a3-blue"}],
        head=dict(
            sha="a" * 40, repo=dict(id=1440390070, full_name="depeng1994/vllm-ascend")
        ),
        base=dict(ref="main", repo=dict(id=1438625957, full_name=VLLM_REPO)),
    )
    return run, plan, source, pr


class NativeBlueTests(unittest.TestCase):
    def setUp(self):
        self.run, self.plan, self.source, self.pr = fixture()
        self.base = self.source["base_sha"]
        self.parents = [{"sha": self.base}, {"sha": self.source["head_sha"]}]
        self.prepare_status = "completed"
        self.prepare_conclusion = "success"
        self.client = Mock(repo=VLLM_REPO)
        self.client.request.side_effect = self.get
        self.client.workflow_status.return_value = dict(
            status="in_progress", run_attempt=1
        )
        self.client.download_artifact.side_effect = self.download

    def get(self, method, path, data=None):
        self.assertEqual(method, "GET")
        if path == "/pulls/7":
            return self.pr
        if path == "/branches/main":
            return {"commit": {"sha": self.base}}
        if "/attempts/1/jobs?" in path:
            return {
                "total_count": 1,
                "jobs": [
                    dict(
                        name="prepare",
                        run_id=123,
                        run_attempt=1,
                        status=self.prepare_status,
                        conclusion=self.prepare_conclusion,
                    )
                ],
            }
        if "/artifacts?" in path:
            return {
                "artifacts": [
                    dict(
                        id=456,
                        name="lite-ci-source-1",
                        size_in_bytes=2048,
                        expired=False,
                        workflow_run={"id": 123},
                    )
                ]
            }
        if path.startswith("/git/commits/"):
            return dict(
                sha=self.source["source_sha"],
                tree={"sha": self.source["tree_sha"]},
                parents=self.parents,
            )
        raise AssertionError(path)

    def download(self, ident, *, limit):
        self.assertEqual(ident, 456)
        self.assertLessEqual(limit, 131072)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("source.json", json.dumps(self.source))
        return stream.getvalue()

    def read(self):
        with patch.dict("os.environ", LITE_ACTIONS_VLLM_CONTROL_SHAS="c" * 40):
            return read_source(self.client, self.run, self.plan)

    def test_exact_current_merge_is_admitted_after_prepare_success(self):
        self.assertEqual(self.read(), self.source)
        self.assertNotEqual(self.source["source_sha"], self.plan["sha"])

    def test_incomplete_or_failed_prepare_has_zero_source_downloads(self):
        for status, conclusion in [
            ("in_progress", None),
            ("completed", "failure"),
            ("completed", "cancelled"),
        ]:
            self.prepare_status, self.prepare_conclusion = status, conclusion
            self.assertIsNone(self.read())
        self.client.download_artifact.assert_not_called()

    def test_wrong_merge_parents_are_not_a_head_only_fallback(self):
        self.parents.reverse()
        with self.assertRaisesRegex(ValueError, "parents"):
            self.read()

    def test_wrong_control_attempt_repository_and_case_spec_are_rejected(self):
        for key, value in [
            ("control_sha", "f" * 40),
            ("attempt", 2),
            ("repository_id", 1210038933),
            ("test_ids", ["dispatch_smoke_a5_8p"]),
            ("head_sha", "f" * 40),
            ("schema", True),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_source(
                    {**self.source, key: value}, self.run, self.plan, {"c" * 40}
                )

    def test_provider_disabled_without_pinned_control_release(self):
        with self.assertRaisesRegex(ValueError, "unapproved"):
            validate_source(self.source, self.run, self.plan, set())

    def test_base_only_update_invalidates_unchanged_head(self):
        self.base = "f" * 40
        with self.assertRaisesRegex(ValueError, "STALE"):
            check_current(self.client, self.source)

    def test_head_retarget_closed_and_label_spec_changes_revoke(self):
        original = copy.deepcopy(self.pr)
        for mutation in (
            lambda p: p["head"].update(sha="f" * 40),
            lambda p: p["base"].update(ref="other"),
            lambda p: p.update(state="closed"),
            lambda p: p["labels"].append({"name": "lite-a3-16p"}),
            lambda p: p.update(labels=[]),
        ):
            self.pr = copy.deepcopy(original)
            mutation(self.pr)
            with self.assertRaisesRegex(ValueError, "STALE"):
                check_current(self.client, self.source)

    def test_cancelled_run_and_new_attempt_block_launch_or_publish(self):
        for state in (
            dict(status="completed", run_attempt=1),
            dict(status="in_progress", run_attempt=2),
        ):
            self.client.workflow_status.return_value = state
            with self.assertRaisesRegex(ValueError, "SUPERSEDED"):
                check_current(self.client, self.source, check_run=True)

    def test_blue_docker_and_topology_rejected_before_lease(self):
        from ci_core.contract import digest

        for change in (
            {"docker": "cann_master_torchtitan_master_xyj"},
            {"worker": "yellow"},
            {"resources": {"nodes": 2, "devices_per_node": 8}},
        ):
            plan = copy.deepcopy(self.plan)
            plan["cases"][1].update(change)
            plan["plan_digest"] = digest(
                {k: v for k, v in plan.items() if k != "plan_digest"}
            )
            with self.assertRaisesRegex(ValueError, "policy"):
                parse_plan(plan, self.run, plan["workflow"], repo=VLLM_REPO)

    def test_probe_has_no_container_pool_or_device(self):
        probe = self.plan["cases"][0]
        self.assertEqual(probe["resources"], {"nodes": 0, "devices_per_node": 0})
        self.assertIsNone(probe["docker"])
        self.assertIsNone(probe["target_pool"])

    def test_report_keeps_head_wire_identity_but_binds_execution_merge(self):
        run = {
            **self.run,
            "_ci_source": self.source,
            "_ci_execution_sha": self.source["source_sha"],
        }
        result = report(
            run, self.plan["cases"][1], self.plan["plan_digest"], "PASS", 0, [], True
        )
        self.assertEqual(result["sha"], self.source["head_sha"])
        self.assertEqual(
            result["native_source"]["source_sha"], self.source["source_sha"]
        )

    def test_registry_does_not_open_or_execute_source_python(self):
        from ci_core.plan import verify_definition

        with patch("tarfile.open", side_effect=AssertionError("PR import forbidden")):
            for case in self.plan["cases"]:
                verify_definition(
                    Path("does-not-exist"), case, self.plan["defaults"], repo=VLLM_REPO
                )


class CapturedContainerTests(unittest.TestCase):
    def setUp(self):
        node = dict(
            host_id="192.0.2.1:153",
            host_ssh="192.0.2.1:153",
            ssh="192.0.2.1:153/" + "a" * 64,
            position=0,
            devices=list(range(8)),
            node_ip="192.0.2.2",
            environments={
                "vllm_hccl_smoke": dict(
                    container_id="a" * 64,
                    image_id="sha256:" + "b" * 64,
                    image_ref="example/pinned:tag",
                )
            },
        )
        node["container_binding"] = ContainerBinding.from_node(node).metadata()
        self.node = node
        self.case = dict(
            docker="vllm_hccl_smoke",
            allocation=[dict(host_id=node["host_id"], devices=list(range(8)))],
            bound_nodes=[node],
        )

    def test_recovery_uses_original_id_not_rebuilt_name_or_current_configuration(self):
        with patch(
            "ci_core.config.execution_node",
            side_effect=AssertionError("no fresh lookup"),
        ):
            self.assertEqual(allocations(self.case, {}), [self.node])

    def test_mutated_capture_cannot_redirect_cleanup(self):
        self.node["ssh"] = "192.0.2.1:153/" + "c" * 64
        with self.assertRaisesRegex(ValueError, "identity"):
            allocations(self.case, {})

    def test_blue_uses_separate_trusted_runner_and_exact_source_sha(self):
        runner = dict(
            source_sha="d" * 40,
            env_vars={"ASCEND_SET_ENV_PATH": "/trusted/set_env.sh"},
            LITE_CI_MASTER_PORT=16000,
        )
        command = remote_command(
            self.node,
            [self.node],
            runner,
            "/mnt/share/ci_tests/task/node0",
            "unique_job",
            180,
            "dispatch_smoke_a3_8p",
        )
        self.assertIn("trusted_hccl.py", command)
        self.assertIn("--source-sha=" + "d" * 40, command)
        self.assertNotIn("a5_smoke.py", command)

    def test_verifier_compares_sum_tree_payload_path_and_source_sha(self):
        with patch(
            "ci_core.ssh_runner.ssh", return_value=SimpleNamespace(returncode=0)
        ) as ssh:
            self.assertEqual(
                verify(
                    [self.node],
                    {"source_sha": "d" * 40},
                    "/mnt/share/ci_tests/task",
                    30,
                    "dispatch_smoke_a3_8p",
                ),
                0,
            )
        command = ssh.call_args.args[1]
        for expected in ("all_reduce_sum", "28", "source_sha", "payload_path"):
            self.assertIn(expected, command)


class SourceCacheTests(unittest.TestCase):
    def test_cache_requires_sealed_digest_identity_and_non_symlink(self):
        from ci_core.source_cache import seal, verified

        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            archive.write_bytes(b"fixed archive")
            self.assertFalse(verified(archive, VLLM_REPO, "a" * 40))
            seal(archive, VLLM_REPO, "a" * 40)
            self.assertTrue(verified(archive, VLLM_REPO, "a" * 40))
            self.assertFalse(verified(archive, "other/repo", "a" * 40))
            self.assertFalse(verified(archive, VLLM_REPO, "b" * 40))
            archive.write_bytes(b"changed archive")
            self.assertFalse(verified(archive, VLLM_REPO, "a" * 40))
            alias = Path(directory) / "alias.tar.gz"
            alias.symlink_to(archive)
            self.assertFalse(verified(alias, VLLM_REPO, "a" * 40))


if __name__ == "__main__":
    unittest.main()
