"""Exercise real GitHub/GitCode request construction without network I/O."""

import http.client
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ci_core.egress_policy import Egress
from ci_core.gitcode import GitCodeClient
from ci_core.github import GitHubClient
from ci_core.reporting import progress_body

SHA = "a" * 40
REPO = "depeng1994/lite-actions"


class _Response:
    headers = {"Content-Length": "8"}

    def __init__(self, request):
        self.request = request

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        if self.request.get_method() == "GET":
            if "/actions/runs/" in self.request.full_url:
                return b'{"status":"completed","attempt":1}'
            return b"[]"
        return b'{"id":1}'


class _Opener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return _Response(request)


class _WireCapture(http.client.HTTPConnection):
    def __init__(self, host):
        super().__init__(host)
        self.wire = bytearray()

    def connect(self):
        # Deliberately no socket: only exercise http.client's request serializer.
        return None

    def send(self, data):
        self.wire.extend(data if isinstance(data, bytes) else data.encode())


def serialized_request(request):
    # Same urllib handler mutation performed before HTTPConnection.request().
    handler = urllib.request.HTTPHandler()
    handler.parent = type("Parent", (), {"addheaders": []})()
    request = handler.do_request_(request)
    from urllib.parse import urlsplit

    url = urlsplit(request.full_url)
    conn = _WireCapture(url.hostname)
    conn.putrequest(
        request.get_method(), url.path + ("?" + url.query if url.query else "")
    )
    for key, value in request.header_items():
        conn.putheader(key, value)
    conn.endheaders(request.data)
    return bytes(conn.wire)


class RealClientHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Egress(
            "yellow",
            {
                "state_dir": self.tmp.name,
                "daily_upload_bytes": 1000000,
                "artifact_hosts": [],
            },
        )
        self.body = progress_body(
            {"id": 123, "head_sha": SHA, "path": ".github/workflows/smoke.yml"},
            {"case_id": "case_001_worker_os_yellow", "worker": "yellow"},
            "b" * 64,
            "QUEUED",
        )

    def _exercise(self, provider):
        opener = _Opener()
        with patch("urllib.request.build_opener", return_value=opener):
            if provider == "github":
                client = GitHubClient("g" * 1200, REPO, policy=self.policy)
            else:
                client = GitCodeClient(
                    "c" * 1200, "depeng1994/torchtitan-npu", policy=self.policy
                )
            client.send_result(SHA, self.body, category="ack_retry")
        request = opener.request
        self.assertIsNotNone(request)
        headers = {k.lower(): v for k, v in request.header_items()}
        auth_name = "authorization" if provider == "github" else "private-token"
        self.assertEqual(
            len(headers[auth_name]), 1207 if provider == "github" else 1200
        )
        # Invoke urllib's actual request preparation, then serialize with http.client.
        wire = serialized_request(request)
        head, body = wire.split(b"\r\n\r\n", 1)
        self.assertIn(b"HTTP/1.1", head.split(b"\r\n", 1)[0])
        self.assertIn(b"host:", head.lower())
        self.assertIn(b"accept-encoding:", head.lower())
        self.assertIn(b"content-length:", head.lower())
        self.assertIn(b"content-type:", head.lower())
        self.assertIn(f"{len(body)}".encode(), head.lower())
        self.assertEqual(json.loads(body), {"body": self.body})
        state = json.loads((Path(self.tmp.name) / "http-egress.json").read_text())
        row = state["classified"][f"{provider}:ack_retry"]
        # Policy's pre-socket conservative bound must cover the serialized HTTP request.
        self.assertGreaterEqual(row["max_request_bytes"], len(wire))
        self.assertLess(row["max_request_bytes"], 2000)
        self.assertEqual(row["admitted"], 1)
        self.assertEqual(row["sent_violations"], 0)
        self.assertEqual(row["rejected"], 0)
        self.assertEqual(row["overlimit_rejected"], 0)
        encoded_state = json.dumps(state)
        self.assertNotIn(self.body, encoded_state)
        self.assertNotIn("?", encoded_state)
        self.assertNotIn("g" * 1200, encoded_state)
        self.assertNotIn("c" * 1200, encoded_state)
        # Exercise the real client wrappers for all non-body retry/replay classes.
        with patch("urllib.request.build_opener", return_value=opener):
            for category in (
                "ack_retry_lookup",
                "terminal_retry_lookup",
                "old_result_replay_lookup",
                "metadata_get",
            ):
                client.commit_comments(SHA, category=category)
            client.request(
                "GET", "/actions/runs/123", egress_category="cancel_check_get"
            )
        state = json.loads((Path(self.tmp.name) / "http-egress.json").read_text())
        for category in (
            "ack_retry_lookup",
            "terminal_retry_lookup",
            "old_result_replay_lookup",
            "metadata_get",
            "cancel_check_get",
        ):
            row = state["classified"][f"{provider}:{category}"]
            self.assertEqual(row["admitted"], 1)
            self.assertEqual(row["sent_violations"], 0)
            self.assertGreater(row["max_request_bytes"], 0)

    def test_github_real_request_headers_and_auth_length(self):
        self._exercise("github")

    def test_gitcode_real_request_headers_and_auth_length(self):
        with patch.dict(
            os.environ, {"LITE_ACTIONS_GITCODE_REPOS": "depeng1994/torchtitan-npu"}
        ):
            self._exercise("gitcode")


if __name__ == "__main__":
    unittest.main()
