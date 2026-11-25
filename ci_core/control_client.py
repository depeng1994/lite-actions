"""Opt-in, pull-only telemetry and command transport for the control center."""

from __future__ import annotations

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

from ci_core.contract import REPO, parse_worker_identity
from ci_core.egress_policy import (
    Rejected,
    control_origin,
    control_proxy,
    estimate_http1_request_bytes,
)

TARGET_BYTES = 1880
RESPONSE_LIMIT = 1024 * 1024
OUTBOX_LIMIT = 5000
HEALTH_REFRESH_SECONDS = 900
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


TASK_DESCRIPTOR_BUDGET = 1240


def _tid_for_key(key, reporter_id=None):
    if reporter_id:
        try:
            if parse_worker_identity(reporter_id)["is_test"]:
                return hashlib.sha256(
                    f"{key}:reporter:{reporter_id}".encode()
                ).hexdigest()[:12]
        except ValueError:
            pass
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _normalize_queues(raw_queues):
    if not raw_queues:
        return {}
    if isinstance(raw_queues, dict):
        result = {}
        for k, v in raw_queues.items():
            if isinstance(k, tuple) and len(k) == 2:
                result[(str(k[0]), str(k[1]))] = v
            elif isinstance(k, str) and "|" in k:
                pool, kind = k.split("|", 1)
                result[(pool, kind)] = v
        return result
    if isinstance(raw_queues, list):
        return {
            (str(entry["pool"]), str(entry["kind"])): entry.get("rows", [])
            for entry in raw_queues
            if isinstance(entry, dict) and "pool" in entry and "kind" in entry
        }
    return {}


