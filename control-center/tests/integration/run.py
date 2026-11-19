#!/usr/bin/env python3
"""Disposable dual-Worker verification against Wrangler D1 with a TLS wire capture."""

import hashlib
import hmac
import http.client
import json
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "control-center/worker"
sys.path.insert(0, str(ROOT))

from ci_core.contract import normalize  # noqa: E402
from ci_core.job_state import JobState  # noqa: E402
from ci_core.reporting import report  # noqa: E402
from ci_core.router import task_key  # noqa: E402
from tests.scenarios import DEFAULTS, definition, write_workspace  # noqa: E402

PEPPER = "fixture-pepper"
SUPER = "fixture-super-key"
TOKENS = {"blue": "fixture-blue-worker-key", "yellow": "fixture-yellow-worker-key"}
REPO = "cann-framework/vllm-ascend"
ZONES = ("blue", "yellow")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def workspace(path):
    pool_roots = {}
    for zone in ZONES:
        child = path / zone
        write_workspace(child)
        pool = json.loads((child / "config/pools.json").read_text())["pool_blue"]
        pool["type"] = "ascend-a3"
        if zone == "yellow":
            pool["owner"] = zone
            hosts = {}
            for node in pool["nodes"]:
                old = node["host_id"]
                new = old.replace("192.0.2.", "198.51.100.")
                hosts[old] = new
                node.update(host_id=new, ssh=new, node_ip=new.split(":")[0])
            for bundle in pool["verified_multinode_allocations"]:
                for group in bundle:
                    group["host_id"] = hosts[group["host_id"]]
        pool_roots["pool_" + zone] = pool
    for zone in ZONES:
        (path / zone / "config/pools.json").write_text(encode(pool_roots))
        state = path / zone / "state"
        state.mkdir()
    return pool_roots


def seed_case(state, zone, index, *, phase="QUEUED"):
    run = {
        "_ci_repo": REPO,
        "_ci_provider": "gitcode" if index % 2 else "github",
        "id": f"{1 if zone == 'blue' else 2}0{index}",
        "run_attempt": 1,
        "created_at": f"2026-01-01T00:00:{index:02d}Z",
        "head_sha": "a" * 40,
        "path": ".github/workflows/lite-ci-fixture.yml",
        "event": "workflow_dispatch",
        "actor": {"login": "fixture"},
    }
    defaults = DEFAULTS | {"worker": zone, "target_pool": "pool_" + zone}
    item = normalize(
        definition("integration", kind="npu_smoke", worker=zone), defaults, 0
    )
    key = task_key(run, item)
    state.admit(
        [
            (
                key,
                {
                    "item": item,
                    "run": run,
                    "phase": "QUEUED",
                    "digest": "b" * 64,
                    "defaults": defaults,
                    "queued_at": time.time(),
                },
            )
        ]
    )
    if phase == "REPORTED":
        state.update(
            key, phase=phase, report=report(run, item, "b" * 64, "PASS", 0, [])
        )
    return key


class Platform:
    """Keep the scheduling loop live using a disposable provider fixture."""

    def for_run(self, run):
        return self

    def running_workflows(self):
        return []

    def workflow_status(self, run):
        return {"status": "in_progress", "run_attempt": 1}

    def commit_comments(self, *_args, **_kwargs):
        return []

    def send_result(self, *_args, **_kwargs):
        return {"id": 1}


