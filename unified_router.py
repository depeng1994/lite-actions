#!/usr/bin/env python3
"""Launch one isolated Pull worker; both regions use this same implementation."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import signal
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from ci_core.config import deployment, load_active_pools
from ci_core.contract import configured_repos, is_valid_worker_id, parse_worker_identity
from ci_core.egress_policy import Egress
from ci_core.gitcode import GitCodeClient, MultiPlatformClient
from ci_core.github import GitHubClient, MultiRepoGitHubClient, authorization
from ci_core.permissions import PRIVATE_DIR_MODE, WORKER_UMASK
from ci_core.resources import single_agent
from ci_core.router import Router
from ci_core.worker_activity import activity_lock, try_lock

ROOT = Path(__file__).resolve().parent


def _worker_id_arg(value: str) -> str:
    if not is_valid_worker_id(value):
        raise argparse.ArgumentTypeError(f"invalid worker-id: {value!r}")
    return value


def _write_sanitized_pools(pools_path: Path, hello: dict) -> None:
    pools = hello["pools"]
    encoded = json.dumps(pools, ensure_ascii=False, separators=(",", ":"))
    revision = max(1, int(hello.get("pools_revision") or 0))
    doc = {
        "revision": revision,
        "pools": pools,
        "config_json": encoded,
        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    }
    pools_path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    pools_path.write_text(
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def main():
    os.umask(WORKER_UMASK)
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", type=_worker_id_arg)
    p.add_argument("--broker-sock", default="")
    p.add_argument("--validate", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    broker_sock = (
        a.broker_sock or os.environ.get("LITE_ACTIONS_BROKER_SOCK", "")
    ).strip()
    broker_client = None
    hello = None
    if broker_sock:
        from ci_core.test_broker import BrokerClient, BrokerControlAdapter

        broker_client = BrokerClient(broker_sock, worker_id=a.worker_id)
        hello = broker_client.hello()
        hw_id = hello["worker_id"]
        if not is_valid_worker_id(hw_id) or not parse_worker_identity(hw_id)["is_test"]:
            raise ValueError(f"broker returned invalid test worker-id: {hw_id!r}")
        if a.worker_id is not None and a.worker_id != hw_id:
            raise ValueError("worker-id mismatch with broker HELLO")
        a.worker_id = hw_id
        os.environ["LITE_ACTIONS_BROKER_SOCK"] = broker_sock
        state_dir = os.environ.get("LITE_ACTIONS_STATE_DIR") or hello["state_dir"]
        Path(state_dir).mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        os.environ["LITE_ACTIONS_STATE_DIR"] = state_dir
        pools_path = Path(
            os.environ.get("LITE_ACTIONS_ACTIVE_POOLS_PATH")
            or (Path(state_dir) / "pools-active.json")
        )
        _write_sanitized_pools(pools_path, hello)
        os.environ["LITE_ACTIONS_ACTIVE_POOLS_PATH"] = str(pools_path)
        zone = parse_worker_identity(a.worker_id)["zone"]
        cfg = {
            "address": hello["address"],
            "state_dir": state_dir,
            "zone_state_dir": state_dir,
            "egress_ledger_dir": state_dir,
            "case_environments": (hello.get("workers") or {})
            .get(zone, {})
            .get("case_environments", {}),
            "repos": hello.get("repos") or [],
            "gitcode_repos": hello.get("gitcode_repos") or [],
        }
        is_test = True
        policy = Egress(a.worker_id, cfg)
        load_active_pools(ROOT, a.worker_id)
        repos = tuple(hello.get("repos") or configured_repos())
        lock_path = Path(state_dir) / "agent.lock"
    else:
        a.worker_id, cfg = deployment(a.worker_id)
        os.environ["LITE_ACTIONS_STATE_DIR"] = cfg["state_dir"]
        is_test = parse_worker_identity(a.worker_id)["is_test"]
        policy = Egress(a.worker_id, cfg)
        load_active_pools(ROOT, a.worker_id)
        repos = configured_repos()
        lock_path = None
    if a.validate:
        print(f"Validated worker {a.worker_id} repositories: {', '.join(repos)}")
        return

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ready_file = os.environ.get("LITE_ACTIONS_READY_FILE", "").strip()
    work_lock = (
        activity_lock(Path(ready_file), create=True)
        if ready_file and not is_test
        else nullcontext()
    )
    with single_agent(lock_path), work_lock as activity:
        if broker_client is not None:
            router = Router(
                ROOT,
                a.worker_id,
                broker_client,
                hello["author"],
                deployment_config=cfg,
                broker_client=broker_client,
            )
            wake = threading.Event()
            control = BrokerControlAdapter(broker_client, wake, router.control_view)
            router.control = control
            control.start()
        else:
            client = None
            if os.environ.get("LITE_ACTIONS_GITHUB_ENABLED", "true").lower() == "true":
                token = authorization()
                client = (
                    MultiRepoGitHubClient(token, repos, policy=policy)
                    if len(repos) > 1
                    else GitHubClient(token, repos[0], policy=policy)
                )
            gitcode_repos = tuple(
                filter(
                    None, os.environ.get("LITE_ACTIONS_GITCODE_REPOS", "").split(",")
                )
            )
            if gitcode_repos:
                client = MultiPlatformClient(
                    client,
                    {
                        repo: GitCodeClient(
                            os.environ.get("LITE_ACTIONS_GITCODE_TOKEN"),
                            repo,
                            policy=policy,
                        )
                        for repo in gitcode_repos
                    },
                )
            if client is None:
                raise ValueError("at least one source provider is required")
            router = Router(
                ROOT,
                a.worker_id,
                client,
                os.environ.get("LITE_ACTIONS_RESULT_AUTHOR"),
                deployment_config=cfg,
            )
            control = None
            wake = threading.Event()
            if cfg.get("control_center"):
                from ci_core.control_client import ControlClient

                try:
                    control = ControlClient(
                        a.worker_id,
                        cfg,
                        os.environ.get("LITE_ACTIONS_CONTROL_TOKEN"),
                        policy,
                        wake,
                        router.control_view,
                    )
                    router.control = control
                    if (
                        not is_test
                        and os.environ.get("LITE_ACTIONS_DISABLE_IDLE_PROBE", "") != "1"
                    ):
                        from ci_core.idle_probe import IdleProbe

                        router.idle_probe = IdleProbe(
                            a.worker_id,
                            lambda: (router.pools, router.state.read()),
                            state_dir=cfg["state_dir"],
                            report=control.capture_device_probes,
                        )
                        router.idle_probe.start()
                    control.start()
                except Exception as exc:
                    control = None
                    router.control = None
                    logging.warning(
                        "control center startup deferred (%s)", type(exc).__name__
                    )
        ready_published = False
        default_poll = "15" if broker_client is not None else "60"
        min_poll = 15 if broker_client is not None else 60
        poll_seconds = max(
            min_poll, int(os.environ.get("LITE_ACTIONS_POLL_SECONDS", default_poll))
        )
        next_poll = 0
        try:
            while True:
                if activity is not None and not try_lock(activity):
                    wake.wait(1)
                    wake.clear()
                    continue
                tick_succeeded = False
                try:
                    if time.monotonic() >= next_poll:
                        next_poll = time.monotonic() + poll_seconds
                        router.tick()
                        if broker_client is not None and control is not None:
                            control.flush_snapshot()
                        router.control_healthy = True
                        tick_succeeded = True
                    elif control:
                        router.consume_control()
                        router.pump()
                        router.flush()
                except Exception:
                    router.control_healthy = False
                    logging.exception("worker poll failed")
                # The optional marker is written only after a successful poll.
                # A failed poll must never convince the updater to commit a release.
                if tick_succeeded and ready_file and not ready_published:
                    try:
                        from ci_core.worker_ready import publish_ready

                        published = publish_ready(
                            ROOT,
                            Path(ready_file),
                            release_sha=(hello.get("release_sha") or None)
                            if hello is not None
                            else None,
                        )
                        ready_published = True
                        logging.info(
                            "worker ready published sha=%s pid=%s",
                            published["release_sha"],
                            published["pid"],
                        )
                    except Exception:
                        logging.exception("worker readiness publication failed")
                if activity is not None and not router.has_active_work():
                    fcntl.flock(activity.fileno(), fcntl.LOCK_UN)
                if a.once:
                    break
                if wake.wait(max(0, next_poll - time.monotonic())):
                    wake.clear()
        except KeyboardInterrupt:
            pass
        finally:
            router.close()
            if control:
                control.close()
            if router.idle_probe:
                router.idle_probe.close()


if __name__ == "__main__":
    main()
