import {beforeAll, expect, test} from "vitest";
import {
  createGitcodeIntent, mergePrWithGate, reconcilePrGateIntents, registerPrGate, reportPrGate,
  type FetchLike,
} from "../src/services/prGate";
import type {Context, Env} from "../src/router";
import {hmac} from "../src/utils/crypto";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";

const gateToken = "gate-coord-secret-token-01";
const config: Env = {...bindings, GITHUB_GATE_TOKEN: "gh-gate-tok", GITCODE_GATE_TOKEN: "gc-gate-tok"};
const now = Math.floor(Date.now() / 1000);
const shaA = "a".repeat(40);
const shaB = "b".repeat(40);
const shaOld = "0".repeat(40);

beforeAll(async () => {
  await migrate();
  config.GATE_COORD_KEY_HASH = await hmac(bindings.KEY_PEPPER!, gateToken);
});

function ctxFor(pathname = "/api/v1/pr-gate/merge", search = "", envOverride: Partial<Env> = {}): Context {
  const url = new URL(`https://example.test${pathname}${search}`);
  return {
    request: new Request(url, {method: "POST"}),
    env: {...config, ...envOverride},
    params: {},
    url,
    now,
    clientIp: "192.0.2.1",
    gateAuthorized: true,
  };
}

function gateReq(path: string, data?: unknown, bearer: string | null = gateToken, cookie?: string) {
  const headers: Record<string, string> = {};
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  return request("/pr-gate" + path, {data, cookie, headers, now}, config);
}

test("pr-gate enforces auth, single serial status updates, stale generation blocking, and superseding", async () => {
  expect((await gateReq("/register", {repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaA}, null)).status).toBe(401);
  expect((await gateReq("/register", {repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaA}, "wrong-token-value-123456")).status).toBe(401);

  const cookie = sessionCookie(await signIn());
  expect((await gateReq("/status?repo=depeng1994%2Flite-actions&pr_number=10", undefined, null, cookie)).status).toBe(200);

  const statuses: Array<{state: string; context: string}> = [];
  const fetchMock: FetchLike = async (_url, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    statuses.push({state: body.state, context: body.context});
    return new Response("{}", {status: 201});
  };

  const r1 = await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaA,
    github_run_id: "1001", github_run_attempt: 1, author_login: "depeng1994",
  }, fetchMock);
  expect(r1).toMatchObject({ok: true, generation: 1, status: "PENDING", status_published: true});

  await createGitcodeIntent(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaA, generation: 1,
    github_run_id: "1001", github_run_attempt: 1, target_test_id: "id1001a1",
    gitcode_repo: "depeng1994/torchtitan-npu",
  });

  // Re-registering SHA B supersedes gen 1 on SHA A and requests cancel of its intent.
  const r2 = await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaB,
    github_run_id: "1002", github_run_attempt: 1, author_login: "depeng1994",
  }, fetchMock);
  expect(r2).toMatchObject({generation: 1, status: "PENDING"});
  expect(await db.prepare("SELECT status FROM pr_gate_generations WHERE repo='depeng1994/lite-actions' AND pr_number=10 AND pr_head_sha=?")
    .bind(shaA).first("status")).toBe("SUPERSEDED");
  expect(await db.prepare("SELECT status FROM gitcode_dispatch_intents WHERE pr_head_sha=?")
    .bind(shaA).first("status")).toBe("CANCEL_REQUESTED");

  // Reporting against superseded shaA or wrong generation is rejected cleanly.
  await expect(reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaA, status: "PASS",
  }, fetchMock)).rejects.toMatchObject({status: 409, code: "STALE_GENERATION"});

  await reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaB, status: "PASS",
    github_run_id: "1002", github_run_attempt: 2, approval_mode: "self_env_approved",
    suite_results: {self_smoke: "PASS", torchtitan_smoke: "PASS"},
  }, fetchMock);

  // Older attempt on active generation must be rejected as STALE_GENERATION.
  await expect(reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 10, pr_head_sha: shaB, status: "FAIL",
    github_run_id: "1002", github_run_attempt: 1,
  }, fetchMock)).rejects.toMatchObject({status: 409, code: "STALE_GENERATION"});

  expect(statuses.every(s => ["pending", "success", "failure"].includes(s.state))).toBe(true);
  expect(statuses.at(-1)).toEqual({state: "success", context: "Lite-Actions-PR-Gate"});
});