def worker(zone, root, proxy):
    import unified_router
    from ci_core.control_client import ControlClient
    from ci_core.egress_policy import Egress
    from ci_core.idle_probe import group_id

    state = root / "state"

    def observe(owner, pools, _records):
        now = int(time.time())
        return (
            {
                group_id(name, node["host_id"], group["devices"]): (
                    "idle" if 0 in group["devices"] else "busy",
                    now,
                )
                for name, pool in pools.items()
                if pool["owner"] == owner
                for node in pool["nodes"]
                for group in node["allocation_groups"]
            },
            now,
        )

    def observe_health(
        owner, pools, records, *, execute=None, previous=None, probe_round=1
    ):
        states, sampled_at = observe(owner, pools, records)
        health = {
            json.dumps([pool, host, sorted(devices)], separators=(",", ":")): {
                "v": 0,
                "mc": 0,
                "lo": sampled_at,
                "pr": probe_round,
            }
            for pool, host, devices in states
        }
        return states, sampled_at, health

    cfg = {
        "state_dir": str(state),
        "address": "192.0.2.254",
        "case_environments": {},
        "daily_upload_bytes": 50_000_000,
        "control_center": {"url": proxy, "ci_reserve_bytes": 1000},
    }
    real_control = ControlClient.__init__
    real_egress = Egress.__init__

    class FixtureWait(threading.Event):
        def wait(self, timeout=None):
            if timeout is not None and timeout >= 30:
                timeout /= 10
            return super().wait(timeout)

    def init_control(self, *args, **kwargs):
        real_control(self, *args, **kwargs, clock=lambda: 10 * time.monotonic())
        self.stop = FixtureWait()

    def init_egress(self, *args):
        origin, epoch = time.monotonic(), time.time()
        real_egress(self, *args, clock=lambda: epoch + 10 * (time.monotonic() - origin))

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "LITE_ACTIONS_AGENT_LOCK": str(state / "agent.lock"),
                    "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(state / "resources"),
                    "LITE_ACTIONS_RESULT_AUTHOR": "fixture",
                    "LITE_ACTIONS_GITHUB_ENABLED": "true",
                    "LITE_ACTIONS_GITCODE_REPOS": "",
                    "LITE_ACTIONS_CONTROL_TOKEN": TOKENS[zone],
                },
            )
        )
        stack.enter_context(
            patch("sys.argv", ["unified_router.py", "--worker-id", zone])
        )
        stack.enter_context(patch.object(unified_router, "ROOT", root))
        stack.enter_context(
            patch.object(unified_router, "deployment", return_value=(zone, cfg))
        )
        stack.enter_context(
            patch.object(unified_router, "configured_repos", return_value=[REPO])
        )
        stack.enter_context(
            patch("ci_core.router.configured_repos", return_value=[REPO])
        )
        stack.enter_context(
            patch.object(unified_router, "authorization", return_value="fixture")
        )
        stack.enter_context(
            patch.object(unified_router, "GitHubClient", return_value=Platform())
        )
        stack.enter_context(patch("ci_core.router.Router.claim", return_value=None))
        stack.enter_context(
            patch("ci_core.idle_probe.observe_health", side_effect=observe_health)
        )
        stack.enter_context(patch.object(ControlClient, "__init__", init_control))
        stack.enter_context(patch.object(Egress, "__init__", init_egress))
        unified_router.main()


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cert, private_key, upstream):
        self.upstream = upstream
        self.paused = False
        self.frames = []
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, private_key)
        self.tls = tls
        super().__init__(("127.0.0.1", 0), ProxyRequest)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(7)
        try:
            return self.tls.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close()
            raise


