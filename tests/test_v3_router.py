"""CPU E2E: two independent Pull workers, FIFO leases and durable result outbox."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ci_core.contract import normalize, create_plan
from ci_core.router import Router, task_key
from ci_core.dispatch import Execution
from ci_core.resources import reserve, ResourceBusy
from ci_core.v2_reporting import report, body_for, post
from ci_core.v2_config import load, runtime_case
from ci_core.transport import command

ROOT = Path(__file__).resolve().parents[1]
D = dict(worker="blue", target_pool="a3_shared_blue", docker="image:tag")


class Client:
    def __init__(self, plans):
        self.plans = plans
        self.comments = []
        self.completed = False

    def ci_request(self, run):
        return self.plans[run["id"]]

    def running_workflows(self):
        return []

    def workflow_status(self, i):
        return dict(status="completed" if self.completed else "in_progress")

    def commit_comments(self, sha):
        return self.comments

    def send_result(self, sha, body):
        c = dict(id=len(self.comments) + 1, body=body, user=dict(login="reporter"))
        self.comments.append(c)
        return c

    def request(self, method, path, data):
        self.comments[int(path.rsplit("/", 1)[1]) - 1].update(data)


def make_plan(workers=("blue",), kind="train", ident=20):
    items = []
    for i, w in enumerate(workers):
        t = SimpleNamespace(
            test_name=f"case_{i}",
            test_descr=f"Case {i}",
            nnodes=1,
            ngpu=8,
            timeout=90 if kind == "worker_probe" else 7200,
            ci_kind=kind,
            ci_overrides={"worker": w},
        )
        items.append(normalize(t, D, i))
    p = create_plan(
        items,
        run_id=ident,
        attempt=1,
        sha="a" * 40,
        workflow="nightly_fulltests.yml",
        defaults=D,
    )
    run = dict(
        id=ident,
        run_attempt=1,
        head_sha="a" * 40,
        path=".github/workflows/nightly_fulltests.yml",
        created_at="2026-10-30T00:00:00Z",
        status="in_progress",
        event="workflow_dispatch",
        head_branch="master",
        actor=dict(login="maintainer"),
        name="Nightly",
        run_number=1,
    )
    return run, p


class RouterV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        for f in (ROOT / "config").glob("*.json"):
            (self.root / "config" / f.name).write_bytes(f.read_bytes())
        path = self.root / "config/pools.json"
        pools = json.loads(path.read_text())
        for n in pools["a3_shared_blue"]["nodes"]:
            n["environments"] = {
                "image:tag": {
                    "container_id": "a" * 64,
                    "image_id": "sha256:" + "b" * 64,
                }
            }
        path.write_text(json.dumps(pools))
        self.env = patch.dict(
            "os.environ",
            LITE_ACTIONS_ALLOWED_ACTORS="maintainer",
            LITE_ACTIONS_RESOURCE_LOCK_ROOT=str(self.root / "leases"),
        )
        self.env.start()
        self.routers = []

    def tearDown(self):
        for r in self.routers:
            r.close()
        self.env.stop()
        self.temp.cleanup()

    def router(self, worker, client):
        root = self.root / worker
        (root / "config").mkdir(parents=True)
        for f in (self.root / "config").glob("*.json"):
            (root / "config" / f.name).write_bytes(f.read_bytes())
        r = Router(root, worker, client, "reporter")
        self.routers.append(r)
        return r

    def test_each_worker_only_receives_own_cases_and_deduplicates(self):
        run, p = make_plan(("blue", "yellow"), "worker_probe")
        c = Client({20: p})
        blue = self.router("blue", c)
        yellow = self.router("yellow", c)
        for r in [blue, yellow]:
            r.receive([run])
            r.receive([run])
        self.assertEqual(len(blue.state.read()), 1)
        self.assertEqual(len(yellow.state.read()), 1)
        self.assertEqual(
            next(iter(yellow.state.read().values()))["item"]["worker"], "yellow"
        )

    def test_declines_untrusted_manual_actor(self):
        run, p = make_plan()
        run["actor"]["login"] = "stranger"
        r = self.router("blue", Client({20: p}))
        r.receive([run])
        self.assertEqual(r.state.read(), {})

    def test_disabled_and_wrong_owner_pools_fail_before_ssh(self):
        _, p = make_plan()
        _, pools = load(self.root, "blue")
        for pool, worker in [
            ("a3_shared_yellow", "yellow"),
            ("a3_shared_blue", "yellow"),
        ]:
            with self.subTest(pool=pool), self.assertRaises(ValueError):
                runtime_case({**p["cases"][0], "target_pool": pool}, pools, worker)

    def test_atomic_claim_and_same_host_capacity(self):
        run, p = make_plan()
        r = self.router("blue", Client({20: p}))
        case = runtime_case(p["cases"][0], r.pools, "blue")
        leases = []
        alloc = []
        try:
            for _ in range(4):
                lease, selected = r.claim(case)
                leases.append(lease)
                alloc += [
                    (n["host_id"], d)
                    for n in selected["allocation"]
                    for d in n["devices"]
                ]
            self.assertEqual(len(set(alloc)), 32)
            self.assertIsNone(r.claim(case))
        finally:
            for lease in leases:
                lease.close()

    def test_quarantined_devices_are_never_admitted(self):
        run, p = make_plan()
        r = self.router("blue", Client({20: p}))
        pool = r.pools["a3_shared_blue"]
        r.state.update(
            "orphan",
            phase="REPORTED",
            quarantined=True,
            allocation=[
                dict(host_id=n["host_id"], devices=n["device_ids"])
                for n in pool["nodes"]
            ],
        )
        self.assertIsNone(r.claim(runtime_case(p["cases"][0], r.pools, "blue")))

    def test_fifo_head_blocks_later_case_in_same_pool(self):
        run, p = make_plan(("blue", "blue"))
        r = self.router("blue", Client({20: p}))
        r.receive([run])
        with patch.object(r, "claim", return_value=None) as claim:
            r.pump()
        self.assertEqual(claim.call_count, 1)

    def test_actual_parallel_cases_hold_disjoint_leases_through_execution(self):
        run, p = make_plan(("blue", "blue"))
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        entered = threading.Barrier(3)
        release = threading.Event()
        allocs = []

        def execute(case, subrun, **kw):
            allocs.append(case["allocation"])
            entered.wait(5)
            release.wait(5)
            log = r.root / (subrun["_ci_test_id"] + ".log")
            log.write_text(f"CASE_RESULT {subrun['_ci_test_id']} PASS 0\n")
            return Execution(0, ["step: 5 loss: 1.0"], log, True)

        with (
            patch("ci_core.router.model_archive", return_value=Path("/unused")),
            patch("ci_core.router.verify_definition"),
            patch("ci_core.router.execute", side_effect=execute),
        ):
            r.pump()
            entered.wait(5)
            self.assertEqual(len(r.active), 2)
            for allocation in allocs:
                with self.assertRaises(ResourceBusy):
                    with reserve(self.root / "leases/devices", allocation):
                        pass
            release.set()
            for future, _, _ in r.active.values():
                future.result(5)
            r.pump()
            r.flush()
        self.assertTrue(
            all(
                x["phase"] == "REPORTED" and x["report"]["status"] == "PASS"
                for x in r.state.read().values()
            )
        )

    def test_cancel_queued_case_never_launches(self):
        run, p = make_plan()
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        c.completed = True
        r.tick()
        rec = next(iter(r.state.read().values()))
        self.assertEqual(rec["report"]["status"], "CANCELLED")
        self.assertFalse(r.active)

    def test_outbox_failure_retries_without_reexecution(self):
        run, p = make_plan()
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        key = task_key(run, p["cases"][0])
        r.finish(key, "PASS", 0, ["done"])
        with patch.object(c, "send_result", side_effect=OSError("offline")):
            r.flush()
        self.assertEqual(r.state.read()[key]["phase"], "PENDING_REPORT")
        r.flush()
        self.assertEqual(r.state.read()[key]["phase"], "REPORTED")

    def test_restart_quarantines_without_relaunch(self):
        run, p = make_plan()
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        key = task_key(run, p["cases"][0])
        r.state.update(
            key,
            phase="RUNNING",
            allocation=[dict(host_id="113.46.17.205:153", devices=list(range(8)))],
        )
        recovered = Router(r.root, "blue", c, "reporter")
        self.routers.append(recovered)
        self.assertTrue(recovered.state.read()[key]["quarantined"])
        self.assertFalse(recovered.active)

    def test_new_alias_container_transport_has_no_scp_colon_ambiguity(self):
        args = command("113.46.17.205:153/" + "a" * 64, "printf hello")
        self.assertIn("113.46.17.205:153", args)
        self.assertIn("docker exec -i", args[-1])
        with self.assertRaises(ValueError):
            command("113.46.17.205:153/evil;rm", "true")

    def test_utf8_and_nested_json_budgets(self):
        run, p = make_plan()
        item = p["cases"][0]
        result = report(
            run,
            item,
            p["plan_digest"],
            "FAIL",
            1,
            ["中文\t\x1b[31m" * 100 for _ in range(100)],
        )
        body = body_for(run, item, result)
        r = json.loads(body.split("\n", 1)[1])
        self.assertLessEqual(len(r["last_20_lines"]), 20)
        self.assertTrue(
            all(len(x) <= 180 and "\t" not in x for x in r["last_20_lines"])
        )
        self.assertLessEqual(len(body.encode()), 6100)
        self.assertLessEqual(
            len(json.dumps({"body": body}, separators=(",", ":")).encode()), 7000
        )

    def test_ambiguous_physical_fingerprint_rejected(self):
        path = self.root / "config/pools.json"
        p = json.loads(path.read_text())
        for n in p["a3_shared_blue"]["nodes"]:
            n["machine_fingerprint"] = "same"
        path.write_text(json.dumps(p))
        with self.assertRaises(ValueError):
            load(self.root, "blue")

    def test_unbound_unallocated_node_does_not_block_8p_admission(self):
        run, p = make_plan()
        r = self.router("blue", Client({20: p}))
        r.pools["a3_shared_blue"]["nodes"][1]["environments"] = {}
        case = runtime_case(p["cases"][0], r.pools, "blue")
        lease, selected = r.claim(case)
        try:
            self.assertEqual(selected["allocation"][0]["host_id"], "113.46.17.205:153")
        finally:
            lease.close()

    def test_missing_binding_fails_before_device_lock(self):
        run, p = make_plan()
        r = self.router("blue", Client({20: p}))
        for n in r.pools["a3_shared_blue"]["nodes"]:
            n["environments"] = {}
        with self.assertRaisesRegex(ValueError, "CONTAINER_BINDING_MISSING"):
            r.claim(runtime_case(p["cases"][0], r.pools, "blue"))
        self.assertFalse((self.root / "leases/devices").exists())

    def test_superseded_attempt_is_cancelled_before_new_launch(self):
        run, p = make_plan()
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        with patch.object(
            c, "workflow_status", return_value=dict(status="in_progress", run_attempt=2)
        ):
            r.tick()
        record = next(iter(r.state.read().values()))
        self.assertEqual(record["report"]["status"], "CANCELLED")
        self.assertFalse(r.active)

    def test_interrupted_admission_recovers_to_queue(self):
        run, p = make_plan()
        c = Client({20: p})
        r = self.router("blue", c)
        r.receive([run])
        key = task_key(run, p["cases"][0])
        r.state.update(key, phase="RECEIVED")
        restarted = Router(r.root, "blue", c, "reporter")
        self.routers.append(restarted)
        self.assertEqual(restarted.state.read()[key]["phase"], "QUEUED")

    @unittest.skipUnless(
        __import__("sys").platform == "linux", "Requires real Linux /proc"
    )
    def test_cpu_initializing_child_blocks_release_even_with_exit_record(self):
        import os
        import subprocess
        from ci_core.ssh_runner import owned_processes_gone

        folder = self.root / "remote"
        folder.mkdir()
        marker = "isolated-v3-cleanup-test"
        (folder / "job.intent").write_text(marker)
        (folder / "exit_code.txt").write_text("0\n")
        child = subprocess.Popen(
            ["sleep", "30"], env={**os.environ, "LITE_CI_JOB_ID": marker}
        )

        def local_ssh(host, script, **kwargs):
            completed = subprocess.run(
                ["bash", "-lc", script], capture_output=True, text=True
            )
            if completed.returncode:
                raise RuntimeError("owned process remains")
            return completed

        try:
            with patch("ci_core.ssh_runner.ssh", side_effect=local_ssh):
                with self.assertRaises(RuntimeError):
                    owned_processes_gone({"ssh": "isolated"}, str(folder), marker)
                child.terminate()
                child.wait(5)
                owned_processes_gone({"ssh": "isolated"}, str(folder), marker)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
