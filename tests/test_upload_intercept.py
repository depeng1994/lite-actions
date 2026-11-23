"""Bound the HTTP/1.1 wire size of real outbound upload code paths."""

import ast
import hashlib
import http.client
import json
import threading
import unittest
import urllib.request
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ci_core.control_client import ControlClient, task_descriptor
from ci_core.egress_policy import Egress, estimate_http1_request_bytes
from ci_core.gitcode import GitCodeClient
from ci_core.github import GitHubClient, ReleaseLogs
from ci_core.logs import BLUE_BYTES
from ci_core.reporting import body_for, progress, report
from tests import test_control_client as control_tests
from tests.test_control_client import record
from tests.test_egress_client_headers import _WireCapture

ROOT = Path(__file__).resolve().parents[1]
WRITES = {"POST", "PUT", "PATCH"}
UPLOAD_CALLSITE_REGISTRY = {
    "ci_core/control_client.py:ControlClient._request:Request",
    "ci_core/github.py:GitHubClient.request:Request",
    "ci_core/github.py:GitHubClient.send_result:request",
    "ci_core/github.py:ReleaseLogs.api_call:Request",
    "ci_core/github.py:ReleaseLogs.deliver:api_call",
    "ci_core/gitcode.py:GitCodeClient.request:Request",
    "ci_core/gitcode.py:GitCodeClient.send_result:request",
    "ci_core/reporting.py:post:send_result",
    "ci_core/reporting.py:progress:request",
    "ci_core/reporting.py:progress:send_result",
}


