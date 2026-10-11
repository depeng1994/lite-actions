"""Explicitly allowlisted model repository for a dispatcher deployment."""
import os

ALLOWED_REPOS = frozenset({
    "depeng1994/torchtitan-npu",
    "torchtitan-npu/torchtitan-npu",
})
REPO = os.environ.get("A3_CI_REPOSITORY", "depeng1994/torchtitan-npu")
if REPO not in ALLOWED_REPOS:
    raise ValueError("untrusted A3_CI_REPOSITORY")
