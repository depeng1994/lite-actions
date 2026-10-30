#!/usr/bin/env python3
"""Launch one isolated Pull worker; both regions use this same implementation."""

import argparse
import logging
import os
import signal
from pathlib import Path
import time
from ci_core.github import GitHubClient, authorization
from ci_core.router import Router
from ci_core.resources import single_agent
from ci_core.v2_config import load

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--worker-id", required=True, choices=["blue", "yellow"])
    p.add_argument("--validate", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    load(ROOT, a.worker_id)
    if a.validate:
        print(f"Validated worker {a.worker_id}")
        return
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    with single_agent():
        router = Router(
            ROOT,
            a.worker_id,
            GitHubClient(authorization()),
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