class ProxyRequest(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(2048)
        if not line:
            return
        method, path, protocol = line.decode("ascii").strip().split()
        if protocol != "HTTP/1.1":
            return
        length = len(line)
        headers = {}
        while True:
            row = self.rfile.readline(2048)
            length += len(row)
            if row in {b"", b"\r\n"}:
                break
            name, value = row.decode("ascii").split(":", 1)
            headers[name.lower()] = value.strip()
        body = self.rfile.read(int(headers.get("content-length", "0")))
        length += len(body)
        token = headers.get("authorization", "").removeprefix("Bearer ")
        zone = next(
            (zone for zone, value in TOKENS.items() if value == token), "unknown"
        )
        host = headers.get("host")
        platform = (host == "api.github.com" and path.startswith("/repos/")) or (
            host == "api.gitcode.com" and path.startswith("/api/v5/repos/")
        )
        if platform:
            zone = "yellow-platform"
        elif not path.startswith("/api/v1/worker/"):
            return
        self.server.frames.append((zone, method, path, length))
        if length >= 2000 or self.server.paused:
            status = 413 if length >= 2000 else 503
            data = b'{"ok":false}'
        elif platform:
            status, data = 201, b'{"id":1}'
        else:
            link = http.client.HTTPConnection(
                "127.0.0.1", self.server.upstream, timeout=5
            )
            try:
                link.request(
                    method,
                    path,
                    body or None,
                    {
                        "Authorization": headers.get("authorization", ""),
                        "Content-Type": "application/json",
                    },
                )
                response = link.getresponse()
                status, data = response.status, response.read()
            finally:
                link.close()
        self.wfile.write(
            f"HTTP/1.1 {status} fixture\r\nContent-Length: {len(data)}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n".encode()
            + data
        )


def platform_wire(root, proxy, cert):
    from ci_core.egress_policy import Egress
    from ci_core.gitcode import GitCodeClient
    from ci_core.github import GitHubClient
    from ci_core.reporting import body_for, progress

    real_connect = socket.create_connection

    def connect(address, *args, **kwargs):
        if address == ("127.0.0.1", proxy.upstream):
            return real_connect(address, *args, **kwargs)
        if address[0] not in {"api.github.com", "api.gitcode.com"}:
            raise AssertionError("Fixture attempted an unexpected connection")
        return real_connect(proxy.server_address, *args, **kwargs)

    clock = time.time()

    def now():
        nonlocal clock
        clock += 6
        return clock

    policy = Egress(
        "yellow", {"state_dir": str(root), "daily_upload_bytes": 1_000_000}, clock=now
    )
    with (
        patch("socket.create_connection", side_effect=connect),
        patch("urllib.request.getproxies", return_value={}),
        patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}),
    ):
        clients = [
            GitHubClient("g" * 64, REPO, policy=policy),
            GitCodeClient("t" * 64, "depeng1994/torchtitan-npu", policy=policy),
        ]
        for client in clients:
            run = {
                "id": "r" * 128,
                "_ci_dispatch_id": "d" * 128,
                "_ci_repo": client.repo,
                "head_sha": "a" * 40,
                "path": ".github/workflows/fixture.yml",
            }
            item = normalize(
                definition("a" * 80, kind="npu_smoke", worker="yellow"),
                DEFAULTS | {"worker": "yellow", "target_pool": "pool_yellow"},
                0,
            )
            digest = "b" * 64
            for retry in [False, True]:
                progress(client, run, item, digest, "QUEUED", retry=retry)
                result = report(
                    run, item, digest, "PASS", 0, ["synthetic line " + "x" * 165] * 10
                )
                client.send_result(
                    run["head_sha"],
                    body_for(run, item, result, local_worker="yellow"),
                    category="terminal_retry" if retry else "terminal",
                )


def until(fn, timeout=35):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        result = fn()
        if result:
            return result
        time.sleep(0.4)
    raise AssertionError("Condition did not converge: " + str(fn))


