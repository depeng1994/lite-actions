#!/usr/bin/env python3
"""Launch one isolated Pull worker; both regions use this same implementation."""

import argparse
import logging
import os
import signal
import time
from pathlib import Path

from ci_core.config import deployment, load
from ci_core.contract import configured_repos
from ci_core.egress_policy import Egress
from ci_core.gitcode import GitCodeClient, MultiPlatformClient
from ci_core.github import GitHubClient, MultiRepoGitHubClient, authorization
from ci_core.resources import single_agent
from ci_core.router import Router

ROOT = Path(__file__).resolve().parent


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", choices=["blue", "yellow"])
    p.add_argument("--validate", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    a.worker_id, cfg = deployment(a.worker_id)
    os.environ["LITE_ACTIONS_STATE_DIR"] = cfg["state_dir"]
    policy = Egress(a.worker_id, cfg)
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
        client = None
        if os.environ.get("LITE_ACTIONS_GITHUB_ENABLED", "true").lower() == "true":
            token = authorization()
            client = (
                MultiRepoGitHubClient(token, repos, policy=policy)
                if len(repos) > 1
                else GitHubClient(token, repos[0], policy=policy)
            )
        gitcode_repos = tuple(
            filter(None, os.environ.get("LITE_ACTIONS_GITCODE_REPOS", "").split(","))
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
        ready_file = os.environ.get("LITE_ACTIONS_READY_FILE", "").strip()
        ready_published = False
        try:
            while True:
                tick_succeeded = False
                try:
                    router.tick()
                    tick_succeeded = True
                except Exception:
                    logging.exception("worker poll failed")
                # The optional marker is written only after a successful poll.
                # A failed poll must never convince the updater to commit a release.
                if tick_succeeded and ready_file and not ready_published:
                    try:
                        from ci_core.worker_ready import publish_ready

                        published = publish_ready(ROOT, Path(ready_file))
                        ready_published = True
                        logging.info(
                            "worker ready published sha=%s pid=%s",
                            published["release_sha"],
                            published["pid"],
                        )
                    except Exception:
                        logging.exception("worker readiness publication failed")
                if a.once:
                    break
                time.sleep(int(os.environ.get("LITE_ACTIONS_POLL_SECONDS", "10")))
        except KeyboardInterrupt:
            pass
        finally:
            router.close()


if __name__ == "__main__":
    main()