test("gitcode intent reconciler discovers unambiguous runs to cancel and marks ambiguity NEEDS_RECONCILIATION", async () => {
  await db.batch([
    db.prepare("DELETE FROM gitcode_dispatch_intents"),
    db.prepare("DELETE FROM pr_gate_generations"),
  ]);

  const mkGate = async (pr: number, sha: string, testId: string, gcRunId: string | null = null) => {
    await db.prepare(`INSERT INTO pr_gate_generations(repo,pr_number,pr_head_sha,generation,status,status_context,created_at,updated_at)
      VALUES('depeng1994/lite-actions',?,?,1,'FAIL','Lite-Actions-PR-Gate',?,?)`).bind(pr, sha, now - 30, now - 10).run();
    const res = await db.prepare(`INSERT INTO gitcode_dispatch_intents(
      intent_id,repo,pr_number,pr_head_sha,generation,github_run_id,github_run_attempt,target_test_id,
      gitcode_repo,gitcode_workflow,gitcode_ref,gitcode_run_id,status,last_error,created_at,updated_at,expires_at
    ) VALUES(?,'depeng1994/lite-actions',?,?,1,?,1,?,'depeng1994/torchtitan-npu','dispatcher_smoke.yml','master',?,'CANCEL_REQUESTED',NULL,?,?,?)`)
      .bind(`gci_${pr}`, pr, sha, String(2000 + pr), testId, gcRunId, now - 30, now - 10, now + 3600).run();
    void res;
  };

  await mkGate(31, shaA, "id2031a1");
  await mkGate(32, shaA, "id2032a1");
  await mkGate(33, shaA, "id2033a1");

  const stopped: string[] = [];
  const fetchMock: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.includes("/stop") && init?.method === "POST") {
      stopped.push(href);
      return new Response("{}", {status: 200});
    }
    if (href.includes("/actions/runs?per_page=20")) {
      return Response.json({
        workflow_runs: [
          // Matched for pr 31 (exactly 1 active match by target_test_id/title window)
          {id: 7701, workflow_file: ".gitcode/workflows/dispatcher_smoke.yml", ref: "master", status: "running", display_title: "Smoke id2031a1", created_at: now - 20},
          // Two ambiguous runs inside window for pr 32 when neither carries unique test id -> matchGitCodeRun filters window
          {id: 7702, workflow_file: ".gitcode/workflows/dispatcher_smoke.yml", ref: "master", status: "running", display_title: "Smoke generic", created_at: now - 20},
        ],
      });
    }
    return new Response("{}", {status: 200});
  };

  // Isolate ambiguous test by making 32 & 33 inspect tailored workflow_runs responses:
  const perPrFetch: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.includes("/stop")) {
      stopped.push(href);
      return new Response("{}", {status: 200});
    }
    // Simulate only pr 31 having unique id match: in practice pr 31 sees only run 7701; pr 32 sees two matching runs; pr 33 sees zero runs.
    return fetchMock(url, init);
  };
  void perPrFetch;

  let callIdx = 0;
  const seqFetch: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.includes("/stop")) {
      stopped.push(href);
      return new Response("{}", {status: 200});
    }
    callIdx += 1;
    if (callIdx === 1) {
      return Response.json({workflow_runs: [
        {workflow_run_id: "gc_run_31", workflow_file: ".gitcode/workflows/dispatcher_smoke.yml", ref: "master", status: "running", created_at: now - 15},
      ]});
    }
    if (callIdx === 2) {
      return Response.json({workflow_runs: [
        {workflow_run_id: "gc_a", workflow_file: ".gitcode/workflows/dispatcher_smoke.yml", ref: "master", status: "running", created_at: now - 15},
        {workflow_run_id: "gc_b", workflow_file: ".gitcode/workflows/dispatcher_smoke.yml", ref: "master", status: "running", created_at: now - 10},
      ]});
    }
    return Response.json({workflow_runs: []});
  };

  const res = await reconcilePrGateIntents(ctxFor(), seqFetch);
  expect(res).toMatchObject({ok: true, cancelled: 1, needs_reconciliation: 2});
  expect(stopped).toEqual(["https://api.gitcode.com/api/v8/repos/depeng1994/torchtitan-npu/actions/runs/gc_run_31/stop"]);
  expect(await db.prepare("SELECT status,gitcode_run_id FROM gitcode_dispatch_intents WHERE intent_id='gci_31'").first())
    .toEqual({status: "CANCELLED", gitcode_run_id: "gc_run_31"});
  expect(await db.prepare("SELECT status,last_error FROM gitcode_dispatch_intents WHERE intent_id='gci_32'").first())
    .toMatchObject({status: "NEEDS_RECONCILIATION", last_error: "AMBIGUOUS_GITCODE_RUNS:2"});
  expect(await db.prepare("SELECT status,last_error FROM gitcode_dispatch_intents WHERE intent_id='gci_33'").first())
    .toMatchObject({status: "NEEDS_RECONCILIATION", last_error: "GITCODE_RUN_NOT_FOUND"});
});

