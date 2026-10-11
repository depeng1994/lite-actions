"""Validate SHA-bound GitHub V2 Matrix plans without trusting arbitrary commands.

Only trusted model-defined Test IDs are executable, and the dispatcher
re-resolves each case from the exact pinned model commit.
"""
from __future__ import annotations
import hashlib
import json
import re

from ci_core.registry import SELECTOR

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REPO = "depeng1994/torchtitan-npu"
CASE_KEYS = {"test_id", "nnodes", "ngpu", "label"}
PLAN_KEYS = {"schema", "repo", "run_id", "attempt", "sha", "pipeline", "cases",
             "plan_digest"}


def plan_digest(plan: dict) -> str:
    canonical = json.dumps(plan, separators=(",", ":"), sort_keys=True,
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_plan(data: dict, run: dict, pipeline: str) -> list[dict]:
    if not isinstance(data, dict) or set(data) != PLAN_KEYS:
        raise ValueError("invalid V2 plan structure")
    if (data["schema"] != 2 or data["repo"] != REPO
            or data["run_id"] != run["id"]
            or data["attempt"] != run.get("run_attempt", 1)
            or data["sha"] != run["head_sha"] or data["pipeline"] != pipeline
            or not SHA.fullmatch(str(data["sha"]))):
        raise ValueError("V2 plan identity mismatch")
    if (not isinstance(data["plan_digest"], str)
            or not DIGEST.fullmatch(data["plan_digest"])):
        raise ValueError("invalid V2 plan digest")
    unsigned = {k: v for k, v in data.items() if k != "plan_digest"}
    if plan_digest(unsigned) != data["plan_digest"]:
        raise ValueError("V2 plan digest mismatch")
    items = data["cases"]
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("invalid V2 case list")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != CASE_KEYS:
            raise ValueError("invalid V2 case record")
        test_id, label = item["test_id"], item["label"]
        if (not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id)
                or test_id in seen):
            raise ValueError("invalid/duplicate V2 test ID")
        if (not isinstance(label, str) or not label or len(label) > 115
                or any(ord(x) < 32 for x in label)):
            raise ValueError("invalid V2 label")
        for k in ("nnodes", "ngpu"):
            if type(item[k]) is not int or not 1 <= item[k] <= 256:
                raise ValueError("invalid V2 case topology")
        seen.add(test_id)
    return items
