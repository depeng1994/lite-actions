"""Inspect and launch the Lite Actions repository's Smoke cases."""

import argparse
import json
import os
from pathlib import Path

from tests.integration_tests.training_cases.dispatch_smoke import (
    SMOKE_DEFAULTS,
    build_smoke_list,
    resolve_smoke_profile,
)


def inspect_definition(case):
    if case.ci_kind == "worker_probe":
        overrides = {"worker": SMOKE_DEFAULTS["worker"], **case.ci_overrides}
    elif case.ci_kind == "npu_smoke":
        overrides = {**SMOKE_DEFAULTS, **case.ci_overrides}
    else:
        raise ValueError("unknown Smoke case kind")
    return {
        "test_id": case.test_name,
        "label": case.test_descr,
        "nnodes": case.nnodes,
        "ngpu": case.ngpu,
        "timeout": case.timeout,
        "kind": case.ci_kind,
        "ci_overrides": overrides,
        "env_vars": dict(case.env_vars or {}),
    }


def inspect_profile(profile="dispatch_smoke"):
    return [inspect_definition(case) for case in resolve_smoke_profile(profile)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("inspect", "launch", "verify"))
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--test-id")
    selection.add_argument("--suite")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.suite:
        if args.phase != "inspect":
            parser.error("launch/verify require a test ID")
        print(json.dumps(inspect_profile(args.suite)))
        return
    cases = [case for case in build_smoke_list() if case.test_name == args.test_id]
    if len(cases) != 1:
        parser.error("unknown or ambiguous Smoke test")
    case = cases[0]
    if args.phase == "inspect":
        print(json.dumps([inspect_definition(case)]))
    elif case.ci_kind != "npu_smoke" or args.output_dir is None:
        parser.error("NPU Smoke and output directory are required")
    elif args.phase == "launch":
        for name, value in (case.env_vars or {}).items():
            os.environ.setdefault(name, value)
        from tests.integration_tests.tools.lite_actions.runner import run_dispatch_smoke

        run_dispatch_smoke(case)
    # The Worker requires every node's launch to exit successfully first.


if __name__ == "__main__":
    main()