class WriteScanner(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.scope = []
        self.hits = []

    def scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = scoped
    visit_FunctionDef = scoped
    visit_AsyncFunctionDef = scoped

    def visit_Call(self, node):
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        first = node.args[0] if node.args else keywords.get("method")

        def literal(value):
            return getattr(value, "value", None)

        data = keywords.get("data")
        if name in {"Request", "urlopen"} and len(node.args) >= 2:
            data = node.args[1]
        request = name in {"request", "api_call"} and (
            literal(first) in WRITES
            or (data is not None and not isinstance(first, ast.Constant))
        )
        upload = name in {"Request", "urlopen"} and (
            (
                data is not None
                and not (isinstance(data, ast.Constant) and data.value is None)
            )
            or literal(keywords.get("method")) in WRITES
        )
        wrapper = name in {"send_result", "_post"} or (
            name in {"post", "put", "patch"}
            and isinstance(node.func, ast.Attribute)
            and getattr(node.func.value, "id", "") in {"requests", "httpx", "session"}
        )
        if request or upload or wrapper:
            self.hits.append(
                (f"{self.path}:{'.'.join(self.scope)}:{name}", node.lineno)
            )
        self.generic_visit(node)


def assert_coverage(hits):
    found = Counter(key for key, _ in hits)
    expected = Counter(UPLOAD_CALLSITE_REGISTRY)
    if found != expected:
        raise AssertionError(
            f"Unregistered uploads: {[(key, line) for key, line in hits if found[key] > expected[key]]}; "
            f"missing: {list((expected - found).elements())}"
        )


class UploadInterceptor:
    def __init__(self, responder):
        self.responder = responder
        self.records = []
        self.tag = ""
        self.bound = 1999

    def build(self, *handlers):
        return self

    def open(self, request, timeout=10):
        handler = urllib.request.HTTPHandler()
        handler.parent = type(
            "Parent", (), {"addheaders": [("User-Agent", "Python-urllib/3.11")]}
        )()
        expected = estimate_http1_request_bytes(request)
        request = handler.do_request_(request)
        target = urlsplit(request.full_url)
        conn = _WireCapture(target.netloc)
        try:
            headers = dict(request.header_items())
            headers["Connection"] = "close"
            conn.request(request.get_method(), request.selector, request.data, headers)
            wire = bytes(conn.wire)
        finally:
            conn.close()
        if request.data is not None or request.get_method() in WRITES:
            assert len(wire) <= self.bound, (
                f"{self.tag}: {len(wire)} bytes exceeds {self.bound}"
            )
            assert expected >= len(wire), (
                f"{self.tag}: estimate {expected} smaller than {len(wire)}"
            )
            self.records.append(
                (self.tag, target.path, len(wire), len(request.data or b""))
            )
        return _WireResponse(self.responder(request))


class _WireResponse:
    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return self.data[:size]


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = control_tests.ControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def intercept(self, responder):
        wire = UploadInterceptor(responder)
        patches = [
            patch("urllib.request.build_opener", side_effect=wire.build),
            patch("urllib.request.urlopen", side_effect=wire.open),
            patch.object(
                http.client.HTTPConnection,
                "connect",
                side_effect=AssertionError("unexpected socket"),
            ),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        return wire

    def test_static_registry_and_negative_checks(self):
        hits = []
        for path in [*ROOT.glob("ci_core/**/*.py"), ROOT / "unified_router.py"]:
            scan = WriteScanner(path.relative_to(ROOT))
            scan.visit(ast.parse(path.read_text()))
            hits.extend(scan.hits)
        assert_coverage(hits)

        rogue = WriteScanner("ci_core/untracked.py")
        rogue.visit(
            ast.parse(
                'def send():\n    urllib.request.Request(url, data=b"{}", method="POST")'
            )
        )
        with self.assertRaisesRegex(
            AssertionError, r"ci_core/untracked.py:send:Request.*2"
        ):
            assert_coverage(hits + rogue.hits)
        wire = self.intercept(lambda _: {})
        with self.assertRaisesRegex(AssertionError, "exceeds"):
            wire.open(
                urllib.request.Request("https://example.test/new", data=b"x" * 2000)
            )

    def test_control_upload_routes_max_fields_and_paging_both_zones(self):
        routes = {"heartbeat", "command-receipts", "events", "snapshots"}

        def respond(request):
            payload = json.loads(request.data)
            return {"complete": payload.get("p") == payload.get("n")}

        wire = self.intercept(respond)
        for zone in ("blue", "yellow"):
            cfg = self.fixture.cfg | {
                "control_center": {
                    "url": "https://" + ("a" * 63) + "." + ("b" * 12) + ".net"
                },
            }
            clock = [self.fixture.now]
            client = ControlClient(
                zone,
                cfg,
                "k" * 64,
                Egress(zone, cfg, clock=lambda: clock[0]),
                threading.Event(),
                lambda: {},
            )
            base = {"w": zone, "i": "1f2e3d4c"}
            long_record = record()
            long_record["item"].update(
                case_id="case_000_" + "z" * 80, label="🐱" * 40, target_pool="p" * 64
            )
            long_record["phase_timestamps"] = {
                "QUEUED": 1795056000,
                "RESERVED": 1795056010,
                "STAGING": 1795056020,
                "RUNNING": 1795056030,
                "CLEANING": 1795056040,
            }
            long_record["run"].update(
                {
                    "_ci_repo": "a" * 39 + "/" + "r" * 88,
                    "id": "n" * 128,
                    "run_attempt": 99,
                    "_ci_dispatch_id": "d" * 128,
                    "_ci_pr_number": 99999,
                    "path": "w" * 180,
                    "actor": {"login": "a" * 40},
                    "_ci_pr_author": "b" * 40,
                    "event": "workflow_dispatch",
                    "created_at": "2026-11-20T00:00:00Z",
                    "name": "W" * 64,
                    "run_number": 2147483647,
                    "display_title": "D" * 128,
                }
            )
            item = task_descriptor("key", long_record)
            counts = Counter()

            def send(route, payload):
                clock[0] += 6
                wire.tag = f"{zone}:{route}"
                counts[route] += 1
                return real_send(route, payload)

            real_send = client._send
            with patch.object(client, "_send", side_effect=send):
                client._send(
                    "heartbeat",
                    {
                        **base,
                        "ts": 1795056000,
                        "rev": 12,
                        "sha": "8" * 8,
                        "ok": 1,
                        "act": 30,
                        "q": 50,
                        "idle_ts": 1795055999,
                        "probe_ts": 1795055999,
                        "eu": 42000,
                    },
                )
                client._send(
                    "command-receipts",
                    {"cmd_id": "cmd_" + "x" * 36, "status": "FAILED", "err": "x" * 200},
                )
                client._send(
                    "command-receipts",
                    {
                        "cmd_id": "cmd_" + "p" * 36,
                        "status": "PARTIAL_APPLIED",
                        "rev": 99,
                        "err": "198.51.100.11:22: " + "e" * 182,
                        "ts": 1795056004,
                    },
                )
                client._send(
                    "command-receipts",
                    {
                        "cmd_id": "cmd_" + "y" * 36,
                        "status": "APPLIED",
                        "pools": [
                            {"p": "p" * 64, "k": "npu_smoke", "st": "APPLIED", "c": 4},
                            {"p": "q" * 64, "k": "train", "st": "NOOP", "c": 0},
                        ],
                        "ts": 1795056005,
                    },
                )
                events = [
                    {"t": "TU", **item},
                    {
                        "s": 1,
                        "t": "IP",
                        "pool": "p" * 64,
                        "h": "255.255.255.255:65535",
                        "d": list(range(16)),
                        "st": "probe_error",
                        "ts": 1795056000,
                        "pr": 999999,
                        "v": 2,
                        "mc": 5,
                        "lo": 1795056000,
                        "qu": 1,
                        "qid": "t_" + "a" * 32,
                    },
                ] + [
                    {
                        "s": i,
                        "t": ["TP", "TL", "TF", "IP", "QP"][i % 5],
                        "tid": f"{i:012x}",
                        "a": [{"h": "198.51.100.42:153", "d": list(range(16))}],
                    }
                    for i in range(2, 52)
                ]
                for batch in client.packets(
                    "events", events, lambda rows: {**base, "ev": rows}
                ):
                    client._send("events", {**base, "ev": batch})
                client.snapshot(
                    base,
                    {
                        "task": [item] * 30,
                        "device": [{"h": f"198.51.100.{i}:22"} for i in range(8)],
                        "queue": [
                            {"tid": f"{i:012x}", "ix": list(range(16))}
                            for i in range(50)
                        ],
                    },
                    99,
                )
            self.assertEqual(set(counts), routes)
            self.assertGreater(counts["events"], 1)
            self.assertGreater(counts["snapshots"], 1)
        self.assertEqual(
            {tag for tag, _, _, _ in wire.records},
            {f"{zone}:{route}" for zone in ("blue", "yellow") for route in routes},
        )

    def test_platform_ack_retry_terminal_and_blue_diagnostics(self):
        wire = self.intercept(lambda request: {"id": 42})
        sha = "a" * 40
        cfg = self.fixture.cfg
        clock = [100]
        for platform in (GitHubClient, GitCodeClient):
            for zone in ("yellow", "blue"):
                client = platform(
                    "t" * 64,
                    "depeng1994/lite-actions"
                    if platform is GitHubClient
                    else "depeng1994/torchtitan-npu",
                    policy=Egress(zone, cfg, clock=lambda: clock[0]),
                )
                item = record()["item"] | {
                    "case_id": "case_000_" + "r" * 80,
                    "test_id": "r" * 80,
                    "worker": zone,
                    "docker": "registry.example/model:latest",
                }
                run = {
                    "id": "n" * 128,
                    "_ci_dispatch_id": "d" * 128,
                    "head_sha": sha,
                    "_ci_repo": client.repo,
                    "path": ".github/workflows/smoke.yml",
                }
                result = report(
                    run,
                    item,
                    "b" * 64,
                    "PASS",
                    0,
                    ["ordinary diagnostics " + "x" * 120] * 10,
                )
                terminal = body_for(run, item, result, local_worker=zone)
                wire.bound = 1999 if zone == "yellow" else 7000
                for category in ("ack", "ack_retry", "terminal", "terminal_retry"):
                    clock[0] += 6
                    wire.tag = f"{zone}:{platform.__name__}:{category}"
                    if category.startswith("ack"):
                        progress(
                            client,
                            run,
                            item,
                            "b" * 64,
                            "QUEUED",
                            retry=category.endswith("retry"),
                        )
                    else:
                        client.send_result(sha, terminal, category=category)
                if zone == "blue":
                    wire.tag = f"{zone}:{platform.__name__}:progress"
                    progress(client, run, item, "b" * 64, "RUNNING", comment_id=42)
        self.assertEqual(len(wire.records), 18)
        counters = json.loads(
            (Path(cfg["state_dir"]) / "http-egress.json").read_text()
        )["classified"]
        for provider in ("github", "gitcode"):
            for category in ("ack", "ack_retry", "terminal", "terminal_retry"):
                row = counters[f"{provider}:{category}"]
                self.assertEqual(row["admitted"], 1)
                self.assertEqual(row["sent_violations"], 0)
                self.assertLess(row["max_request_bytes"], 2000)

    def test_blue_release_asset_is_the_only_large_upload_whitelist(self):
        def respond(request):
            url = urlsplit(request.full_url)
            if request.data is not None:
                name = parse_qs(url.query)["name"][0]
                return {
                    "id": 13,
                    "name": name,
                    "size": len(request.data),
                    "state": "uploaded",
                    "digest": "sha256:" + hashlib.sha256(request.data).hexdigest(),
                    "browser_download_url": f"https://github.com/sample/logs/releases/download/ci/{name}",
                    "uploader": {"login": "publisher"},
                }
            return (
                {"id": 1, "draft": False, "tag_name": "ci"}
                if "/releases/" in url.path
                else {"full_name": "sample/logs", "private": False}
            )

        wire = self.intercept(respond)
        wire.tag = "blue:release_asset"
        wire.bound = BLUE_BYTES + 1024
        cfg = self.fixture.cfg | {
            "public_release": {
                "repository": "sample/logs",
                "release_id": 1,
                "tag": "ci",
                "publisher_login": "publisher",
                "public_diagnostics_approved": True,
            }
        }
        release = ReleaseLogs("blue", cfg, token="r" * 64)
        with self.assertRaisesRegex(ValueError, "BLUE_SIZE"):
            release.deliver("too-large", b"l" * (BLUE_BYTES + 1), False)
        release.deliver("case", b"l" * BLUE_BYTES, False)
        self.assertEqual(
            [(tag, body_bytes) for tag, _, _, body_bytes in wire.records],
            [("blue:release_asset", BLUE_BYTES)],
        )
