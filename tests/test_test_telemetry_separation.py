import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ci_core.control_client import TARGET_BYTES, ControlClient
from ci_core.egress_policy import Egress, Rejected, estimate_http1_request_bytes
from ci_core.idle_probe import IdleProbe, _coordinator_leased_resources, observe_health
from ci_core.resources import PhysicalResourceCoordinator


def _sample_pools():
    return {
        "a3_shared_blue": {
            "owner": "blue",
            "enabled": True,
            "process_indexing": "card-die",
            "nodes": [
                {
                    "host_id": "198.51.100.10:22",
                    "ssh": "198.51.100.10:22",
                    "device_ids": [0, 1, 2, 3],
                    "allocation_groups": [
                        {"devices": [0, 1], "verified": True},
                        {"devices": [2, 3], "verified": True},
                    ],
                }
            ],
        }
    }


def _make_record(run_id="900", case_id="case_000_smoke", phase="QUEUED", alloc=None):
    return {
        "phase": phase,
        "queued_at": 1700000000,
        "phase_timestamps": {"QUEUED": 1700000000},
        "allocation": alloc or [],
        "run": {
            "_ci_provider": "github",
            "_ci_repo": "depeng1994/lite-actions",
            "id": int(run_id),
            "run_attempt": 1,
            "name": "lite-ci-pr-smoke",
            "display_title": "PR #52 Hardware Smoke " + ("x" * 100),
            "path": ".github/workflows/lite_actions_pr_smoke.yml",
            "created_at": "2026-11-25T00:00:00Z",
        },
        "item": {
            "case_id": case_id,
            "test_id": "smoke",
            "label": "Smoke " + case_id + " " + ("y" * 80),
            "kind": "npu_smoke",
            "worker": "blue-test-id900a1",
            "target_pool": "a3_shared_blue",
            "resources": {"nodes": 1, "devices_per_node": 2},
        },
    }


class TelemetrySeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_idle_probe_unions_test_worker_coordinator_leases(self):
        lock_root = self.root / "locks"
        coord = PhysicalResourceCoordinator(lock_root)
        lease = coord.acquire(
            [{"host_id": "198.51.100.10:22", "devices": [0, 1]}],
            owner_worker="blue-test-id900a1",
            owner_zone="blue",
            pool="a3_shared_blue",
            is_production=False,
            task_key="depeng1994/lite-actions:900:1:case_000_smoke",
            max_jobs_per_host=2,
            enforce_quarantine=False,
        )
        try:
            leased = _coordinator_leased_resources(lock_root)
            self.assertIn(("198.51.100.10:22", 0), leased)
            self.assertIn(("198.51.100.10:22", 1), leased)

            def fake_smi(_ssh, _cmd, *, timeout=20):
                # Test worker owns chips 0,1; chips 2,3 are idle.
                return SimpleNamespace(
                    stdout="\n".join(
                        [
                            "| NPU Chip | Process id | Process name | Process memory(MB) | Process id in container |",
                            "| 0 0 | 7777 | python | 512 | NA |",
                            "| 0 1 | 7778 | python | 512 | NA |",
                            "| No running processes found in NPU 1 |",
                            "+============================+",
                        ]
                    )
                )

            probe = IdleProbe(
                "blue",
                lambda: (_sample_pools(), {}),
                state_dir=self.root / "state",
                lease_provider=lambda: _coordinator_leased_resources(lock_root),
            )
            with patch("ci_core.idle_probe.observe_health", wraps=observe_health):
                states, completed, _ = observe_health(
                    "blue",
                    _sample_pools(),
                    {},
                    execute=fake_smi,
                    lease_provider=probe.lease_provider,
                )
            self.assertTrue(completed)
            self.assertEqual(
                states[("a3_shared_blue", "198.51.100.10:22", (0, 1))][0], "leased"
            )
            self.assertEqual(
                states[("a3_shared_blue", "198.51.100.10:22", (2, 3))][0], "idle"
            )
        finally:
            coord.release(lease.lease_id, remote_revoked_hosts=["198.51.100.10:22"])

    def test_control_client_forwards_test_telemetry_under_budget_and_strips_probes(
        self,
    ):
        cfg = {
            "state_dir": str(self.root / "main_state"),
            "daily_upload_bytes": 1_000_000,
            "control_center": {"url": "https://control.example.test"},
        }
        policy = Egress("blue", cfg)
        client = ControlClient(
            "blue",
            cfg,
            "t" * 64,
            policy,
            threading.Event(),
            lambda: {"records": {}, "devices": [], "queues": {}, "revision": 1},
        )

        sent = []
        need_snap = {"events_calls": 0}

        def fake_send(route, payload=None):
            req = client._request(route, payload)
            size = estimate_http1_request_bytes(req)
            self.assertLessEqual(size, TARGET_BYTES)
            policy.before(req, repo="control-center", provider="control")
            sent.append((route, payload, size))
            if route == "events":
                need_snap["events_calls"] += 1
                return {"ok": True, "need_snapshot": need_snap["events_calls"] == 1}
            if route == "snapshots":
                return {"ok": True, "complete": True}
            return {"ok": True, "worker_id": payload.get("w")}

        test_wid = "blue-test-id900a1"
        with patch.object(client, "_send", side_effect=fake_send):
            reg = client.register_test_worker(
                {
                    "worker_id": test_wid,
                    "repo": "depeng1994/lite-actions",
                    "pr_number": 52,
                    "pr_head_sha": "a" * 40,
                    "run_id": 900,
                    "attempt": 1,
                    "ttl_seconds": 3600,
                    "expires_at": 1700003600,
                    "status": "ACTIVE",
                }
            )
            self.assertEqual(reg["worker_id"], test_wid)

            task_key = "depeng1994/lite-actions:900:1:case_000_smoke"
            rec = _make_record(
                phase="RUNNING",
                alloc=[{"host_id": "198.51.100.10:22", "devices": [0, 1]}],
            )
            summary = client.forward_test_telemetry(
                test_wid,
                events=[
                    {
                        "t": "IP",
                        "pool": "a3_shared_blue",
                        "h": "198.51.100.10:22",
                        "d": [0, 1],
                        "st": "busy",
                    },
                ],
                snapshot={
                    "revision": 1,
                    "records": {task_key: rec},
                    "devices": [
                        {
                            "pool": "a3_shared_blue",
                            "h": "198.51.100.10:22",
                            "d": [0, 1],
                            "st": "busy",
                        }
                    ],
                    "queues": [
                        {
                            "pool": "a3_shared_blue",
                            "kind": "npu_smoke",
                            "rows": [
                                {
                                    "tid": "abcd12345678",
                                    "pos": 0,
                                    "ix": [[i] for i in range(120)],
                                }
                            ],
                        }
                    ],
                },
            )
            self.assertGreaterEqual(summary["dropped_probes"], 2)
            self.assertGreaterEqual(summary["snapshots"], 1)

            # Verify test tid namespacing matches Control Center taskIdentity().
            expected_tid = hashlib.sha256(
                f"{task_key}:reporter:{test_wid}".encode()
            ).hexdigest()[:12]
            task_packets = [
                p for r, p, _ in sent if r == "snapshots" and p.get("kind") == "task"
            ]
            self.assertTrue(task_packets)
            self.assertEqual(task_packets[0]["items"][0]["tid"], expected_tid)
            self.assertEqual(task_packets[0]["w"], test_wid)

            # Verify no IP or device snapshot frames were emitted, and main outbox untouched.
            self.assertFalse(
                any(r == "snapshots" and p.get("kind") == "device" for r, p, _ in sent)
            )
            for r, p, _ in sent:
                if r == "events":
                    self.assertFalse(any(ev.get("t") == "IP" for ev in p["ev"]))
            self.assertEqual(client.outbox.view()["seq"], 0)

            dereg = client.deregister_test_worker(test_wid, reason="cleanup")
            self.assertEqual(dereg["worker_id"], test_wid)

        with self.assertRaisesRegex(ValueError, "WORKER_ZONE_MISMATCH"):
            client.register_test_worker(
                {
                    "worker_id": "yellow-test-id900a1",
                    "repo": "depeng1994/lite-actions",
                    "run_id": 900,
                    "attempt": 1,
                }
            )

    def test_egress_rejects_direct_control_requests_from_test_worker(self):
        (self.root / "yellow_zone").mkdir(parents=True, exist_ok=True)
        cfg = {
            "state_dir": str(self.root / "yellow_test"),
            "egress_ledger_dir": str(self.root / "yellow_zone"),
            "daily_upload_bytes": 1_000_000,
            "control_center": {"url": "https://control.example.test"},
        }
        test_egress = Egress("yellow-test-id900a1", cfg)
        for path, method, data in (
            ("/api/v1/worker/heartbeat", "POST", b'{"w":"yellow-test-id900a1"}'),
            ("/api/v1/worker/commands", "GET", None),
            ("/api/v1/worker/events", "POST", b'{"w":"yellow-test-id900a1","ev":[]}'),
            ("/api/v1/worker/snapshots", "POST", b'{"w":"yellow-test-id900a1"}'),
            (
                "/api/v1/worker/test-workers/register",
                "POST",
                b'{"w":"yellow-test-id900a1"}',
            ),
        ):
            req = urllib.request.Request(
                f"https://control.example.test{path}", data=data, method=method
            )
            with self.assertRaisesRegex(Rejected, "UNTRUSTED_CONTROL_DESTINATION"):
                test_egress.before(req, repo="control-center", provider="control")

        main_egress = Egress("yellow", cfg)
        payload = json.dumps(
            {
                "w": "yellow-test-id900a1",
                "z": "yellow",
                "r": "depeng1994/lite-actions",
                "rid": "900",
            }
        ).encode()
        req = urllib.request.Request(
            "https://control.example.test/api/v1/worker/test-workers/register",
            data=payload,
            method="POST",
        )
        admitted = main_egress.before(req, repo="control-center", provider="control")
        self.assertIs(admitted, req)


if __name__ == "__main__":
    unittest.main()
