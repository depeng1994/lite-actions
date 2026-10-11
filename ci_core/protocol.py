"""Validate SHA-bound GitHub CI Matrix plans without trusting arbitrary commands.

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
from ci_core.source_repo import REPO
CASE_KEYS = {"test_id", "nnodes", "ngpu", "label"}
PLAN_KEYS = {"schema", "repo", "run_id", "attempt", "sha", "pipeline", "cases",
             "plan_digest"}


def plan_digest(plan: dict) -> str:
    canonical = json.dumps(plan, separators=(",", ":"), sort_keys=True,
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_plan(data: dict, run: dict, pipeline: str) -> list[dict]:
    if not isinstance(data, dict) or set(data) != PLAN_KEYS:
        raise ValueError("invalid CI plan structure")
    if (data["schema"] != 2 or data["repo"] != REPO
            or data["run_id"] != run["id"]
            or data["attempt"] != run.get("run_attempt", 1)
            or data["sha"] != run["head_sha"] or data["pipeline"] != pipeline
            or not SHA.fullmatch(str(data["sha"]))):
        raise ValueError("CI plan identity mismatch")
    if (not isinstance(data["plan_digest"], str)
            or not DIGEST.fullmatch(data["plan_digest"])):
        raise ValueError("invalid CI plan digest")
    unsigned = {k: v for k, v in data.items() if k != "plan_digest"}
    if plan_digest(unsigned) != data["plan_digest"]:
        raise ValueError("CI plan digest mismatch")
    items = data["cases"]
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("invalid CI case list")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != CASE_KEYS:
            raise ValueError("invalid CI case record")
        test_id, label = item["test_id"], item["label"]
        if (not isinstance(test_id, str) or not SELECTOR.fullmatch(test_id)
                or test_id in seen):
            raise ValueError("invalid/duplicate CI test ID")
        if (not isinstance(label, str) or not label or len(label) > 115
                or any(ord(x) < 32 for x in label)):
            raise ValueError("invalid CI label")
        for k in ("nnodes", "ngpu"):
            if type(item[k]) is not int or not 1 <= item[k] <= 256:
                raise ValueError("invalid CI case topology")
        seen.add(test_id)
    return items
