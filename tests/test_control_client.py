import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from ci_core.control_client import ControlClient, task_descriptor
from ci_core.egress_policy import Deferred, Egress


def record(now=1, phase="QUEUED"):
    return {
        "phase": phase,
        "queued_at": now,
        "phase_timestamps": {"QUEUED": now},
        "run": {"_ci_repo": "owner/repo", "id": "1"},
        "item": {
            "case_id": "case_000_demo",
            "kind": "npu_smoke",
            "target_pool": "a3_shared_blue",
            "resources": {"nodes": 1, "devices_per_node": 8},
        },
    }


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = {
            "state_dir": self.directory.name,
            "daily_upload_bytes": 1_000_000,
            "control_center": {"url": "https://control.example.test"},
        }
        self.responses = {
            "heartbeat": {"pending_commands": 0, "need_snapshot": False},
            "events": {"need_snapshot": False},
            "snapshots": {"complete": True},
            "commands": {
                "commands": [{"cmd_id": "test", "cmd_type": "MOVE_UP", "payload": {}}]
            },
            "command-receipts": {"ok": True},
        }
        self.records = {"owner/repo:1:1:case_000_demo": record()}
        self.now = 100
        self.calls = []

    def client(self, worker="blue"):
        return ControlClient(
            worker,
            self.cfg,
            "x" * 64,
            Egress(worker, self.cfg),
            threading.Event(),
            lambda: {"records": self.records},
            clock=lambda: self.now,
        )

    def send(self, route, body=None):
        self.calls.append((route, body))
        return self.responses[route]

    def test_heartbeat_only_pulls_commands_when_needed_and_wakes_router(self):
        client = self.client()
        with patch.object(client, "_send", side_effect=self.send):
            client.cycle()
            self.assertNotIn("commands", [route for route, _ in self.calls])
            self.responses["heartbeat"]["pending_commands"] = 1
            self.now += 30
            client.cycle()
            self.assertTrue(client.wake.is_set())
            self.assertEqual(client.commands()[0]["cmd_id"], "test")
            client.complete("test", "APPLIED", pos=1)
            client.cycle()
        self.assertEqual(client.commands(), [])
        self.assertEqual(
            [
                body["status"]
                for route, body in self.calls
                if route == "command-receipts"
            ],
            ["ACKED", "APPLIED"],
        )

    def test_persist_and_retry_without_importing_old_terminal_tasks(self):
        client = self.client()
        old = record(2, "REPORTED")
        old["report"] = {"status": "PASS", "cleanup_verified": True}
        old["phase_timestamps"]["FINISHED"] = 2
        self.records["old"] = old
        error = urllib.error.HTTPError(
            "https://example.test", 503, "unavailable", {}, None
        )
        with (
            patch.object(client, "_send", side_effect=error),
            self.assertRaises(urllib.error.HTTPError),
        ):
            client.cycle()
        pending = client.outbox.view()
        self.assertEqual([event["t"] for event in pending["events"]], ["TU", "TP"])
        restored = self.client()
        self.assertEqual(restored.outbox.view(), pending)
        with patch.object(restored, "_send", side_effect=self.send):
            restored.cycle()
        self.assertEqual(restored.outbox.view()["events"], [])
        self.assertNotIn("old", restored.outbox.view()["tracked"])

    def test_snapshot_resumes_after_deferral(self):
        client = self.client()
        task = task_descriptor("k", record())
        tasks = [{**task, "lbl": "x" * 115} for _ in range(50)]
        calls = []

        def interrupt(route, body):
            calls.append(body)
            if len(calls) == 2:
                raise Deferred("busy")
            return {"complete": body["p"] == body["n"]}

        with patch.object(client, "_send", side_effect=interrupt):
            with self.assertRaises(Deferred):
                client.snapshot({"w": "blue", "i": "deadbeef"}, {"task": tasks}, 0)
        restored = self.client()
        with patch.object(restored, "_send", side_effect=interrupt):
            restored._send_snapshot()
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(sum(len(page["items"]) for page in [calls[0], *calls[2:]]), 50)
        self.assertTrue(all(page["sid"] == calls[0]["sid"] for page in calls))

    def test_control_reserve_leaves_bytes_for_platform(self):
        self.cfg["daily_upload_bytes"] = 3200
        self.cfg["control_center"]["ci_reserve_bytes"] = 3000
        client = self.client("yellow")
        with self.assertRaisesRegex(Deferred, "CONTROL_CI_RESERVE"):
            client._send("heartbeat", {"w": "yellow"})
        client.policy.reserve(False, 400, provider="github", category="terminal")
        ledger = json.loads(
            (Path(self.cfg["state_dir"]) / "http-egress.json").read_text()
        )
        self.assertEqual(ledger["used"], 400)

    def test_rebases_before_first_available_sequence_after_gap(self):
        self.records.clear()
        client = self.client()
        client.outbox.enqueue({"t": "IP", "ts": 100})
        client.outbox.enqueue({"t": "IP", "ts": 101})
        client.outbox.change(lambda data: data["events"].pop(0))
        remote = {"seq": 0}

        def server(route, payload):
            self.calls.append((route, payload))
            if route == "snapshots":
                remote["seq"] = payload["seq"]
            if route == "events":
                events = payload["ev"]
                if events[0]["s"] != remote["seq"] + 1:
                    return {"need_snapshot": True, "expected_seq": remote["seq"] + 1}
                remote["seq"] = events[-1]["s"]
            return self.responses[route]

        with patch.object(client, "_send", side_effect=server):
            client.cycle()
            client.cycle()
        self.assertEqual(remote["seq"], 2)
        self.assertEqual(client.outbox.view()["events"], [])
        self.assertIn(
            ("snapshots", 1), [(route, body.get("seq")) for route, body in self.calls]
        )

    def test_large_single_allocation_and_queue_preserve_every_item_across_packets(self):
        client = self.client()
        base = {"w": "blue", "i": "0a0b0c0d"}
        allocation = [
            {"h": f"198.51.100.{i}:22", "d": list(range(256))} for i in range(1, 9)
        ]
        parts = client._lease_parts(base, "b" * 12, 450, allocation)
        self.assertGreater(len(parts), 1)
        self.assertEqual([host for part in parts for host in part["a"]], allocation)
        options = [list(range(i, i + 16)) for i in range(350)]
        queues = {("mypool", "npu_smoke"): [{"tid": "0" * 12, "pos": 0, "ix": options}]}
        pages = client._queue_pages(queues, 3, base)
        self.assertGreater(len(pages), 1)
        self.assertEqual(
            [option for page in pages for row in page["items"] for option in row["ix"]],
            options,
        )
        with patch.object(client, "_send", side_effect=self.send):
            client.snapshot(base, {"queue": pages}, 1)
        self.assertGreater(len(self.calls), 1)
