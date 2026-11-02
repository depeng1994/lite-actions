"""Trusted SDK entrypoint for the thin personal GitCode workflow wrapper."""

from __future__ import annotations

import os

from ci_core.gitcode_manual import aggregate, capture, dispatch
from ci_core.gitcode_manual.common import ManualForwardError, emit_failure


def main() -> int:
    try:
        phase = os.environ.get("LITE_GITCODE_MANUAL_PHASE", "")
        if phase == "capture":
            return capture.main()
        if phase == "aggregate":
            return aggregate.main()
        if phase in {"forward", "result"}:
            if phase == "result" and not os.environ.get("RESULT_CASE"):
                raise ManualForwardError("RESULT_CASE_REQUIRED")
            if phase == "forward" and os.environ.get("RESULT_CASE"):
                raise ManualForwardError("RESULT_PHASE_MISMATCH")
            return dispatch.main()
        raise ManualForwardError("MANUAL_PHASE_NOT_ALLOWED")
    except Exception as error:
        return emit_failure(error)


if __name__ == "__main__":
    raise SystemExit(main())
