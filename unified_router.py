#!/usr/bin/env python3
"""Launch one isolated Pull worker; both regions use this same implementation."""

import argparse
import logging
import os
import signal
import time
from pathlib import Path

from ci_core.config import load
from ci_core.contract import configured_repos
from ci_core.github import GitHubClient, MultiRepoGitHubClient, authorization
from ci_core.resources import single_agent
from ci_core.router import Router

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True, choices=["blue", "yellow"])
    p.add_argument("--validate", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--gitcode-config", action="append", type=Path, default=[])
    p.add_argument("--gitcode-state-directory", type=Path)
    a = p.parse_args()
    if a.gitcode_config and (
        a.worker_id != "blue" or a.gitcode_state_directory is None
    ):
        p.error("GitCode requires Blue and an explicit durable state directory")
    if a.gitcode_state_directory is not None and not a.gitcode_config:
        p.error("GitCode state directory requires an explicit release config")
    if a.gitcode_config:
        from ci_core.gitcode_assembly import load_release

        for config_path in a.gitcode_config:
            load_release(config_path)
    load(ROOT, a.worker_id)
    repos = configured_repos()
    if a.validate:
        print(f"Validated worker {a.worker_id} repositories: {', '.join(repos)}")
        return

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with single_agent():
        token = authorization()
        gitcode_adapter = None
        if a.gitcode_config:
            from ci_core.gitcode_assembly import assemble_blue

            gitcode_adapter = assemble_blue(
                a.gitcode_config,
                environment=os.environ,
                state_directory=a.gitcode_state_directory,
            )
        schema4_clients = (
            tuple(b.reader.github for b in gitcode_adapter.brokers.values())
            if gitcode_adapter is not None
            else ()
        )
        client = (
            MultiRepoGitHubClient(token, repos, schema4_clients=schema4_clients)
            if len(repos) > 1 or schema4_clients
            else GitHubClient(token, repos[0])
        )
        router = Router(
            ROOT,
            a.worker_id,
            client,
            os.environ.get("LITE_ACTIONS_RESULT_AUTHOR"),
            gitcode_adapter=gitcode_adapter,
        )
        try:
            while True:
                try:
                    router.tick()
                except Exception:
                    logging.exception("worker poll failed")
                if a.once:
                    break
                time.sleep(int(os.environ.get("LITE_ACTIONS_POLL_SECONDS", "10")))
        except KeyboardInterrupt:
            pass
        finally:
            router.close()


if __name__ == "__main__":
    main()
