"""Run a trusted, identity-checked probe on a configured Lite Actions worker."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_ID = re.compile(r"[a-z][a-z0-9-]{0,62}\\Z")
PROBE = "printf \"WORKER_HOSTNAME=\"; hostname -f; printf \"WORKER_ARCH=\"; uname -m; printf \"WORKER_PYTHON=\"; python3 --version"


def load_worker(worker_id: str, config_path: Path) -> dict:
    if not WORKER_ID.fullmatch(worker_id):
        raise ValueError("invalid worker ID")
    workers = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(workers, dict):
        raise ValueError("worker registry must be an object")
    worker = workers.get(worker_id)
    if not isinstance(worker, dict) or worker.get("enabled") is not True:
        raise ValueError("worker is not registered and enabled")
    transport = worker.get("transport")
    if transport == "local":
        allowed = {"transport", "enabled", "expected_hostname", "capabilities"}
        if set(worker) - allowed:
            raise ValueError("unexpected local worker fields")
    elif transport == "ssh":
        allowed = {"transport", "enabled", "host", "expected_hostname", "capabilities"}
        if not isinstance(worker.get("host"), str) or not worker["host"].strip():
            raise ValueError("SSH worker host alias is required")
        if set(worker) - allowed:
            raise ValueError("unexpected SSH worker fields")
    else:
        raise ValueError("unsupported worker transport")
    expected = worker.get("expected_hostname")
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise ValueError("invalid expected worker hostname")
    capabilities = worker.get("capabilities", [])
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise ValueError("invalid worker capabilities")
    return worker


def run_probe(worker_id: str, worker: dict) -> int:
    transport = worker["transport"]
    if transport == "local":
        command = ["bash", "-lc", PROBE]
    else:
        # Host-key verification stays enabled; host must be a trusted SSH config alias.
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   worker["host"], "bash -lc " + shlex.quote(PROBE)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    output = completed.stdout.strip()
    if output:
        print(output)
    if completed.stderr.strip():
        print(completed.stderr.strip())
    if completed.returncode:
        print("WORKER_STATUS=FAIL")
        return completed.returncode
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    actual = fields.get("WORKER_HOSTNAME", "")
    expected = worker.get("expected_hostname")
    if expected and actual != expected:
        print(f"WORKER_ID={worker_id}")
        print(f"WORKER_STATUS=FAIL expected_hostname_mismatch expected={expected!r} actual={actual!r}")
        return 1
    print(f"WORKER_ID={worker_id}")
    print(f"WORKER_TRANSPORT={transport}")
    print("WORKER_STATUS=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/workers.json")
    args = parser.parse_args()
    try:
        worker = load_worker(args.worker_id, args.config)
        return run_probe(args.worker_id, worker)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"WORKER_STATUS=FAIL error={type(exc).__name__}: {str(exc)[:180]}")
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
