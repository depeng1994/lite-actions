"""Identity-bound, byte-budgeted result outbox and metadata-only progress."""

import json
from itertools import islice

from ci_core.contract import REPO
from ci_core.logs import clean, sanitized_lines, yellow_lines


def marker(run, item):
    dispatch = f":{run['_ci_dispatch_id']}" if run.get("_ci_dispatch_id") else ""
    return f"Lite-CI-V3 {run['id']}:{run.get('run_attempt', 1)}{dispatch}:{item['case_id']}"


def report(run, item, plan_digest, status, rc, lines, cleanup_verified=True):
    result = dict(
        schema=3,
        repo=run.get("_ci_repo", REPO),
        workflow=run["path"].rsplit("/", 1)[-1],
        run_id=run["id"],
        attempt=run.get("run_attempt", 1),
        sha=run["head_sha"],
        plan_digest=plan_digest,
        case_id=item["case_id"],
        test_id=item["test_id"],
        worker=item["worker"],
        target_pool=item["target_pool"],
        docker=item["docker"],
        resources=item["resources"],
        status=status,
        exit_code=rc,
        cleanup_verified=cleanup_verified,
        last_20_lines=[clean(x) for x in islice(sanitized_lines(lines), 20)],
    )
    if run.get("_ci_dispatch_id"):
        result["dispatch_id"] = run["_ci_dispatch_id"]
    if run.get("_ci_source"):
        result["native_source"] = run["_ci_source"]
    return result


def body_for(run, item, result, *, local_worker=None):
    result = {**result, "last_20_lines": list(result["last_20_lines"])}
    if local_worker is not None and result["worker"] != local_worker:
        raise ValueError("STATE_OWNER_MISMATCH")
    if local_worker == "yellow":
        result["last_20_lines"] = yellow_lines(result["last_20_lines"])
        if "native_source" in result:
            from ci_core.contract import digest

            result["source_digest"] = digest(result.pop("native_source"))
    while True:
        body = (
            marker(run, item)
            + "\n"
            + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        request = json.dumps({"body": body}, separators=(",", ":"))
        if len(body.encode()) <= 6100 and len(request.encode()) <= 7000:
            return body
        if result.get("diagnostics"):
            result["diagnostics"] = result["diagnostics"][:-1]
            result["inline_truncated"] = True
            continue
        if not result["last_20_lines"]:
            raise ValueError("metadata exceeds upload budget")
        result["last_20_lines"].pop()


def post(
    client,
    run,
    item,
    result,
    author,
    *,
    local_worker=None,
    cache=None,
    verify_only=False,
):
    body = body_for(run, item, result, local_worker=local_worker)
    prefix = marker(run, item) + "\n"
    cache = {} if cache is None else cache
    key = (id(client), run["head_sha"])
    if key not in cache:
        cache[key] = client.commit_comments(run["head_sha"])
    prior = [
        c
        for c in cache[key]
        if c.get("user", {}).get("login") == author
        and c.get("body", "").startswith(prefix)
    ]
    if prior:
        if any(c["body"] != body for c in prior):
            raise ValueError("conflicting existing result")
        return
    if verify_only:
        raise ValueError("UNCONFIRMED_MANUAL_RECOVERY")
    client.send_result(run["head_sha"], body)


def progress_body(run, item, digest, phase):
    return "Lite-CI-PROGRESS " + json.dumps(
        dict(
            run_id=run["id"],
            attempt=run.get("run_attempt", 1),
            case_id=item["case_id"],
            worker=item["worker"],
            plan_digest=digest,
            phase=phase,
            **(
                {"dispatch_id": run["_ci_dispatch_id"]}
                if run.get("_ci_dispatch_id")
                else {}
            ),
        ),
        separators=(",", ":"),
    )


def progress(client, run, item, digest, phase, comment_id=None):
    body = progress_body(run, item, digest, phase)
    if comment_id:
        client.request("PATCH", f"/comments/{comment_id}", {"body": body})
        return comment_id
    return client.send_result(run["head_sha"], body)["id"]


def case_passed(log, test_id):
    if not log.is_file():
        return False
    events = [
        line
        for line in log.read_text(errors="replace").splitlines()
        if line.startswith("CASE_RESULT " + test_id + " ")
    ]
    return events == [f"CASE_RESULT {test_id} PASS 0"]
