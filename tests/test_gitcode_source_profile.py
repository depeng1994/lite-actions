"""CPU contract tests for GitCode source profiles and PR lifecycle."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_core.contract import digest
from ci_core.executor import remote_task_name, task_dir
from ci_core.gitcode import GitCodeClient, RunChanged, source_plan
from ci_core.reporting import post, report
from ci_core.router import Router
from ci_core.ssh_runner import model_archive
from tests.scenarios import write_workspace


def profile():
    def regional(worker):
        return dict(
            test_id=f"local_{worker}",
            label=worker,
            kind="npu_smoke",
            nnodes=1,
            ngpu=2,
            env_vars={"ASCEND_SET_ENV_PATH": "/fixture/cann"},
            ci_overrides={
                "worker": worker,
                "target_pool": "pool_" + worker,
                "docker": "training",
            },
            timeout=90,
        )

    def smoke(name, nodes):
        return dict(
            test_id=name,
            label=name,
            env_vars={"ASCEND_SET_ENV_PATH": "/fixture/cann"},
            kind="npu_smoke",
            nnodes=nodes,
            ngpu=8,
            timeout=90,
            ci_overrides={
                "worker": "blue",
                "target_pool": "pool_blue",
                "docker": "training",
            },
        )

    return [
        regional("blue"),
        regional("yellow"),
        smoke("small", 1),
        smoke("large", 2),
        smoke("small", 1),
    ]


def source_archive(root, rows):
    """Fixture model repository whose real inspect entrypoint returns the profile."""
    archive_path = root / "model.tar.gz"
    from tests.scenarios import write_catalog

    with tarfile.open(archive_path, "w:gz") as tar:
        write_catalog(tar, rows)
        data = b"raise RuntimeError('model code executed on Worker')"
        info = tarfile.TarInfo(
            "tests/integration_tests/tools/lite_actions/entrypoint.py"
        )
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return archive_path


class SourceProfileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        write_workspace(self.root)
        self.archive = source_archive(self.root, profile())
        self.client = GitCodeClient("fixture-token", "cann/torchtitan-npu")
        self.job = {
            "name": "Dispatcher Smoke",
            "status": "RUNNING",
            "last_dispatch_id": "dispatch_1",
            "start_time": 1767225600000,
        }
        self.run = dict(
            workflow_run_id="run_1",
            attempt=1,
            event="MR",
            workflow_name="lite-ci-per-pr-alltest",
            status="RUNNING",
            head_sha="a" * 40,
            head_branch="branch",
            file_path=".gitcode/workflows/dispatcher_smoke.yml",
            pull_request_id="17",
            actor={"login": "fork_author"},
            start_time=1767225600,
            run_number=1,
            stages=[{"jobs": [{"name": "parent", "status": "RUNNING"}, self.job]}],
        )
        self.pr = dict(
            number=17,
            state="open",
            head={
                "ref": "branch",
                "sha": "a" * 40,
                "repo": {"full_name": "someone/torchtitan-npu"},
            },
            base={"repo": {"full_name": "cann/torchtitan-npu"}},
        )
        self.merge_ref = None
        self.comments = []
        env = {
            "LITE_ACTIONS_GITCODE_SOURCE_WORKFLOWS": "dispatcher_smoke.yml",
            "LITE_ACTIONS_GITCODE_WORKFLOWS": "dispatcher_smoke.yml",
            "LITE_ACTIONS_GITCODE_ALLOWED_ACTORS": "fork_author",
            "LITE_ACTIONS_GITCODE_ALLOWED_BRANCHES": "branch",
            "LITE_ACTIONS_STATE_DIR": str(self.root / "state"),
            "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(self.root / "locks"),
        }
        for fixture in (
            patch.dict(os.environ, env),
            patch.object(self.client, "request", side_effect=self.api),
            patch.object(
                self.client, "commit_comments", side_effect=lambda _: self.comments
            ),
            patch.object(self.client, "send_result", side_effect=self.send),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    def api(self, method, path, **kwargs):
        if path.startswith("/actions/runs?"):
            return {"workflow_runs": [self.run]}
        if path == "/actions/runs/run_1":
            return self.run
        if path == "/pulls/17":
            return self.pr
        if (
            path == "/commits/refs%2Fmerge-requests%2F17%2Fmerge"
            and self.merge_ref is not None
        ):
            return self.merge_ref
        raise AssertionError((method, path))

    def send(self, sha, body):
        row = {
            "id": len(self.comments) + 1,
            "body": body,
            "user": {"login": "reporter"},
        }
        self.comments.append(row)
        return row

    def router(self, worker):
        with patch.dict(
            os.environ, {"LITE_ACTIONS_STATE_DIR": str(self.root / worker)}
        ):
            router = Router(self.root, worker, self.client, "reporter")
        self.addCleanup(router.close)
        return router

    # 看护模型仓完整用例计划在不同 Worker 按归属分发，手动和 PR 使用同一来源。
    def test_profile_discovery_routes_all_cases_to_their_own_worker(self):
        with patch(
            "ci_core.ssh_runner.model_archive", return_value=self.archive
        ) as archive:
            run = self.client.running_workflows()[0]
            plan = self.client.ci_request(run)
            self.assertEqual(archive.call_args.args[0], "a" * 40)
            self.assertEqual(
                [c["test_id"] for c in plan["cases"]], [r["test_id"] for r in profile()]
            )
            self.assertEqual(len({c["case_id"] for c in plan["cases"]}), len(profile()))
            self.assertEqual(run["_ci_source_repo"], "someone/torchtitan-npu")
            self.assertEqual(run["_ci_execution_sha"], "a" * 40)
            self.assertEqual(plan["sha"], "a" * 40)
            for worker, expected in (
                ("blue", ["local_blue", "small", "large", "small"]),
                ("yellow", ["local_yellow"]),
            ):
                router = self.router(worker)
                router.receive([run, run])
                records = sorted(
                    router.state.read().values(), key=lambda r: r["admission_seq"]
                )
                self.assertEqual([r["item"]["test_id"] for r in records], expected)
            self.run["event"] = "Manual"
            self.run.pop("pull_request_id", None)
            manual = self.client.running_workflows()[0]
            self.assertEqual(
                [c["test_id"] for c in self.client.ci_request(manual)["cases"]],
                [r["test_id"] for r in profile()],
            )

        for field, value in (("kind", "smoke_typo"), ("ngpu", 0)):
            rows = profile()
            rows[0][field] = value
            with self.assertRaises(ValueError):
                source_plan(rows, run, self.client.repo)

    def dual_sha_fixture(self):
        base, head, merge = (letter * 40 for letter in "bcd")
        self.pr["base"]["sha"] = base
        self.pr["head"]["sha"] = head
        self.run["head_sha"] = merge
        self.merge_ref = {"sha": merge, "parents": [{"sha": base}, {"sha": head}]}
        return base, head, merge

    def test_mr_merge_sha_admits_and_reports_on_the_verified_pr_head(self):
        _, head, merge = self.dual_sha_fixture()
        (run,) = self.client.running_workflows()
        with patch(
            "ci_core.ssh_runner.model_archive", return_value=self.archive
        ) as archive:
            plan = self.client.ci_request(run)
            self.assertEqual(archive.call_args.args[0], head)
            self.assertEqual(plan["sha"], head)
            for worker, count in (("blue", 4), ("yellow", 1)):
                router = self.router(worker)
                router._receive_one(run)
                rows = list(router.state.read().values())
                self.assertEqual(len(rows), count)
                for row in rows:
                    stored, item = row["run"], row["item"]
                    self.assertEqual(
                        (stored["head_sha"], stored["_ci_execution_sha"]), (merge, head)
                    )
                    self.assertEqual(row["digest"], plan["plan_digest"])
                    self.assertEqual((row["phase"], item["worker"]), ("QUEUED", worker))
                    result = report(stored, item, row["digest"], "PASS", 0, [], True)
                    post(self.client, stored, item, result, "reporter")
                    self.client.commit_comments.assert_called_with(head)
                    dest, body = self.client.send_result.call_args.args
                    self.assertEqual(
                        (dest, json.loads(body.split("\n", 1)[1])["sha"]), (head, head)
                    )

    def test_dual_sha_rejects_unbound_or_stale_merge_before_discovery(self):
        base, head, merge = self.dual_sha_fixture()
        for proof in (
            {},
            {"sha": "e" * 40, "parents": [{"sha": base}, {"sha": head}]},
            {"sha": merge, "parents": [{"sha": head}, {"sha": base}]},
            {"sha": merge, "parents": [{"sha": base}]},
            {"sha": merge, "parents": [{"sha": base}, {"sha": "e" * 40}]},
            {
                "sha": merge,
                "parents": [{"sha": base}, {"sha": head}, {"sha": "e" * 40}],
            },
        ):
            with self.subTest(proof=proof):
                self.merge_ref = proof
                self.assertEqual(self.client.running_workflows(), [])

    def test_dual_sha_cannot_rebind_after_discovery_or_before_admission(self):
        self.dual_sha_fixture()
        (run,) = self.client.running_workflows()
        router = self.router("yellow")
        with patch("ci_core.ssh_runner.model_archive", return_value=self.archive):
            plan = self.client.ci_request(run)
        with patch.object(self.client, "ci_request", return_value=plan):
            self.pr["head"]["sha"] = "e" * 40
            with self.assertRaises(RunChanged):
                router._receive_one(run)
            self.assertEqual(router.state.read(), {})
            self.assertEqual(self.client.running_workflows(), [])

    def test_dual_sha_queued_case_is_cancelled_and_not_rebound(self):
        _, old_head, _ = self.dual_sha_fixture()
        (run,) = self.client.running_workflows()
        router = self.router("yellow")
        with patch("ci_core.ssh_runner.model_archive", return_value=self.archive):
            router._receive_one(run)
        self.pr["head"]["sha"] = "f" * 40
        with patch.object(router, "pump"):
            router.tick()
        (row,) = router.state.read().values()
        self.assertEqual(
            (row["report"]["status"], row["report"]["sha"]), ("CANCELLED", old_head)
        )

    def test_dual_sha_never_substitutes_a_manual_run_or_an_unidentified_mr(self):
        self.dual_sha_fixture()
        self.run["event"] = "Manual"
        self.assertEqual(self.client.running_workflows(), [])
        self.run["event"] = "MR"
        del self.run["pull_request_id"]
        self.assertEqual(self.client.running_workflows(), [])

    def test_dual_sha_refuses_a_rehashed_plan_for_the_run_sha(self):
        _, _, merge = self.dual_sha_fixture()
        (run,) = self.client.running_workflows()
        with patch("ci_core.ssh_runner.model_archive", return_value=self.archive):
            plan = self.client.ci_request(run)
        plan["sha"] = merge
        del plan["plan_digest"]
        plan["plan_digest"] = digest(plan)
        router = self.router("yellow")
        with (
            patch.object(self.client, "ci_request", return_value=plan),
            self.assertRaisesRegex(ValueError, "plan identity mismatch"),
        ):
            router._receive_one(run)
        self.assertEqual(router.state.read(), {})

    def test_prefix_discovery_ignores_trigger_type(self):
        for event in ("Manual", "MR", "Schedule", "Note", "Push"):
            self.run["event"] = event
            with self.subTest(event=event):
                run = self.client.running_workflows()[0]
                self.assertEqual(run["event"], event)
                self.assertEqual(run["name"], "lite-ci-per-pr-alltest")
                self.assertEqual(run["_ci_pr_number"], 17)
        self.run["workflow_name"] = "ordinary-workflow"
        self.assertEqual(self.client.running_workflows(), [])

    # 看护 Fork PR 的 Head 改变后，旧版本排队用例被取消。
    def test_changed_fork_head_cancels_queued_cases(self):
        with patch("ci_core.ssh_runner.model_archive", return_value=self.archive):
            run = self.client.running_workflows()[0]
            router = self.router("blue")
            router.receive([run])
            self.pr["head"]["sha"] = "b" * 40
            router.tick()
            self.assertTrue(
                all(
                    r["report"]["status"] == "CANCELLED"
                    for r in router.state.read().values()
                )
            )

    # 看护运行中探针收到取消时不能报告成功。
    def test_recovered_model_probe_is_refused_before_source_or_commands(self):
        run = self.client.running_workflows()[0]
        router = self.router("blue")
        row = {
            "kind": "worker_probe",
            "test_id": "old_host_probe",
            "label": "old",
            "nnodes": 0,
            "ngpu": 0,
            "ci_overrides": {"worker": "blue"},
            "timeout": 90,
        }
        plan = source_plan([row], run, self.client.repo)
        item = plan["cases"][0]
        router.state.update(
            "legacy",
            phase="QUEUED",
            run=run,
            item=item,
            digest=plan["plan_digest"],
            defaults=plan["defaults"],
        )
        with (
            patch("ci_core.router.probe_worker") as probe,
            patch("ci_core.router.model_archive") as archive,
        ):
            router.work("legacy", None, None, threading.Event(), threading.Event())
            probe.assert_not_called()
            archive.assert_not_called()
        self.assertEqual(
            router.state.read()["legacy"]["report"]["status"], "INFRA_ERROR"
        )

    # 看护重跑取消旧用例，且新旧结果、执行目录互不混淆。
    def test_rerun_cancels_old_cases_and_uses_distinct_result_and_execution_identity(
        self,
    ):
        with patch("ci_core.ssh_runner.model_archive", return_value=self.archive):
            first = self.client.running_workflows()[0]
            router = self.router("blue")
            router.receive([first])
            self.job["start_time"] += 1000
            second = self.client.running_workflows()[0]
            with patch.object(router, "pump"):
                router.tick()
            records = list(router.state.read().values())
            old = [
                r
                for r in records
                if r["run"]["_ci_dispatch_id"] == "dispatch_1_1767225600000"
            ]
            new = [
                r
                for r in records
                if r["run"]["_ci_dispatch_id"] == "dispatch_1_1767225601000"
            ]
            self.assertEqual((len(old), len(new)), (4, 4))
            self.assertTrue(all(r["report"]["status"] == "CANCELLED" for r in old))
            self.assertTrue(all(r["phase"] == "QUEUED" for r in new))
            self.assertTrue(
                any(
                    c["body"].startswith("Lite-CI-V3 run_1:1:dispatch_1_1767225600000:")
                    for c in self.comments
                )
            )
            case = next(r["item"] for r in old if r["item"]["kind"] == "npu_smoke")

            def instance(run):
                return {
                    **run,
                    "_ci_test_id": case["test_id"],
                    "_ci_case_id": case["case_id"],
                }

            self.assertNotEqual(
                task_dir(instance(first), self.root),
                task_dir(instance(second), self.root),
            )
            self.assertNotEqual(
                remote_task_name(instance(first)), remote_task_name(instance(second))
            )

    # 看护目标仓无法读取指定提交时，从 PR 来源仓获取相同提交。
    def test_fetch_falls_back_to_fork_and_returns_requested_commit(self):
        source = self.root / "fork"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "marker").write_text("fork commit", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "marker"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        sha = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        work = self.root / "work"
        work.mkdir()
        # Isolate rewriting from user configuration, including on Git <2.31.
        home = self.root / "home"
        home.mkdir()
        for remote, repo in [
            ("cann/torchtitan-npu", self.root / "missing"),
            ("someone/torchtitan-npu", source),
        ]:
            subprocess.run(
                [
                    "git",
                    "config",
                    "--file",
                    str(home / ".gitconfig"),
                    f"url.file://{repo}.insteadOf",
                    f"https://gitcode.com/{remote}.git",
                ],
                check=True,
            )
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        with patch.dict(os.environ, env), patch("ci_core.ssh_runner.ROOT", self.root):
            archive = model_archive(
                sha,
                work,
                source_repo=self.client.repo,
                provider="gitcode",
                fallback_repo="someone/torchtitan-npu",
            )
            with tarfile.open(archive, "r:gz") as content:
                self.assertEqual(content.extractfile("marker").read(), b"fork commit")


if __name__ == "__main__":
    unittest.main()
