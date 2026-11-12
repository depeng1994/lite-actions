"""Hardware-free adverse boundaries; real release/worker/organization evidence is separate."""

import io
import json
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ci_core.config import identify_worker
from ci_core.contract import normalize
from ci_core.egress_policy import Deferred, Egress, Rejected, yellow_body
from ci_core.logs import yellow_lines
from ci_core.registry import CATALOG, inspect_archive
from ci_core.reporting import body_for, report
from tests.scenarios import DEFAULTS, definition


class Rfc24Boundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 864000.0
        self.cfg = {
            "state_dir": self.tmp.name,
            "daily_upload_bytes": 1000000,
            "artifact_hosts": ["storage.invalid"],
        }

    def policy(self):
        return Egress("yellow", self.cfg, clock=lambda: self.now)

    def test_role_exact_and_no_override(self):
        mapping = {"blue": "192.0.2.7", "yellow": "192.0.2.8"}
        self.assertEqual(identify_worker(["192.0.2.8"], mapping), "yellow")
        for addresses, asserted in [
            ([], None),
            (["192.0.2.9"], None),
            (list(mapping.values()), None),
            (["192.0.2.8"], "blue"),
        ]:
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                identify_worker(addresses, mapping, asserted)
        with self.assertRaises(Rejected):
            Egress("unknown", self.cfg)

    def test_cli_refuses_before_authorization_and_http(self):
        import unified_router

        with (
            patch.object(sys, "argv", ["unified_router.py", "--once"]),
            patch.object(
                unified_router,
                "deployment",
                side_effect=ValueError("WORKER_IDENTITY_MISMATCH"),
            ),
            patch.object(unified_router, "authorization") as auth,
            patch.object(urllib.request, "build_opener") as http,
            self.assertRaisesRegex(ValueError, "IDENTITY"),
        ):
            unified_router.main()
        auth.assert_not_called()
        http.assert_not_called()

    def test_yellow_envelope_on_wire_and_bypass(self):
        run = {"id": 123, "head_sha": "a" * 40, "path": ".github/workflows/smoke.yml"}
        item = normalize(
            definition("task", kind="npu_smoke", worker="yellow"), DEFAULTS, 0
        )
        lines = [
            "Bearer example-canary",
            "api_\x1b[0mkey=never-print",
            "一" * 300 + "\u2028more",
        ] + ["长" * 180] * 15
        result = report(run, item, "b" * 64, "PASS", 0, lines)
        body = body_for(run, item, result, local_worker="yellow")
        payload = json.dumps(
            {"body": body}, separators=(",", ":"), ensure_ascii=False
        ).encode()
        yellow_body(payload, "a" * 40)
        inner = json.loads(body.split("\n", 1)[1])
        out = inner["last_20_lines"]
        self.assertLessEqual(len(out), 10)
        self.assertLessEqual(len("\n".join(out).encode()), 2048)
        self.assertLessEqual(len(payload), 7000)
        self.assertNotIn("never-print", body)
        self.assertNotIn("canary", body)
        rk = "depeng1994/lite-actions"
        base = "https://api.github.com/repos/" + rk
        for dest, method, content in [
            (base + "/check-runs", "POST", payload),
            (base + "/actions/workflows/4/dispatches", "POST", payload),
            (base + "/comments/2", "PATCH", payload),
            (base + "/actions/runs?data=leak", "GET", None),
            ("https://example.invalid/", "GET", None),
        ]:
            with self.subTest(dest=dest), self.assertRaises(Rejected):
                self.policy().before(
                    urllib.request.Request(dest, data=content, method=method),
                    repo=rk,
                    provider="github",
                )
        self.assertFalse(self.policy().path.exists())
        for mutation in [
            {"worker": "blue"},
            {"hidden": "raw"},
            {"resources": {"nodes": 1, "extra": "raw"}},
            {"last_20_lines": ["x"] * 11},
        ]:
            data = json.dumps(
                {"body": body.split("\n", 1)[0] + "\n" + json.dumps(inner | mutation)}
            ).encode()
            with self.assertRaises(Rejected):
                yellow_body(data, "a" * 40)

    def test_key_material_removed_as_a_block(self):
        text = "-----BEGIN PRIVATE KEY-----\nprivate-fixture-value\n-----END PRIVATE KEY-----\nOK"
        self.assertEqual(yellow_lines([text]), ["[REDACTED]"] * 3 + ["OK"])

    def test_request_wire_budget_accepts_1999_and_rejects_2000_offline(self):
        from urllib.parse import urlsplit

        class RequestFixture:
            def __init__(self, pad):
                self.full_url = "https://api.github.com/repos/cann-framework/torchtitan-npu/branches/main"
                self.data = None
                self.pad = pad

            def get_method(self):
                return "GET"

            def header_items(self):
                return [("X-Pad", "x" * self.pad)]

        def make(target):
            url = urlsplit(
                "https://api.github.com/repos/cann-framework/torchtitan-npu/branches/main"
            )
            line = len(f"GET {url.path} HTTP/1.1\r\n".encode())
            host = len(f"Host: {url.netloc}\r\n".encode())
            empty = line + host + 9 + 2 + 128
            return RequestFixture(target - empty)

        for target in (1999, 2000):
            req = make(target)
            u = urlsplit(req.full_url)
            headers = sum(
                len(k.encode()) + 2 + len(v.encode()) + 2 for k, v in req.header_items()
            )
            actual = (
                len(f"GET {u.path} HTTP/1.1\r\n".encode())
                + len(f"Host: {u.netloc}\r\n".encode())
                + headers
                + 2
                + 128
            )
            self.assertEqual(actual, target)
            if target == 1999:
                self.policy().before(
                    req, repo="cann-framework/torchtitan-npu", provider="github"
                )
            else:
                with self.assertRaisesRegex(Rejected, "REQUEST_SIZE_LIMIT"):
                    self.policy().before(
                        req, repo="cann-framework/torchtitan-npu", provider="github"
                    )
        state = json.loads(self.policy().path.read_bytes())
        row = state["classified"]["github:metadata_get"]
        self.assertEqual(
            (row["sent"], row["rejected"], row["max_request_bytes"]), (1, 1, 2000)
        )

    def test_discovery_get_caps_are_per_repo_and_600_seconds(self):
        policy = self.policy()
        repo = "cann-framework/torchtitan-npu"
        url = f"https://api.github.com/repos/{repo}/actions/runs?status=queued&per_page=100&page=1"
        for _ in range(20):
            policy.before(urllib.request.Request(url), repo=repo, provider="github")
            self.now += 1
        with self.assertRaisesRegex(Deferred, "DISCOVERY_POLL_LIMIT"):
            policy.before(urllib.request.Request(url), repo=repo, provider="github")
        self.now += 600
        policy.before(urllib.request.Request(url), repo=repo, provider="github")
        state = json.loads(policy.path.read_bytes())
        self.assertEqual(state["discovery_600s"][f"github:{repo}"]["count"], 1)

    def test_restart_and_clock_do_not_replenish_daily_or_burst_allowance(self):
        self.cfg["daily_upload_bytes"] = 19
        g = self.policy()
        for _ in range(4):
            g.reserve(False, 3, 1)
        with self.assertRaises(Deferred):
            self.policy().reserve(False, 1)
        self.now += 1800
        self.policy().reserve(False, 7, 2)
        with self.assertRaises(Deferred):
            self.policy().reserve(True, 1)
        state = json.loads(g.path.read_bytes())
        self.assertEqual(
            (state["used"], state["write_attempts"], state["body_bytes"]), (19, 5, 6)
        )
        self.now -= 2000
        with self.assertRaises(Deferred):
            self.policy().reserve(True, 1)
        self.now = 11 * 86400 + 1
        self.policy().reserve(True, 5)

    def test_gitcode_merge_ref_read_is_bounded_to_one_native_ref_shape(self):
        repo = "cann/torchtitan-npu"
        base = f"https://api.gitcode.com/api/v5/repos/{repo}/commits/"
        ref = "refs%2Fmerge-requests%2F1244%2Fmerge"
        policy = self.policy()
        policy.before(urllib.request.Request(base + ref), repo=repo, provider="gitcode")
        for bad in (
            ref.replace("1244", "x"),
            ref.replace("merge-requests", "heads"),
            ref + "?page=1",
            ref + "%2Fother",
        ):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                policy.before(
                    urllib.request.Request(base + bad), repo=repo, provider="gitcode"
                )
        with self.assertRaises(Rejected):
            policy.before(
                urllib.request.Request(base + ref, method="POST", data=b"{}"),
                repo=repo,
                provider="gitcode",
            )

    def test_virtual_hour_provider_discovery_and_unsigned_storage(self):
        g = self.policy()
        repo = "cann/torchtitan-npu"
        ghrepo = "cann-framework/torchtitan-npu"
        for minute in range(60):
            self.now += 60
            for status in ("queued", "in_progress"):
                g.before(
                    urllib.request.Request(
                        f"https://api.github.com/repos/{ghrepo}/actions/runs?status={status}&per_page=100&page=1"
                    ),
                    repo=ghrepo,
                    provider="github",
                )
            g.before(
                urllib.request.Request(
                    f"https://api.gitcode.com/api/v8/repos/{repo}/actions/runs?status=RUNNING&per_page=100&page=1"
                ),
                repo=repo,
                provider="gitcode",
            )
        self.assertEqual(json.loads(g.path.read_bytes())["read_attempts"], 180)
        for u, h in [
            ("https://unknown.invalid/a", {}),
            ("https://storage.invalid/a", {"Private-token": "secret"}),
        ]:
            with self.assertRaises(Rejected):
                g.before(
                    urllib.request.Request(u, headers=h),
                    repo=repo,
                    provider="gitcode",
                    storage=True,
                )

    def test_poisoned_model_is_never_run_by_catalog_reader(self):
        # Positive control uses exactly the same innocuous payload on a loopback listener.
        marker = self.root / "sentinel"
        archive = self.root / "model.tar.gz"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            code = f'import socket,pathlib; pathlib.Path({str(marker)!r}).write_text("hit");socket.create_connection({listener.getsockname()!r},timeout=1).close()'.encode()
            subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=3)
            listener.accept()[0].close()
            self.assertEqual(marker.read_text(), "hit")
            marker.unlink()
            catalog = (Path(__file__).resolve().parents[1] / CATALOG).read_bytes()

            def write(members):
                with tarfile.open(archive, "w:gz") as tf:
                    for name, data in members:
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        tf.addfile(info, io.BytesIO(data))

            model = ("tests/integration_tests/tools/lite_actions/entrypoint.py", code)
            write([model, (CATALOG, catalog)])
            with patch(
                "subprocess.Popen", side_effect=AssertionError("unexpected child")
            ):
                rows = inspect_archive(archive, {"suite": "dispatch_smoke"})
                self.assertEqual(len(rows), 3)
                for row in rows:
                    self.assertEqual(
                        inspect_archive(archive, {"test_id": row["test_id"]}), [row]
                    )
                for members in [
                    [model],
                    [(CATALOG, catalog), ("./" + CATALOG, catalog)],
                    [(CATALOG, b'{"version":1,"version":1}')],
                ]:
                    write(members)
                    with self.assertRaisesRegex(ValueError, "CATALOG_"):
                        inspect_archive(archive, {"suite": "dispatch_smoke"})
            self.assertFalse(marker.exists())
            listener.settimeout(0.1)
            with self.assertRaises(TimeoutError):
                listener.accept()

    def test_release_bytes_recovery_and_tombstone(self):
        import hashlib

        from ci_core.github import ReleaseLogs
        from ci_core.logs import BLUE_BYTES, blue_diagnostic

        dest = {
            "repository": "example/public-ci-logs",
            "release_id": 13,
            "tag": "ci-logs",
            "publisher_login": "fixture-bot",
            "public_diagnostics_approved": True,
        }
        cfg = self.cfg | {"public_release": dest}
        repo = dest["repository"]
        assets = {}
        uploads = []

        def api(method, path, *, data=None):
            if method == "DELETE":
                self.assertEqual(path, "/releases/assets/81")
                assets.clear()
                return None
            if data is not None:
                self.assertEqual(method, "POST")
                name = path.split("name=")[1]
                uploads.append(data)
                assets[name] = {
                    "id": 81,
                    "name": name,
                    "size": len(data),
                    "state": "uploaded",
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "browser_download_url": f"https://github.com/{repo}/releases/download/ci-logs/{name}",
                    "uploader": {"login": "fixture-bot"},
                }
                raise TimeoutError("simulated loss AFTER upload commit")
            if path == "":
                return {"full_name": repo, "private": False}
            if path == "/releases/13":
                return {"id": 13, "draft": False, "tag_name": "ci-logs"}
            return list(assets.values())

        with self.assertRaises(ValueError):
            ReleaseLogs("yellow", cfg, token="dummy")
        value = b"abcdefgh" * (BLUE_BYTES // 8)
        self.assertEqual(blue_diagnostic(value), (value, False))
        self.assertEqual(blue_diagnostic(value + b"x"), (value, True))
        self.assertEqual(blue_diagnostic(b"foo\n" * 500), (b"foo\n" * 500, False))
        self.assertEqual(
            blue_diagnostic(b"foo\nAuthorization: secret\n"),
            (b"foo\n[REDACTED]\n", False),
        )
        prefix = ("a" * (BLUE_BYTES - 2) + "中").encode()
        self.assertEqual(blue_diagnostic(prefix), (prefix[:-3], True))
        with patch.object(ReleaseLogs, "api_call", side_effect=api):
            writer = ReleaseLogs("blue", cfg, token="dummy", clock=lambda: self.now)
            ref = writer.deliver("scope-key", value, False)
            self.assertEqual(
                (ref["bytes"], ref["sha256"]),
                (BLUE_BYTES, hashlib.sha256(value).hexdigest()),
            )
            resumed = ReleaseLogs("blue", cfg, token="dummy", clock=lambda: self.now)
            self.assertEqual(resumed.deliver("scope-key", value, False), ref)
            self.assertEqual(len(uploads), 1)
            self.now += 7 * 86400 + 1
            resumed.expire()
            self.assertFalse(assets)
            with self.assertRaisesRegex(ValueError, "EXPIRED"):
                resumed.deliver("scope-key", value, False)
            self.assertEqual(len(uploads), 1)

    def test_host_action_needs_attested_callee_and_active_attempt(self):
        from types import SimpleNamespace

        from ci_core.worker_probe import JOB_NAME, TEMPLATE, github_call

        repo = "cann-framework/vllm-ascend"
        head = "a" * 40
        tpl = "b" * 40
        path = ".github/workflows/dispatcher.yml"
        run = {
            "id": 3,
            "run_attempt": 2,
            "head_sha": head,
            "event": "workflow_dispatch",
            "path": path,
            "_ci_repo": repo,
        }
        rule = {
            "repo": repo,
            "branch": "main",
            "workflow": path,
            "approved_caller_shas": [head],
            "approved_template_shas": [tpl],
        }
        detail = {
            **run,
            "repository": {"full_name": repo},
            "head_branch": "main",
            "status": "in_progress",
            "referenced_workflows": [{"path": TEMPLATE, "sha": tpl}],
        }
        job = {
            "run_id": 3,
            "head_sha": head,
            "name": "caller / " + JOB_NAME,
            "id": 10,
            "status": "in_progress",
        }

        def check(d, j):
            def request(method, path):
                self.assertEqual(method, "GET")
                self.assertTrue(path.startswith("/actions/runs/3/attempts/2"))
                return {"jobs": j, "total_count": len(j)} if "/jobs?" in path else d

            return github_call(SimpleNamespace(repo=repo, request=request), run, rule)

        self.assertEqual(check(detail, [job])["job_id"], 10)
        for d, j in [
            (dict(detail, referenced_workflows=[]), [job]),
            (dict(detail, run_attempt=1), [job]),
            (detail, [job, job]),
            (detail, [dict(job, status="completed")]),
            (detail, [dict(job, head_sha="c" * 40)]),
        ]:
            with self.assertRaises(ValueError):
                check(d, j)
