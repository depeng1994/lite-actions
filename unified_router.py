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
    a = p.parse_args()
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
        client = (
            MultiRepoGitHubClient(token, repos)
            if len(repos) > 1
            else GitHubClient(token, repos[0])
        )
        router = Router(
            ROOT,
            a.worker_id,
            client,
            os.environ.get("LITE_ACTIONS_RESULT_AUTHOR"),
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