def fini(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def integration(root):
    pools = workspace(root)
    cert, key = root / "cert.pem", root / "private.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,DNS:api.github.com,DNS:api.gitcode.com",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        capture_output=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    python = sys.executable
    wrangler = [str(BACKEND / "node_modules/.bin/wrangler")]
    storage = ["--local", "--persist-to", str(root / "d1")]
    config = encode(pools)
    sha = hashlib.sha256(config.encode()).hexdigest()
    migration = root / "seed.sql"
    migration.write_text(
        f"INSERT INTO pools_configs(revision,sha256,config_json,created_by,created_at) VALUES(1,'{sha}','{config}','fixture',1);"
    )
    subprocess.run(
        [*wrangler, "d1", "migrations", "apply", "DB", *storage],
        cwd=BACKEND,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [*wrangler, "d1", "execute", "DB", *storage, "--file", str(migration), "--yes"],
        cwd=BACKEND,
        check=True,
        capture_output=True,
    )
    key_args = [
        item
        for zone, token in TOKENS.items()
        for item in [
            "--var",
            f"WORKER_{zone.upper()}_KEY_HASH:"
            + hmac.new(PEPPER.encode(), token.encode(), hashlib.sha256).hexdigest(),
        ]
    ]
    environment = os.environ | {
        "LITE_ACTIONS_CONTROL_CA_FILE": str(cert),
        "NODE_ENV": "test",
    }
    states = {zone: JobState(root / zone) for zone in ZONES}
    cases = {
        zone: [seed_case(states[zone], zone, i) for i in range(4)] for zone in ZONES
    }
    seed_case(states["blue"], "blue", 9, phase="REPORTED")

    def http(path, *, payload=None, session=None):
        headers = {"Content-Type": "application/json"}
        if session:
            headers["Cookie"] = session
        req = urllib.request.Request(
            base + "/api/v1" + path,
            None if payload is None else encode(payload).encode(),
            headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as response:
            return response.code, json.load(response), response.headers
        except OSError:
            return 0, {}, {}

    def tasks():
        status, body, _ = http("/public/models/vllm-ascend/runs")
        return (
            [task for run in body.get("active", []) for task in run["tasks"]]
            if status == 200
            else []
        )

    def spawn_worker(stack, proxy, zone):
        log = stack.enter_context((root / (zone + ".log")).open("ab"))
        proc = subprocess.Popen(
            [
                python,
                __file__,
                "--worker",
                zone,
                str(root / zone),
                f"https://localhost:{proxy.server_address[1]}",
            ],
            env=environment,
            stdout=log,
            stderr=log,
            cwd=ROOT,
        )
        stack.callback(fini, proc)
        return proc

    with ExitStack() as stack:
        output = stack.enter_context((root / "backend.log").open("wb"))
        proc = subprocess.Popen(
            [
                *wrangler,
                "dev",
                *storage,
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--var",
                "KEY_PEPPER:" + PEPPER,
                "--var",
                "SUPER_SECRET:" + SUPER,
                *key_args,
            ],
            cwd=BACKEND,
            env=environment,
            stdout=output,
            stderr=output,
        )
        stack.callback(fini, proc)
        until(lambda: http("/public/overview")[0] == 200)
        proxy = stack.enter_context(Proxy(cert, key, port))
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        stack.callback(proxy.shutdown)
        workers = {zone: spawn_worker(stack, proxy, zone) for zone in ZONES}
        status, _, headers = http(
            "/auth/login", payload={"username": "super", "secret": SUPER}
        )
        assert status == 200 and "Secure" in headers["Set-Cookie"]
        cookie = headers["Set-Cookie"].split(";")[0]
        active = until(
            lambda: (
                rows
                if len(rows := tasks()) == 8
                and all(row["queue_pos"] is not None for row in rows)
                else None
            )
        )
        status, pub, cache = http("/public/timeline")
        assert status == 200 and cache["Cache-Control"] == "public, max-age=10"
        assert "192.0.2." not in encode(pub) and "198.51.100." not in encode(pub)
        admin = http("/admin/timeline", session=cookie)
        assert admin[0] == 200 and "192.0.2." in encode(admin[1])
        assert http("/admin/cluster")[0] == 401
        assert any(
            g["state"] == "CONFIRMED_IDLE"
            for zone in pub["workers"]
            for pool in zone["pools"]
            for node in pool["nodes"]
            for g in node["device_groups"]
        ), pub
        print(
            "INT-01/03  public/admin cache isolation, masking, dual Worker census",
            flush=True,
        )
        by_zone = [task for task in active if task["worker_id"] == "blue"]
        target = max(by_zone, key=lambda task: task["queue_pos"])
        for _ in range(3):
            status, issued, _ = http(
                f"/admin/tasks/{target['task_uid']}/move-up", payload={}, session=cookie
            )
            assert status == 202, issued

            def command():
                status, data, _ = http(
                    "/admin/commands/" + issued["cmd_id"], session=cookie
                )
                return data if status == 200 and data["status"] == "APPLIED" else None

            until(command)
        assert (
            states["blue"].read()[cases["blue"][-1]]["queue_rank"]
            < states["blue"].read()[cases["blue"][0]]["queue_rank"]
        )
        print(
            "INT-07  three standalone move-up commands applied in sequence", flush=True
        )
        node = pools["pool_blue"]["nodes"][0]
        op = {
            "base_revision": 1,
            "operation": {
                "op": "PAUSE",
                "scope": "device_group",
                "pool": "pool_blue",
                "host_id": node["host_id"],
                "devices": [0, 1],
            },
        }
        status, preview, _ = http("/admin/cluster/preview", payload=op, session=cookie)
        assert status == 200, preview
        status, published, _ = http(
            "/admin/cluster/publish",
            payload={**op, "preview_hash": preview["preview_hash"]},
            session=cookie,
        )
        assert status == 200 and published["revision"] == 2, published
        until(
            lambda: (
                http("/admin/cluster", session=cookie)[1]["workers"][0]["status"]
                == "APPLIED"
            )
        )
        applied = json.loads((root / "blue/state/pools-active.json").read_text())
        assert (
            applied["revision"] == 2
            and applied["pools"]["pool_blue"]["nodes"][0]["allocation_groups"][0][
                "paused"
            ]
        )
        fini(workers["blue"])
        spawn_worker(stack, proxy, "blue")
        until(
            lambda: (
                http("/admin/cluster", session=cookie)[1]["workers"][0]["applied_rev"]
                == 2
            )
        )
        print(
            "INT-08  revision published and acknowledged through real Worker process",
            flush=True,
        )

        proxy.paused = True
        ident = cases["blue"][0]
        row = states["blue"].read()[ident]
        states["blue"].update(
            ident,
            phase="RUNNING",
            allocation=[{"host_id": node["host_id"], "devices": [0, 1]}],
        )
        states["blue"].update(
            ident,
            phase="PENDING_REPORT",
            report=report(row["run"], row["item"], row["digest"], "PASS", 0, []),
        )
        for index in range(20, 30):
            seed_case(states["blue"], "blue", index)
        outbox = root / "blue/state/control-outbox.json"
        until(
            lambda: (
                sum(e["t"] == "TU" for e in json.loads(outbox.read_text())["events"])
                >= 10
            )
        )
        until(
            lambda: any(
                e["t"] == "TF" for e in json.loads(outbox.read_text())["events"]
            )
        )

        def offline():
            status, data, _ = http("/admin/overview", session=cookie)
            return status == 200 and all(zone["offline"] for zone in data["workers"])

        until(offline, timeout=110)
        proxy.paused = False
        history = until(
            lambda: http("/admin/models/vllm-ascend/runs", session=cookie)[1].get(
                "history"
            ),
            timeout=65,
        )
        finished = [task for run in history for task in run["tasks"]]
        assert len(finished) == 1 and finished[0]["status"] == "PASS", finished
        until(lambda: len(tasks()) == 17, timeout=55)
        required = {
            "/api/v1/worker/" + route
            for route in ("heartbeat", "events", "snapshots", "command-receipts")
        }
        assert required <= {path for _, _, path, _ in proxy.frames}
        assert {zone for zone, _, _, _ in proxy.frames} == set(ZONES)
        platform_wire(root, proxy, cert)
        platform_posts = [
            frame for frame in proxy.frames if frame[0] == "yellow-platform"
        ]
        assert len(platform_posts) == 8
        peak = max(length for *_, length in proxy.frames)
        assert peak < 2000, peak
        print(
            f"INT-02/05  90s offline and 20+ events recovered, {len(proxy.frames)} TLS requests including {len(platform_posts)} Yellow platform writes, max {peak} bytes",
            flush=True,
        )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), sys.argv[4])
    elif len(sys.argv) == 2:
        destination = Path(sys.argv[1]).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        integration(destination)
    else:
        with tempfile.TemporaryDirectory(prefix="lite-control-integration-") as scratch:
            integration(Path(scratch))
