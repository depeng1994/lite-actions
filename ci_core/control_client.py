"""Opt-in, pull-only telemetry and command transport for the control center."""

import copy
import datetime as dt
import hashlib
import json
import logging
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

from ci_core.contract import REPO
from ci_core.egress_policy import (
    Rejected,
    control_origin,
    estimate_http1_request_bytes,
)

TARGET_BYTES = 1880
RESPONSE_LIMIT = 1024 * 1024
OUTBOX_LIMIT = 5000
ACTIVE = {"QUEUED", "RESERVED", "STAGING", "RUNNING", "CLEANING"}
TERMINAL = {"PENDING_REPORT", "REPORTED"}


def _encode(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def write_json(path, value):
    """Publish one durable document. A reader sees either complete generation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(_encode(value))
            file.flush()
            os.fsync(file.fileno())
            temp.replace(path)
            handle = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        finally:
            temp.unlink(missing_ok=True)


def _bounded(value, size):
    return (
        str(value).encode()[:size].decode(errors="ignore")
        if value is not None
        else None
    )


def _login(value):
    value = _bounded(value, 40)
    return value if value and re.fullmatch(r"[A-Za-z0-9_\[\]-]+", value) else None


def _actor(raw):
    return _login(raw.get("login")) if isinstance(raw, dict) else None


def _submitted_at(run, fallback):
    """Return the platform creation time without making another API request."""
    value = run.get("created_at")
    if value is None:
        return int(fallback or 0)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return int(fallback or 0)


def _allocation(record):
    return [
        {"h": node["host_id"], "d": sorted(node["devices"])}
        for node in (record.get("allocation") or [])
    ]


def task_descriptor(key, record):
    run, item = record["run"], record["item"]
    result = {
        "tid": hashlib.sha256(key.encode()).hexdigest()[:12],
        "p": run.get("_ci_provider", "github"),
        "r": run.get("_ci_repo", REPO),
        "run": str(run["id"]),
        "att": int(run.get("run_attempt", 1)),
        "did": run.get("_ci_dispatch_id"),
        "cid": item["case_id"],
        "lbl": _bounded(item.get("label") or item["case_id"], 115),
        "k": item["kind"],
        "pool": item.get("target_pool") or "",
        "req": [item["resources"]["nodes"], item["resources"]["devices_per_node"]],
        "pr": run.get("_ci_pr_number"),
        "pa": _login(run.get("_ci_pr_author")),
        "tr": _actor(run.get("triggering_actor") or run.get("actor")),
        "et": _bounded(run.get("event"), 40),
        "wf": _bounded(run.get("path"), 100),
        "sub": _submitted_at(run, record.get("queued_at")),
        "wn": _bounded(run.get("name"), 64),
        "rn": run.get("run_number") if type(run.get("run_number")) is int and run["run_number"] > 0 else None,
        "dt": _bounded(run.get("display_title") or run.get("run_name"), 128),
        "ph": record["phase"] if record["phase"] in ACTIVE else "QUEUED",
        "ts": int(record.get("queued_at", 0)),
        "tm": record.get("phase_timestamps", {}),
        "reliable": int(bool(record.get("phase_timestamps", {}).get("QUEUED"))),
    }
    return {name: value for name, value in result.items() if value is not None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ControlOutbox:
    def __init__(self, directory):
        self.path = Path(directory) / "control-outbox.json"
        self.lock = threading.Lock()
        self.data = (
            json.loads(self.path.read_bytes())
            if self.path.exists()
            else {
                "instance": uuid4().hex[:8],
                "seq": 0,
                "acked": 0,
                "events": [],
                "commands": {},
                "tracked": {},
                "started_at": time.time(),
            }
        )
        if not self.path.exists():
            write_json(self.path, self.data)

    def change(self, update):
        with self.lock:
            candidate = copy.deepcopy(self.data)
            result = update(candidate)
            if candidate != self.data:
                write_json(self.path, candidate)
                self.data = candidate
            return result

    def view(self):
        with self.lock:
            return copy.deepcopy(self.data)

    @staticmethod
    def append(data, event):
        if len(data["events"]) >= OUTBOX_LIMIT:
            return False
        data["seq"] += 1
        data["events"].append({**event, "s": data["seq"]})
        return True

    def enqueue(self, event):
        return self.change(lambda data: self.append(data, event))

    def acknowledge(self, seq):
        def advance(data):
            data["acked"] = seq
            data["events"] = [event for event in data["events"] if event["s"] > seq]

        self.change(advance)


class ControlClient:
    def __init__(self, worker, cfg, token, policy, wake, view, *, clock=time.monotonic):
        if not re.fullmatch(r"[!-~]{16,64}", token or ""):
            raise ValueError("INVALID_CONTROL_MACHINE_TOKEN")
        self.worker = worker
        self.origin = control_origin(cfg["control_center"]["url"])
        self.outbox = ControlOutbox(cfg["state_dir"])
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            # Cloudflare's edge rejects urllib's default Python-urllib
            # fingerprint. Keep the client identity explicit for the
            # production worker transport.
            "User-Agent": "lite-actions-control-client/1.0",
        }
        self.policy = policy
        self.wake = wake
        self.view = view
        self.clock = clock
        self.stop = threading.Event()
        self.next_heartbeat = 0
        self.need_snapshot = True
        self.rebase_before = None
        self.thread = threading.Thread(
            target=self.run, name="control-center", daemon=True
        )

    def _request(self, route, payload=None):
        return urllib.request.Request(
            self.origin + "/api/v1/worker/" + route,
            data=None if payload is None else _encode(payload),
            headers=self.headers,
            method="GET" if payload is None else "POST",
        )

    def _send(self, route, payload=None):
        request = self.policy.before(
            self._request(route, payload), repo="", provider="control"
        )
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        ca_file = os.environ.get("LITE_ACTIONS_CONTROL_CA_FILE")
        if ca_file:
            handlers.append(
                urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=ca_file)
                )
            )
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=5) as response:
            data = response.read(RESPONSE_LIMIT + 1)
        if len(data) > RESPONSE_LIMIT:
            raise ValueError("CONTROL_RESPONSE_TOO_LARGE")
        return json.loads(data)

    def packets(self, route, items, packet, *, max_items=100, budget=TARGET_BYTES):
        # Pack using conservative headers and the actual UTF-8 serialization.
        current = []
        for item in items:
            trial = [*current, item]
            if (
                len(trial) <= max_items
                and estimate_http1_request_bytes(self._request(route, packet(trial)))
                < budget
            ):
                current = trial
                continue
            if current:
                yield current
            if (
                estimate_http1_request_bytes(self._request(route, packet([item])))
                >= budget
            ):
                raise Rejected("CONTROL_ITEM_TOO_LARGE")
            current = [item]
        if current:
            yield current

    def snapshot(self, base, contents, seq):
        if self.outbox.view().get("snapshot"):
            return self._send_snapshot()
        sid = uuid4().hex[:10]
        pages = []
        for kind, items in contents.items():
            template = {
                **base,
                "sid": sid,
                "p": 999999,
                "n": 999999,
                "seq": seq,
                "kind": kind,
            }
            pages.extend(
                (kind, page)
                for page in self.packets(
                    "snapshots",
                    items,
                    lambda part, template=template: {**template, "items": part},
                )
            )
        pages = pages or [("task", [])]
        packets = [
            {
                **base,
                "sid": sid,
                "p": index,
                "n": len(pages),
                "seq": seq,
                "kind": kind,
                "items": items,
            }
            for index, (kind, items) in enumerate(pages, 1)
        ]
        self.outbox.change(
            lambda data: data.update(snapshot={"packets": packets, "position": 0})
        )
        self._send_snapshot()

    def _send_snapshot(self):
        snapshot = self.outbox.view()["snapshot"]
        for pos in range(snapshot["position"], len(snapshot["packets"])):
            result = self._send("snapshots", snapshot["packets"][pos])
            last = pos + 1 == len(snapshot["packets"])
            if last and not result.get("complete"):
                self.outbox.change(lambda data: data.pop("snapshot"))
                raise ValueError("CONTROL_SNAPSHOT_INCOMPLETE")
            self.outbox.change(
                lambda data: (
                    data.pop("snapshot")
                    if last
                    else data["snapshot"].update(position=pos + 1)
                )
            )

    def _lease_parts(self, base, tid, now, allocation):
        template = {"t": "TL", "tid": tid, "ts": now, "s": 999999999999, "ap": 999999}
        parts = list(
            self.packets(
                "events",
                allocation,
                lambda nodes: {
                    **base,
                    "ev": [{**template, "a": nodes}],
                },
                max_items=16,
            )
        ) or [[]]
        return [
            {"t": "TL", "tid": tid, "ts": now, "ap": part, "a": nodes}
            for part, nodes in enumerate(parts, 1)
        ]

    def _capture(self, records, base=None):
        base = base or {"w": self.worker, "i": self.outbox.view()["instance"]}

        def observe(data):
            for key, record in records.items():
                phase = record.get("phase")
                current = data["tracked"].get(key)
                if current and current.get("done"):
                    continue
                report = record.get("report") or {}
                finished = record.get("phase_timestamps", {}).get("FINISHED", 0)
                if phase not in ACTIVE and not (
                    phase in TERMINAL
                    and report
                    and (current or finished >= int(data["started_at"]))
                ):
                    continue
                task = task_descriptor(key, record)
                tid, now = task["tid"], int(time.time())
                alloc = _allocation(record)
                events = []
                if current is None:
                    events.append({"t": "TU", **task})
                    current = {}
                if phase in TERMINAL:
                    if alloc != current.get("allocation", []):
                        events.extend(self._lease_parts(base, tid, now, alloc))
                    events.append(
                        {
                            "t": "TF",
                            "tid": tid,
                            "ts": finished or now,
                            "tm": task["tm"],
                            "st": report.get("status", "INFRA_ERROR"),
                            "ec": report.get("exit_code"),
                            "cv": int(bool(report.get("cleanup_verified"))),
                            "qu": int(bool(record.get("quarantined"))),
                        }
                    )
                elif phase != current.get("phase") or task["tm"] != current.get(
                    "times"
                ):
                    events.append(
                        {
                            "t": "TP",
                            "tid": tid,
                            "ts": now,
                            "ph": phase,
                            "tm": task["tm"],
                        }
                    )
                if phase in ACTIVE and alloc != current.get("allocation", []):
                    events.extend(self._lease_parts(base, tid, now, alloc))
                if len(data["events"]) + len(events) > OUTBOX_LIMIT:
                    break
                for event in events:
                    self.outbox.append(data, event)
                if phase in TERMINAL:
                    # Keep one compact high-water mark per task while JobState retains it.
                    data["tracked"][key] = {"done": True}
                else:
                    data["tracked"][key] = {
                        "phase": phase,
                        "times": task["tm"],
                        "allocation": alloc,
                    }

        self.outbox.change(observe)

    def _capture_devices(self, devices):
        def observe(data):
            seen = data.setdefault("devices", {})
            for group in devices:
                key = json.dumps([group["pool"], group["h"], group["d"]])
                state = [
                    group.get("st"),
                    group.get("qu"),
                    group.get("qid"),
                    group.get("pr", 0),
                    group.get("v", 2),
                    group.get("mc", 0),
                    group.get("lo"),
                ]
                # A skipped group has no fresh reading. A quarantine change still
                # needs reporting independently of whether there is a reading.
                previous = seen.get(key)
                if group["st"] is None and previous:
                    state[0] = previous[0]
                if state != previous and self.outbox.append(
                    data,
                    {
                        **group,
                        "t": "IP",
                        "ts": group.get("ts"),
                    },
                ):
                    seen[key] = state

        self.outbox.change(observe)

    def _queue_pages(self, queues, revision, base):
        def observe(data):
            old = data.setdefault("queues", {})
            version = data.setdefault("queue_version", 0)
            current = {}
            for pool, kind in sorted(
                set(queues) | {tuple(json.loads(name)) for name in old}
            ):
                key = json.dumps([pool, kind])
                rows = queues.get((pool, kind), [])
                definition = {"pool": pool, "k": kind, "rev": revision}
                fingerprint = hashlib.sha256(_encode((definition, rows))).hexdigest()
                previous = old.get(key)
                if previous and previous["fingerprint"] == fingerprint:
                    current[key] = previous
                    continue
                version += 1
                template = {
                    "t": "QP",
                    "ts": int(time.time()),
                    "s": data["seq"] + OUTBOX_LIMIT,
                    "v": version,
                    "last": 1,
                    **definition,
                }
                wire_rows = []
                for row in rows:
                    # A shape can have many valid candidate combinations.
                    # Chunk even a single case's options without truncating them.
                    offset = 0
                    for options in self.packets(
                        "events",
                        row["ix"],
                        lambda options, row=row, template=template: {
                            **base,
                            "ev": [
                                {
                                    **template,
                                    "items": [{**row, "ix": options, "off": 8192}],
                                }
                            ],
                        },
                        max_items=96,
                        budget=1800,
                    ):
                        wire_rows.append({**row, "ix": options, "off": offset})
                        offset += len(options)
                    if not offset:
                        wire_rows.append(row)
                pages = list(
                    self.packets(
                        "events",
                        wire_rows,
                        lambda items, template=template: {
                            **base,
                            "ev": [{**template, "items": items}],
                        },
                        max_items=8,
                        budget=1800,
                    )
                ) or [[]]
                if len(data["events"]) + len(pages) > OUTBOX_LIMIT:
                    if previous:
                        current[key] = previous
                    continue
                published = []
                for index, items in enumerate(pages):
                    page = {**definition, "v": version, "items": items}
                    self.outbox.append(
                        data,
                        {
                            **page,
                            "t": "QP",
                            "ts": template["ts"],
                            "last": int(index == len(pages) - 1),
                        },
                    )
                    published.append(page)
                if rows:
                    current[key] = {"fingerprint": fingerprint, "pages": published}
            data["queues"] = current
            data["queue_version"] = version
            return [page for queue in current.values() for page in queue["pages"]]

        return self.outbox.change(observe)

    def commands(self):
        return [
            cmd
            for cmd in self.outbox.view()["commands"].values()
            if "receipt" not in cmd
        ]

    def complete(self, command, status, **fields):
        self.outbox.change(
            lambda data: data["commands"][command].update(
                receipt={
                    "cmd_id": command,
                    "status": status,
                    "ts": int(time.time()),
                    **{
                        key: _bounded(value, 200) if key == "err" else value
                        for key, value in fields.items()
                    },
                }
            )
        )

    def _receipts(self):
        for command, stored in self.outbox.view()["commands"].items():
            if not stored.get("acked"):
                self._send(
                    "command-receipts",
                    {"cmd_id": command, "status": "ACKED", "ts": int(time.time())},
                )
                self.outbox.change(
                    lambda data: data["commands"][command].update(acked=True)
                )
            if stored.get("receipt"):
                self._send("command-receipts", stored["receipt"])
                self.outbox.change(lambda data: data["commands"].pop(command, None))

    def _pull_commands(self):
        for command in self._send("commands")["commands"]:
            if command["cmd_id"] in self.outbox.view()["commands"]:
                continue
            if command["cmd_type"] == "SYNC_CONFIG":
                revision = int(command["payload"]["revision"])
                command["config"] = self._send(f"configs/{revision}")
            self.outbox.change(
                lambda data: data["commands"].setdefault(command["cmd_id"], command)
            )
            self.wake.set()

    def cycle(self):
        view = self.view()
        records = view["records"]
        base = {"w": self.worker, "i": self.outbox.view()["instance"]}
        self._capture(records, base)
        self._capture_devices(view.get("devices", []))
        queues = self._queue_pages(
            view.get("queues", {}), view.get("revision", 0), base
        )
        journal = self.outbox.view()
        now = self.clock()
        if now >= self.next_heartbeat:
            try:
                ledger = json.loads(self.policy.path.read_bytes())
                egress_used = (
                    ledger["used"] if ledger["day"] == int(time.time() // 86400) else 0
                )
            except (OSError, ValueError, KeyError):
                egress_used = 0
            result = self._send(
                "heartbeat",
                {
                    **base,
                    "ts": int(time.time()),
                    "rev": view.get("revision", 0),
                    "sha": (view.get("sha") or "")[:8],
                    "ok": int(view.get("healthy", True)),
                    "act": sum(
                        record.get("phase") in ACTIVE - {"QUEUED"}
                        for record in records.values()
                    ),
                    "q": sum(
                        record.get("phase") == "QUEUED" for record in records.values()
                    ),
                    "idle_ts": view.get("idle_ts"),
                    "probe_ts": view.get("probe_ts"),
                    "eu": egress_used,
                },
            )
            self.need_snapshot |= result["need_snapshot"]
            self.next_heartbeat = now + 30
            if result["pending_commands"]:
                self._pull_commands()
        self._receipts()

        def rebase(seq):
            contents = {
                "task": [
                    task_descriptor(key, record)
                    for key, record in records.items()
                    if record.get("phase") in ACTIVE
                ],
                "lease": [
                    {"tid": hashlib.sha256(key.encode()).hexdigest()[:12], "a": [node]}
                    for key, record in records.items()
                    if record.get("phase") in ACTIVE
                    for node in _allocation(record)
                ],
                "device": view.get("devices", []),
                "queue": queues,
            }
            self.snapshot(base, contents, seq)
            self.need_snapshot = False

        if journal.get("snapshot"):
            self._send_snapshot()
            self.need_snapshot = False
            self.rebase_before = None
        if self.rebase_before is not None:
            rebase(self.rebase_before)
            self.rebase_before = None
        contiguous = []
        for event in journal["events"]:
            if not contiguous or event["s"] != contiguous[-1][-1]["s"] + 1:
                contiguous.append([])
            contiguous[-1].append(event)
        for events in contiguous:
            for batch in self.packets(
                "events", events, lambda rows: {**base, "ev": rows}, max_items=6
            ):
                result = self._send("events", {**base, "ev": batch})
                if result["need_snapshot"]:
                    self.rebase_before = batch[0]["s"] - 1
                    return
                self.outbox.acknowledge(batch[-1]["s"])
        if self.need_snapshot:
            rebase(journal["seq"])

    def run(self):
        delay = 30
        while not self.stop.is_set():
            try:
                self.cycle()
                delay = 30
                self.stop.wait(5)
            except Exception as exc:
                logging.warning("control center deferred (%s)", type(exc).__name__)
                self.need_snapshot = True
                self.next_heartbeat = 0
                self.stop.wait(delay)
                delay = min(delay * 2, 300)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