def task_descriptor(key, record, *, reporter_id=None):
    run, item = record["run"], record["item"]
    result = {
        "tid": _tid_for_key(key, reporter_id),
        "p": run.get("_ci_provider", "github"),
        "r": run.get("_ci_repo", REPO),
        "run": str(run["id"]),
        "att": int(run.get("run_attempt", 1)),
        "did": run.get("_ci_dispatch_id"),
        "cid": item["case_id"],
        "lbl": _bounded(item.get("label") or item["case_id"], 80),
        "k": item["kind"],
        "pool": item.get("target_pool") or "",
        "req": [item["resources"]["nodes"], item["resources"]["devices_per_node"]],
        "pr": run.get("_ci_pr_number"),
        "pa": _login(run.get("_ci_pr_author")),
        "tr": _actor(run.get("triggering_actor") or run.get("actor")),
        "et": _bounded(run.get("event"), 40),
        "wf": _bounded(run.get("path"), 80),
        "sub": _submitted_at(run, record.get("queued_at")),
        "wn": _bounded(run.get("name"), 48),
        "rn": run.get("run_number")
        if type(run.get("run_number")) is int and run["run_number"] > 0
        else None,
        "dt": _bounded(run.get("display_title") or run.get("run_name"), 64),
        "ph": record["phase"] if record["phase"] in ACTIVE else "QUEUED",
        "ts": int(record.get("queued_at", 0)),
        "tm": record.get("phase_timestamps", {}),
        "reliable": int(bool(record.get("phase_timestamps", {}).get("QUEUED"))),
    }
    compact = {name: value for name, value in result.items() if value is not None}
    # Under worst-case identity lengths + 80-char origin + 64-char Bearer token,
    # trim optional display fields dynamically so one TU / snapshot task item
    # always fits well under TARGET_BYTES (1,880 B) and the hard <2,000 B gate.
    for field, floor in (("dt", 0), ("wn", 0), ("wf", 0), ("lbl", 16)):
        overflow = len(_encode(compact)) - TASK_DESCRIPTOR_BUDGET
        if overflow <= 0:
            break
        if field not in compact:
            continue
        current_bytes = len(compact[field].encode())
        target_bytes = max(floor, current_bytes - overflow)
        if target_bytes == 0:
            compact.pop(field, None)
        else:
            compact[field] = _bounded(compact[field], target_bytes)
    return compact


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
        self.proxy = (
            control_proxy(cfg["control_center"]["proxy"])
            if "proxy" in cfg["control_center"]
            else None
        )
        self.outbox = ControlOutbox(cfg["state_dir"])
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            # Some provider edges reject urllib's default Python-urllib
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
        self._test_reporters = {}
        self._test_lock = threading.Lock()
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
        # Keep environment proxies disabled by default. A proxy is used only
        # when explicitly present in the root-owned deployment configuration.
        proxy_handlers = {"https": self.proxy} if self.proxy is not None else {}
        handlers = [urllib.request.ProxyHandler(proxy_handlers), _NoRedirect()]
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

    def _capture_into(self, data, records, base, *, reporter_id=None):
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
            task = task_descriptor(key, record, reporter_id=reporter_id)
            tid, now = task["tid"], int(time.time())
            raw_alloc = _allocation(record)
            alloc = (
                raw_alloc
                if (raw_alloc or phase == "QUEUED")
                else (current or {}).get("allocation", [])
            )
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
            elif phase != current.get("phase") or task["tm"] != current.get("times"):
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

    def _capture(self, records, base=None):
        base = base or {"w": self.worker, "i": self.outbox.view()["instance"]}
        self.outbox.change(lambda data: self._capture_into(data, records, base))

    def capture_device_probes(self):
        self._capture_devices(self.view().get("devices", []))

    def _capture_devices(self, devices):
        def observe(data):
            seen = data.setdefault("devices", {})
            sampled = data.setdefault("device_sample_at", {})
            for group in devices:
                key = json.dumps([group["pool"], group["h"], group["d"]])
                timestamp = group.get("ts")
                last_sample = sampled.get(key, 0)
                if timestamp is not None and timestamp < last_sample:
                    continue
                fresh_sample = timestamp is not None and timestamp > last_sample
                occupancy = "leased" if group.get("sk") else group.get("st")
                state = [
                    occupancy,
                    group.get("qu"),
                    group.get("qid"),
                    group.get("v"),
                    group.get("mc"),
                ]
                # No reading after a config change does not undo the last sample.
                previous = seen.get(key)
                if occupancy is None and previous:
                    state[0] = previous[0]
                refresh = fresh_sample and (
                    occupancy == "busy"
                    or (
                        (
                            occupancy == "idle"
                            or (occupancy == "leased" and group.get("v") == 0)
                        )
                        and timestamp - last_sample >= HEALTH_REFRESH_SECONDS
                    )
                )
                payload = {
                    name: value
                    for name, value in group.items()
                    if value is not None or name == "ts"
                }
                if (refresh or state != previous) and self.outbox.append(
                    data,
                    {
                        **payload,
                        "t": "IP",
                        "ts": group.get("ts"),
                    },
                ):
                    seen[key] = state
                    if fresh_sample:
                        sampled[key] = timestamp

        self.outbox.change(observe)

    def _queue_pages_into(self, data, queues, revision, base):
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

    def _queue_pages(self, queues, revision, base):
        return self.outbox.change(
            lambda data: self._queue_pages_into(data, queues, revision, base)
        )

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

    def _require_zone_test_worker(self, worker_id: str) -> None:
        identity = parse_worker_identity(worker_id)
        if not identity["is_test"] or identity["zone"] != self.worker:
            raise ValueError("WORKER_ZONE_MISMATCH")

    def _test_reporter_state(self, worker_id: str, instance_id: str | None = None):
        state = self._test_reporters.get(worker_id)
        if state is None:
            inst = (
                instance_id
                if isinstance(instance_id, str)
                and re.fullmatch(r"[0-9a-f]{8}", instance_id)
                else uuid4().hex[:8]
            )
            state = {
                "instance": inst,
                "seq": 0,
                "acked": 0,
                "events": [],
                "tracked": {},
                "queues": {},
                "queue_version": 0,
                "started_at": time.time(),
            }
            self._test_reporters[worker_id] = state
        return state

    def register_test_worker(self, record: dict) -> dict:
        worker_id = str(record["worker_id"])
        self._require_zone_test_worker(worker_id)
        with self._test_lock:
            state = self._test_reporter_state(worker_id, record.get("instance_id"))
            ttl = int(record.get("ttl_seconds", 14400))
            payload = {
                "w": worker_id,
                "z": self.worker,
                "r": record["repo"],
                "rid": str(record["run_id"]),
                "at": int(record.get("attempt", 1)),
                "ttl": ttl,
                "exp": int(record.get("expires_at", int(time.time()) + ttl)),
                "i": state["instance"],
                "st": (
                    record.get("status")
                    if record.get("status") in {"DEPLOYING", "ACTIVE"}
                    else "ACTIVE"
                ),
            }
            if record.get("pr_number") is not None:
                payload["pr"] = int(record["pr_number"])
            if record.get("pr_head_sha"):
                payload["sha"] = str(record["pr_head_sha"])
            return self._send("test-workers/register", payload)

    def deregister_test_worker(
        self, worker_id: str, *, reason: str = "cleanup"
    ) -> dict:
        self._require_zone_test_worker(worker_id)
        with self._test_lock:
            self._test_reporters.pop(worker_id, None)
            return self._send(
                "test-workers/deregister",
                {"w": worker_id, "reason": _bounded(reason, 64)},
            )

    def _send_test_snapshot(
        self, test_worker_id: str, base: dict, records: dict, queues: list, seq: int
    ) -> dict:
        contents = {
            "task": [
                task_descriptor(key, record, reporter_id=test_worker_id)
                for key, record in records.items()
                if record.get("phase") in ACTIVE
            ],
            "lease": [
                {
                    "tid": _tid_for_key(key, test_worker_id),
                    "a": [node],
                }
                for key, record in records.items()
                if record.get("phase") in ACTIVE
                for node in _allocation(record)
            ],
            "queue": queues,
        }
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
        for index, (kind, items) in enumerate(pages, 1):
            result = self._send(
                "snapshots",
                {
                    **base,
                    "sid": sid,
                    "p": index,
                    "n": len(pages),
                    "seq": seq,
                    "kind": kind,
                    "items": items,
                },
            )
            if index == len(pages) and not result.get("complete"):
                raise ValueError("CONTROL_SNAPSHOT_INCOMPLETE")
        return {"ok": True, "complete": True, "parts": len(pages)}

    def forward_test_telemetry(
        self, test_worker_id: str, *, events=(), snapshot=None
    ) -> dict:
        """Proxy Test Worker task/queue telemetry within Main Worker egress bounds."""
        self._require_zone_test_worker(test_worker_id)
        with self._test_lock:
            state = self._test_reporter_state(test_worker_id)
            base = {"w": test_worker_id, "i": state["instance"]}
            dropped_probes = 0
            for raw in events or ():
                if not isinstance(raw, dict):
                    continue
                if raw.get("t") == "IP":
                    dropped_probes += 1
                    continue
                cleaned = {k: v for k, v in raw.items() if k != "s"}
                self.outbox.append(state, cleaned)

            records = {}
            queues_list = [
                page for queue in state["queues"].values() for page in queue["pages"]
            ]
            if isinstance(snapshot, dict):
                records = snapshot.get("records") or {}
                if snapshot.get("devices"):
                    dropped_probes += len(snapshot["devices"])
                self._capture_into(state, records, base, reporter_id=test_worker_id)
                norm_queues = _normalize_queues(snapshot.get("queues"))
                queues_list = self._queue_pages_into(
                    state,
                    norm_queues,
                    int(snapshot.get("revision", 0)),
                    base,
                )

            sent_events = 0
            sent_snapshots = 0
            contiguous = []
            for event in state["events"]:
                if not contiguous or event["s"] != contiguous[-1][-1]["s"] + 1:
                    contiguous.append([])
                contiguous[-1].append(event)
            for chunk in contiguous:
                for batch in self.packets(
                    "events",
                    chunk,
                    lambda rows: {**base, "ev": rows},
                    max_items=6,
                ):
                    result = self._send("events", {**base, "ev": batch})
                    sent_events += len(batch)
                    if result.get("need_snapshot"):
                        rebase_seq = batch[0]["s"] - 1
                        self._send_test_snapshot(
                            test_worker_id, base, records, queues_list, rebase_seq
                        )
                        sent_snapshots += 1
                        state["acked"] = rebase_seq
                        state["events"] = [
                            e for e in state["events"] if e["s"] > rebase_seq
                        ]
                        # Re-send buffered events now that baseline seq matches.
                        for retry in self.packets(
                            "events",
                            state["events"],
                            lambda rows: {**base, "ev": rows},
                            max_items=6,
                        ):
                            self._send("events", {**base, "ev": retry})
                            sent_events += len(retry)
                            state["acked"] = retry[-1]["s"]
                            state["events"] = [
                                e for e in state["events"] if e["s"] > state["acked"]
                            ]
                        return {
                            "ok": True,
                            "events": sent_events,
                            "snapshots": sent_snapshots,
                            "dropped_probes": dropped_probes,
                        }
                    state["acked"] = batch[-1]["s"]
                    state["events"] = [
                        e for e in state["events"] if e["s"] > state["acked"]
                    ]
            return {
                "ok": True,
                "events": sent_events,
                "snapshots": sent_snapshots,
                "dropped_probes": dropped_probes,
            }

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
