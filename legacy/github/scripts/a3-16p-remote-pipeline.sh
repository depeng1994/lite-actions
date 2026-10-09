#!/usr/bin/env bash
# Dispatcher host stages exact GitHub SHA and launches the 16P model test.
set -euo pipefail
run_id="${1:?run ID}"
attempt="${2:?attempt}"
sha="${3:?sha}"
stamp="${4:?timestamp}"
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ && "$sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$stamp" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}_run-[0-9]+_attempt-[0-9]+$ ]]
dispatcher_py="$PWD/legacy/github/scripts/a3-16p-dispatch.py"
test -f "$dispatcher_py"
work=$(mktemp -d /tmp/a3_16p_run_XXXXXX)
trap 'rm -rf "$work"' EXIT
git -C "$work" init -q
git -C "$work" remote add origin https://github.com/depeng1994/torchtitan-npu.git
git -C "$work" fetch -q --depth=1 origin "$sha"
git -C "$work" checkout -q --detach FETCH_HEAD
[[ "$(git -C "$work" rev-parse HEAD)" == "$sha" ]]
cd "$work"
set +e
python3 -u "$dispatcher_py" --run-id "$run_id" > "$work/dispatch_report.log" 2>&1
rc=$?
set -e
python3 - "$work/dispatch_report.log" "$rc" <<'PY'
import json
from pathlib import Path
import sys

records=Path(sys.argv[1]).read_text(encoding="utf-8",errors="replace").splitlines()
rc=int(sys.argv[2])
reports=[line.partition("[16P_RESULT] ")[2] for line in records if line.startswith("[16P_RESULT] ")]
try:
    data=json.loads(reports[-1]) if reports else {}
except ValueError:
    data={}
print(f"RESULT_STATUS: {data.get('status','FAIL')}")
print(f"RESULT_EXIT_CODE: {rc}")
print(f"RESULT_DIR: {data.get('result_dir','unknown')}")
print(f"RESULT_HOST: {data.get('log_host','a3-3-docker-relay')}")
print(f"RESULT_LOG: {data.get('log_file','')}")
print(f"NODE_EXITS: {json.dumps(data.get('node_exit_codes',{}),separators=(',',':'))}")
if not reports:
    print("DISPATCH_FAILURE: 16P dispatcher returned no result metadata")
PY
exit "$rc"
