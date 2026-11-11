"""Worker-wide HTTP egress admission; budgets are reserved before opening a socket."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path


class Deferred(Exception):
    """A local budget is exhausted; do not attempt or count an HTTP retry."""


class Rejected(ValueError):
    """A permanent policy rejection; never retry the same envelope."""


# Only the API reads needed for source proof, discovery, status and result correlation.
_READ = re.compile(
    r"(?:|/actions/runs(?:/[A-Za-z0-9_-]{1,128}(?:/artifacts|/attempts/[1-9][0-9]*/jobs)?)?|/actions/workflows/[0-9]+|/actions/artifacts/[A-Za-z0-9_-]{1,128}/zip|/pulls/[0-9]+|/branches/[A-Za-z0-9._/%-]{1,300}|/compare/[a-f0-9]{40}\.\.\.[a-f0-9]{40}|/commits/[a-f0-9]{40}/comments|/git/commits/[a-f0-9]{40})\Z"
)
_QUERY = {
    "status": r"queued|in_progress|RUNNING",
    "page": r"[1-9][0-9]?",
    "per_page": r"100|10|1",
    "name": r"lite-ci-(?:source|request)-[1-9][0-9]*",
}


def yellow_body(payload, commit):
    from ci_core.logs import yellow_lines

    if not payload or len(payload) > 7000:
        raise Rejected("YELLOW_ENVELOPE_SIZE")
    from ci_core.registry import _unique

    obj = json.loads(payload, object_pairs_hook=_unique)
    if (
        set(obj) != {"body"}
        or not isinstance(obj["body"], str)
        or len(obj["body"].encode()) > 6100
    ):
        raise Rejected("YELLOW_ENVELOPE_SHAPE")
    body = obj["body"]
    if body.startswith("Lite-CI-PROGRESS "):
        r = json.loads(body[len("Lite-CI-PROGRESS ") :], object_pairs_hook=_unique)
        keys = {"run_id", "attempt", "case_id", "worker", "plan_digest", "phase"}
        if (
            set(r) - {"dispatch_id"} != keys
            or r["phase"] != "QUEUED"
            or len(payload) > 1024
        ):
            raise Rejected("YELLOW_ACK_INVALID")
    elif body.startswith(("Lite-CI-V3 ", "Lite-CI-V4 ")):
        _, _, text = body.partition("\n")
        r = json.loads(text, object_pairs_hook=_unique)
        lines = r.get("diagnostics", r.get("last_20_lines"))
        if (
            not isinstance(lines, list)
            or r.get("sha") != commit
            or lines != yellow_lines(lines)
        ):
            raise Rejected("YELLOW_DIAGNOSTICS_INVALID")
        allowed = {
            "schema",
            "repo",
            "workflow",
            "run_id",
            "attempt",
            "sha",
            "plan_digest",
            "case_id",
            "test_id",
            "worker",
            "target_pool",
            "docker",
            "resources",
            "status",
            "exit_code",
            "cleanup_verified",
            "last_20_lines",
            "diagnostics",
            "log_locator",
            "source_digest",
            "finished_at",
            "dispatch_id",
        }
        if set(r) - allowed or ("diagnostics" in r) == ("last_20_lines" in r):
            raise Rejected("YELLOW_RESULT_FIELDS")
    else:
        raise Rejected("YELLOW_WRITE_NOT_ALLOWED")
    run_id = r.get("run_id")
    if (
        not (
            (type(run_id) is int and 0 < run_id < 2**63)
            or (type(run_id) is str and re.fullmatch(r"[0-9A-Za-z_-]{1,128}", run_id))
        )
        or type(r.get("attempt")) is not int
        or not 1 <= r["attempt"] < 2**31
    ):
        raise Rejected("YELLOW_RUN_IDENTITY_INVALID")
    if not re.fullmatch(
        r"case_[0-9]{3}_[a-z][a-z0-9_]{0,79}", str(r.get("case_id"))
    ) or not re.fullmatch(r"[0-9a-f]{64}", str(r.get("plan_digest"))):
        raise Rejected("YELLOW_CASE_IDENTITY_INVALID")
    if "dispatch_id" in r and not re.fullmatch(
        r"[0-9A-Za-z_-]{1,128}", str(r["dispatch_id"])
    ):
        raise Rejected("YELLOW_DISPATCH_INVALID")
    if not body.startswith("Lite-CI-PROGRESS "):
        from ci_core.contract import REPOSITORY, STATUS
        from ci_core.reporting import marker

        required = {
            "schema",
            "sha",
            "repo",
            "workflow",
            "test_id",
            "target_pool",
            "docker",
            "resources",
            "status",
            "exit_code",
            "cleanup_verified",
        }
        if (
            not required <= r.keys()
            or r["schema"] != 3
            or r["status"] not in STATUS
            or type(r["exit_code"]) is not int
            or type(r["cleanup_verified"]) is not bool
        ):
            raise Rejected("YELLOW_RESULT_SHAPE")
        if (
            not REPOSITORY.fullmatch(str(r["repo"]))
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,180}\.ya?ml", str(r["workflow"]))
            or r["test_id"] != r["case_id"][9:]
        ):
            raise Rejected("YELLOW_RESULT_IDENTITY_INVALID")
        envelope_run = {
            "id": run_id,
            "run_attempt": r["attempt"],
            "_ci_dispatch_id": r.get("dispatch_id"),
        }
        if body.split("\n", 1)[0] != marker(envelope_run, r):
            raise Rejected("YELLOW_RESULT_MARKER")
        res = r["resources"]
        if (
            type(res) is not dict
            or set(res) != {"nodes", "devices_per_node"}
            or any(type(n) is not int or not 0 <= n <= 256 for n in res.values())
        ):
            raise Rejected("YELLOW_RESOURCES_INVALID")
        if any(
            v is not None and not re.fullmatch(r"[0-9A-Za-z._:/-]{1,256}", str(v))
            for v in (r["docker"], r["target_pool"])
        ):
            raise Rejected("YELLOW_BINDING_INVALID")
        if "source_digest" in r and not re.fullmatch(
            r"[0-9a-f]{64}", str(r["source_digest"])
        ):
            raise Rejected("YELLOW_SOURCE_DIGEST_INVALID")
        if "finished_at" in r and type(r["finished_at"]) is not int:
            raise Rejected("YELLOW_FINISH_INVALID")
        if "log_locator" in r:
            from ipaddress import IPv4Address

            loc = r["log_locator"]
            if (
                type(loc) is not dict
                or set(loc) != {"host", "path"}
                or not isinstance(loc["host"], str)
                or not re.fullmatch(
                    r"/[0-9A-Za-z./_-]{1,600}/(?:run|probe|pipeline)\.log",
                    str(loc["path"]),
                )
                or ".." in loc["path"].split("/")
            ):
                raise Rejected("YELLOW_LOCATOR_INVALID")
            ip, _, port = loc["host"].partition(":")
            IPv4Address(ip)
            if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
                raise Rejected("YELLOW_LOCATOR_INVALID")
    if r.get("worker") != "yellow":
        raise Rejected("YELLOW_IDENTITY_MISMATCH")


class Egress:
    def __init__(self, worker, cfg, clock=time.time):
        if worker not in {"blue", "yellow"}:
            raise Rejected("UNKNOWN_WORKER")
        self.worker = worker
        self.cfg = cfg
        self.clock = clock
        self.root = Path(cfg["state_dir"])
        self.path = self.root / "http-egress.json"

    def before(self, request, *, repo, provider, storage=False):
        if self.worker != "yellow":
            return
        url = urllib.parse.urlsplit(request.full_url)
        method = request.get_method()
        payload = request.data
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.fragment
            or url.port not in (None, 443)
        ):
            raise Rejected("UNTRUSTED_DESTINATION")
        if storage:
            if (
                method != "GET"
                or payload is not None
                or url.hostname not in self.cfg.get("artifact_hosts", [])
                or any(
                    k.lower() in ("authorization", "private-token", "cookie")
                    for k, _ in request.header_items()
                )
            ):
                raise Rejected("UNTRUSTED_ARTIFACT_DESTINATION")
        else:
            host = {"github": "api.github.com", "gitcode": "api.gitcode.com"}[provider]
            prefix = (
                r"/repos/" if provider == "github" else r"/api/v[58]/repos/"
            ) + re.escape(repo)
            match = re.fullmatch(prefix + r"(/.*|)", url.path)
            if url.hostname != host or not match:
                raise Rejected("UNTRUSTED_API")
            path = match[1]
            if (
                len(url.query) > 300
                or len(url.query.split("&")) > 4
                or ".." in urllib.parse.unquote(path).split("/")
            ):
                raise Rejected("UNSUPPORTED_URL")
            if method == "GET":
                if (
                    not _READ.fullmatch(path)
                    or payload is not None
                    or not all(
                        k in _QUERY and re.fullmatch(_QUERY[k], v)
                        for k, v in urllib.parse.parse_qsl(
                            url.query, keep_blank_values=True, strict_parsing=True
                        )
                    )
                ):
                    raise Rejected("UNSUPPORTED_READ")
            else:
                m = re.fullmatch(r"/commits/([a-f0-9]{40})/comments", path)
                if method != "POST" or url.query or not m:
                    raise Rejected("UNSUPPORTED_WRITE")
                yellow_body(payload, m[1])
        # Conservative HTTP/1 header estimate; excludes TLS, which needs gateway measurement.
        headers = sum(
            len(k.encode()) + len(v.encode()) + 4 for k, v in request.header_items()
        )
        size = (
            192
            + len(method)
            + len(request.full_url.encode())
            + headers
            + len(payload or b"")
        )
        self.reserve(method == "GET", size, len(payload or b""))

    def reserve(self, read, size, body_bytes=0):
        with (self.root / "http-egress.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            now = self.clock()
            day = int(now // 86400)
            saved = (
                json.loads(self.path.read_bytes())
                if self.path.exists()
                else {
                    "at": now,
                    "day": day,
                    "used": 0,
                    "reads": 10,
                    "writes": 4,
                    "bytes": 16384,
                    "requests": 0,
                }
            )
            if now < saved["at"] or day < saved["day"]:
                raise Deferred("CLOCK_ROLLBACK")
            elapsed = now - saved["at"]
            saved["at"] = now
            if day > saved["day"]:
                saved.update(day=day, used=0)
            for key, cap, rate in [
                ("reads", 10, 60),
                ("writes", 4, 12),
                ("bytes", 16384, 131072),
            ]:
                saved[key] = min(cap, saved[key] + elapsed * rate / 60)
            key = "reads" if read else "writes"
            if (
                saved[key] < 1
                or saved["bytes"] < size
                or saved["used"] + size > self.cfg["daily_upload_bytes"]
            ):
                raise Deferred("YELLOW_BUDGET_EXHAUSTED")
            saved[key] -= 1
            saved["bytes"] -= size
            saved["used"] += size
            saved["requests"] += 1
            saved["body_bytes"] = saved.get("body_bytes", 0) + body_bytes
            counter = "read_attempts" if read else "write_attempts"
            saved[counter] = saved.get(counter, 0) + 1
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temp:
                name = temp.name
                try:
                    temp.write(json.dumps(saved, allow_nan=False).encode())
                    temp.flush()
                    os.fsync(temp.fileno())
                    os.replace(name, self.path)
                    fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    Path(name).unlink(missing_ok=True)
