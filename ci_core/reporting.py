"""Identity-bound, byte-budgeted result outbox and metadata-only progress."""

import json

from ci_core.contract import REPO
from ci_core.logs import clean


def marker(run, item):
    return f"Lite-CI-V3 {run['id']}:{run.get('run_attempt', 1)}:{item['case_id']}"


def report(run, item, plan_digest, status, rc, lines, cleanup_verified=True):
    return dict(
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
        last_20_lines=[clean(x) for x in lines[:20]],
    )


def body_for(run, item, result):
    result = {**result, "last_20_lines": list(result["last_20_lines"])}
    while True:
        body = (
            marker(run, item)
            + "\n"
            + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        request = json.dumps({"body": body}, separators=(",", ":"))
        if len(body.encode()) <= 6100 and len(request.encode()) <= 7000:
            return body
        if not result["last_20_lines"]:
            raise ValueError("metadata exceeds upload budget")
        result["last_20_lines"].pop()


def post(client, run, item, result, author):
    body = body_for(run, item, result)
    prefix = marker(run, item) + "\n"
    prior = [
        c
        for c in client.commit_comments(run["head_sha"])
        if c.get("user", {}).get("login") == author
        and c.get("body", "").startswith(prefix)
    ]
    if prior:
        if any(c["body"] != body for c in prior):
            raise ValueError("conflicting existing result")
        return
    client.send_result(run["head_sha"], body)


def progress(client, run, item, digest, phase, comment_id=None):
    body = "Lite-CI-PROGRESS " + json.dumps(
        dict(
            run_id=run["id"],
            attempt=run.get("run_attempt", 1),
            case_id=item["case_id"],
            worker=item["worker"],
            plan_digest=digest,
            phase=phase,
        ),
        separators=(",", ":"),
    )
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