test("merge coordinator enforces SHA binding, same-SHA PR isolation, and live approval/run state", async () => {
  await db.batch([
    db.prepare("DELETE FROM gitcode_dispatch_intents"),
    db.prepare("DELETE FROM pr_gate_generations"),
  ]);

  const noOpFetch: FetchLike = async () => new Response("{}", {status: 201});
  await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA,
    github_run_id: "5001", github_run_attempt: 1, author_login: "contributor",
  }, noOpFetch);
  await reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA, status: "PASS",
    github_run_id: "5001", github_run_attempt: 1, approved_by: "depeng1994", approval_mode: "review",
  }, noOpFetch);

  // Same SHA on unverified PR #51 MUST be rejected with PR_NOT_VERIFIED_FOR_SHA.
  const sameShaOtherPrFetch: FetchLike = async url => {
    if (String(url).endsWith("/pulls/51")) return Response.json({state: "open", head: {sha: shaA}, user: {login: "contributor"}});
    return new Response("{}", {status: 200});
  };
  await expect(mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 51, pr_head_sha: shaA,
  }, sameShaOtherPrFetch)).rejects.toMatchObject({status: 409, code: "PR_NOT_VERIFIED_FOR_SHA"});

  // PR head moved concurrently -> PR_HEAD_MISMATCH.
  const movedHeadFetch: FetchLike = async url => {
    if (String(url).endsWith("/pulls/50")) return Response.json({state: "open", head: {sha: shaB}, user: {login: "contributor"}});
    return new Response("{}", {status: 200});
  };
  await expect(mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA,
  }, movedHeadFetch)).rejects.toMatchObject({status: 409, code: "PR_HEAD_MISMATCH"});

  // External PR review dismissed OR bound to old SHA before Check updated -> AUTH_REVOKED_OR_RUN_CANCELLED.
  const publishedFailure: string[] = [];
  const dismissedReviewFetch: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.endsWith("/pulls/50")) return Response.json({state: "open", head: {sha: shaA}, user: {login: "contributor"}});
    if (href.endsWith("/actions/runs/5001")) return Response.json({status: "completed", conclusion: "success", run_attempt: 1});
    if (href.includes("/pulls/50/reviews")) {
      return Response.json([
        {user: {login: "depeng1994"}, state: "APPROVED", commit_id: shaA, author_association: "OWNER"},
        {user: {login: "depeng1994"}, state: "DISMISSED", commit_id: shaA, author_association: "OWNER"},
        {user: {login: "maintainer2"}, state: "APPROVED", commit_id: shaOld, author_association: "COLLABORATOR"},
      ]);
    }
    if (href.includes("/statuses/")) {
      publishedFailure.push(JSON.parse(String(init?.body)).state);
      return new Response("{}", {status: 201});
    }
    return new Response("{}", {status: 200});
  };
  await expect(mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA,
  }, dismissedReviewFetch)).rejects.toMatchObject({status: 403, code: "AUTH_REVOKED_OR_RUN_CANCELLED"});
  expect(publishedFailure).toEqual(["failure"]);

  // Happy path for maintainer-approved external PR (re-report PASS first since revocation transitioned to CANCELLED).
  await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA,
    github_run_id: "5002", github_run_attempt: 1, author_login: "contributor",
  }, noOpFetch);
  await reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA, status: "PASS",
    github_run_id: "5002", github_run_attempt: 1,
  }, noOpFetch);

  const mergeBodies: Array<Record<string, unknown>> = [];
  const happyExternalFetch: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.endsWith("/pulls/50")) return Response.json({state: "open", head: {sha: shaA}, user: {login: "contributor"}});
    if (href.endsWith("/actions/runs/5002")) return Response.json({status: "completed", conclusion: "success", run_attempt: 1});
    if (href.includes("/pulls/50/reviews")) {
      return Response.json([{user: {login: "depeng1994"}, state: "APPROVED", commit_id: shaA, author_association: "OWNER"}]);
    }
    if (href.endsWith("/pulls/50/merge") && init?.method === "PUT") {
      mergeBodies.push(JSON.parse(String(init.body)));
      return Response.json({merged: true, sha: "f".repeat(40)});
    }
    return new Response("{}", {status: 200});
  };
  const mergedExt = await mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 50, pr_head_sha: shaA, generation: 2,
  }, happyExternalFetch);
  expect(mergedExt).toEqual({ok: true, merged: true, sha: "f".repeat(40), pr_number: 50, generation: 2});
  expect(mergeBodies[0]).toMatchObject({sha: shaA, merge_method: "merge"});

  // Cancelled GitHub run caught synchronously even while DB still says PASS.
  await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB,
    github_run_id: "5201", github_run_attempt: 1, author_login: "depeng1994",
  }, noOpFetch);
  await reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB, status: "PASS",
    github_run_id: "5201", github_run_attempt: 1, approval_mode: "self_env_approved",
  }, noOpFetch);

  const cancelledRunFetch: FetchLike = async url => {
    const href = String(url);
    if (href.endsWith("/pulls/52")) return Response.json({state: "open", head: {sha: shaB}, user: {login: "depeng1994"}});
    if (href.endsWith("/actions/runs/5201")) return Response.json({status: "completed", conclusion: "cancelled", run_attempt: 1});
    if (href.includes("/statuses/")) return new Response("{}", {status: 201});
    return new Response("{}", {status: 200});
  };
  await expect(mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB,
  }, cancelledRunFetch)).rejects.toMatchObject({status: 409, code: "AUTH_REVOKED_OR_RUN_CANCELLED"});

  // Happy path for depeng1994 self-PR via protected environment approval binding.
  await registerPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB,
    github_run_id: "5202", github_run_attempt: 1, author_login: "depeng1994",
  }, noOpFetch);
  await reportPrGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB, status: "PASS",
    github_run_id: "5202", github_run_attempt: 1, approval_mode: "self_env_approved",
  }, noOpFetch);
  const happySelfFetch: FetchLike = async (url, init) => {
    const href = String(url);
    if (href.endsWith("/pulls/52")) return Response.json({state: "open", head: {sha: shaB}, user: {login: "depeng1994"}});
    if (href.endsWith("/actions/runs/5202")) return Response.json({status: "completed", conclusion: "success", run_attempt: 1});
    if (href.includes("/pulls/52/reviews")) return Response.json([]);
    if (href.endsWith("/pulls/52/merge") && init?.method === "PUT") {
      return Response.json({merged: true, sha: "e".repeat(40)});
    }
    return new Response("{}", {status: 200});
  };
  const mergedSelf = await mergePrWithGate(ctxFor(), {
    repo: "depeng1994/lite-actions", pr_number: 52, pr_head_sha: shaB, generation: 2,
  }, happySelfFetch);
  expect(mergedSelf).toEqual({ok: true, merged: true, sha: "e".repeat(40), pr_number: 52, generation: 2});
});
