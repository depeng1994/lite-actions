import {type Context, type Env, HttpError} from "../router";
import {appJwt} from "./cancelPlatform";
import {array, integer, member, nullableInt, nullableString, object, REPO, RUN_ID, SHA40, string} from "../utils/validate";

export type FetchLike = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

const GH = "https://api.github.com";
const GC = "https://api.gitcode.com/api/v8";
const GH_HEADERS = {
  "Accept": "application/vnd.github+json",
  "User-Agent": "lite-actions-control-center",
  "X-GitHub-Api-Version": "2022-11-28",
};
const GATE_ACTIVE = ["PENDING", "RUNNING"] as const;
const INTENT_ACTIVE = ["INTENT_CREATED", "DISPATCHED", "RUNNING", "CANCEL_REQUESTED", "NEEDS_RECONCILIATION"] as const;
const MAINTAINER_ASSOC = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);

type GateRow = {
  repo: string;
  pr_number: number;
  pr_head_sha: string;
  generation: number;
  status: "PENDING" | "RUNNING" | "PASS" | "FAIL" | "CANCELLED" | "SUPERSEDED";
  github_run_id: string | null;
  github_run_attempt: number | null;
  check_run_id: string | null;
  status_context: string;
  required_suites_json: string;
  suite_results_json: string;
  target_url: string | null;
  summary: string | null;
  author_login: string | null;
  trigger_actor: string | null;
  approved_by: string | null;
  approval_mode: string | null;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
};

type IntentRow = {
  intent_id: string;
  repo: string;
  pr_number: number;
  pr_head_sha: string;
  generation: number;
  github_run_id: string;
  github_run_attempt: number;
  target_test_id: string;
  gitcode_repo: string;
  gitcode_workflow: string;
  gitcode_ref: string;
  gitcode_run_id: string | null;
  status: string;
  last_error: string | null;
  created_at: number;
  updated_at: number;
  expires_at: number;
};

function statusContext(env: Env): string {
  return env.GATE_STATUS_CONTEXT?.trim() || "Lite-Actions-PR-Gate";
}

function trustedOwners(env: Env): Set<string> {
  return new Set(
    (env.GATE_TRUSTED_OWNERS ?? "depeng1994")
      .split(",")
      .map(s => s.trim().toLowerCase())
      .filter(Boolean),
  );
}

function safeUrl(value: unknown, field = "target_url"): string | null {
  if (value == null || value === "") return null;
  const raw = string(value, field, 512);
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new HttpError(400, "INVALID_INPUT", field);
  }
  if (parsed.protocol !== "https:") throw new HttpError(400, "INVALID_INPUT", field);
  return parsed.toString();
}

function stringList(value: unknown, field: string): string[] {
  if (value == null) return [];
  return array(value, field, 32).map(entry => string(entry, field, 64, /^[A-Za-z0-9_.:-]+$/));
}

function suiteResults(value: unknown): Record<string, string> {
  if (value == null) return {};
  const obj = object(value, "suite_results");
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const name = string(k, "suite", 64, /^[A-Za-z0-9_.:-]+$/);
    out[name] = string(v, "suite status", 32, /^[A-Z_]+$/);
  }
  return out;
}

async function resolveGitHubAuth(env: Env, repo: string, now: number, fetchFn: FetchLike): Promise<{
  mode: "app" | "token";
  headers: HeadersInit;
} | null> {
  if (env.GITHUB_APP_ID && env.GITHUB_APP_PRIVATE_KEY && env.GITHUB_APP_INSTALLATION_ID) {
    const jwt = await appJwt(env, now);
    const resp = await fetchFn(`${GH}/app/installations/${env.GITHUB_APP_INSTALLATION_ID}/access_tokens`, {
      method: "POST",
      headers: {...GH_HEADERS, Authorization: "Bearer " + jwt, "Content-Type": "application/json"},
      body: JSON.stringify({}),
      signal: AbortSignal.timeout(8000),
    });
    if (!resp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub app token HTTP ${resp.status}`);
    const data = await resp.json<{token: string}>();
    if (!data.token) throw new HttpError(502, "INSTALLATION_TOKEN_UNAVAILABLE");
    return {mode: "app", headers: {...GH_HEADERS, Authorization: "Bearer " + data.token}};
  }
  if (env.GITHUB_GATE_TOKEN) {
    return {mode: "token", headers: {...GH_HEADERS, Authorization: "Bearer " + env.GITHUB_GATE_TOKEN}};
  }
  void repo;
  return null;
}

async function publishGateStatus(
  env: Env,
  fetchFn: FetchLike,
  now: number,
  params: {
    repo: string;
    sha: string;
    state: "pending" | "success" | "failure";
    targetUrl: string | null;
    description: string;
    existingCheckRunId?: string | null;
  },
): Promise<{published: boolean; checkRunId: string | null}> {
  const auth = await resolveGitHubAuth(env, params.repo, now, fetchFn);
  if (!auth) return {published: false, checkRunId: params.existingCheckRunId ?? null};
  const ctx = statusContext(env);
  const desc = params.description.slice(0, 140) || `PR Gate: ${params.state}`;

  if (auth.mode === "app") {
    const conclusion = params.state === "success" ? "success" : params.state === "failure" ? "failure" : undefined;
    const body = {
      name: ctx,
      head_sha: params.sha,
      status: params.state === "pending" ? "in_progress" : "completed",
      ...(conclusion ? {conclusion} : {}),
      ...(params.targetUrl ? {details_url: params.targetUrl} : {}),
      output: {title: ctx, summary: desc},
    };
    const updateExisting = Boolean(params.existingCheckRunId);
    const url = updateExisting
      ? `${GH}/repos/${params.repo}/check-runs/${params.existingCheckRunId}`
      : `${GH}/repos/${params.repo}/check-runs`;
    const resp = await fetchFn(url, {
      method: updateExisting ? "PATCH" : "POST",
      headers: {...auth.headers, "Content-Type": "application/json"},
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(8000),
    });
    if (!resp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub check-run HTTP ${resp.status}`);
    const payload = await resp.json<{id?: number | string}>();
    return {published: true, checkRunId: payload.id != null ? String(payload.id) : (params.existingCheckRunId ?? null)};
  }

  const resp = await fetchFn(`${GH}/repos/${params.repo}/statuses/${params.sha}`, {
    method: "POST",
    headers: {...auth.headers, "Content-Type": "application/json"},
    body: JSON.stringify({
      state: params.state,
      context: ctx,
      description: desc,
      ...(params.targetUrl ? {target_url: params.targetUrl} : {}),
    }),
    signal: AbortSignal.timeout(8000),
  });
  if (!resp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub commit status HTTP ${resp.status}`);
  return {published: true, checkRunId: null};
}

export async function registerPrGate(
  context: Context,
  input: Record<string, unknown>,
  fetchFn: FetchLike = fetch,
) {
  const repo = string(input.repo, "repo", 128, REPO);
  const prNumber = integer(input.pr_number, "pr_number", 1);
  const sha = string(input.pr_head_sha, "pr_head_sha", 40, SHA40);
  const initialStatus = input.status == null ? "PENDING" : member(input.status, "status", GATE_ACTIVE);
  const runId = nullableString(input.github_run_id ?? null, "github_run_id", 64, /^[0-9]{1,20}$/);
  const attempt = nullableInt(input.github_run_attempt ?? null, "github_run_attempt", 1);
  const requiredSuites = stringList(input.required_suites, "required_suites");
  const targetUrl = safeUrl(input.target_url);
  const summary = nullableString(input.summary ?? null, "summary", 256);
  const author = nullableString(input.author_login ?? null, "author_login", 64, /^[A-Za-z0-9_.\[\]-]+$/);
  const trigger = nullableString(input.trigger_actor ?? null, "trigger_actor", 64, /^[A-Za-z0-9_.\[\]-]+$/);
  const db = context.env.DB;
  const now = context.now;

  const prev = await db.prepare(`
    SELECT COALESCE(MAX(generation), 0) AS max_gen FROM pr_gate_generations
    WHERE repo=? AND pr_number=? AND pr_head_sha=?
  `).bind(repo, prNumber, sha).first<{max_gen: number}>();
  const generation = Number(prev?.max_gen ?? 0) + 1;
  const ctx = statusContext(context.env);

  await db.batch([
    db.prepare(`
      UPDATE pr_gate_generations SET status='SUPERSEDED',updated_at=?,finished_at=COALESCE(finished_at,?)
      WHERE repo=? AND pr_number=? AND status IN ('PENDING','RUNNING','PASS')
        AND (pr_head_sha<>? OR generation<?)
    `).bind(now, now, repo, prNumber, sha, generation),
    db.prepare(`
      UPDATE gitcode_dispatch_intents SET status='CANCEL_REQUESTED',updated_at=?
      WHERE repo=? AND pr_number=? AND status IN ('INTENT_CREATED','DISPATCHED','RUNNING','NEEDS_RECONCILIATION')
        AND (pr_head_sha<>? OR generation<?)
    `).bind(now, repo, prNumber, sha, generation),
    db.prepare(`
      INSERT INTO pr_gate_generations(
        repo,pr_number,pr_head_sha,generation,status,github_run_id,github_run_attempt,check_run_id,
        status_context,required_suites_json,suite_results_json,target_url,summary,author_login,
        trigger_actor,approved_by,approval_mode,created_at,updated_at,finished_at
      ) VALUES (?,?,?,?,?,?,?,NULL,?,?,'{}',?,?,?,?,NULL,NULL,?,?,NULL)
    `).bind(
      repo, prNumber, sha, generation, initialStatus, runId, attempt, ctx,
      JSON.stringify(requiredSuites), targetUrl, summary, author, trigger, now, now,
    ),
  ]);

  const pub = await publishGateStatus(context.env, fetchFn, now, {
    repo,
    sha,
    state: "pending",
    targetUrl,
    description: summary || `PR #${prNumber} gen ${generation} waiting for hardware gate`,
  });
  if (pub.checkRunId) {
    await db.prepare(`
      UPDATE pr_gate_generations SET check_run_id=?
      WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
    `).bind(pub.checkRunId, repo, prNumber, sha, generation).run();
  }
  return {
    ok: true, repo, pr_number: prNumber, pr_head_sha: sha, generation, status: initialStatus,
    status_context: ctx, status_published: pub.published,
  };
}

export async function reportPrGate(
  context: Context,
  input: Record<string, unknown>,
  fetchFn: FetchLike = fetch,
) {
  const repo = string(input.repo, "repo", 128, REPO);
  const prNumber = integer(input.pr_number, "pr_number", 1);
  const sha = string(input.pr_head_sha, "pr_head_sha", 40, SHA40);
  const nextStatus = member(input.status, "status", ["PENDING", "RUNNING", "PASS", "FAIL", "CANCELLED"] as const);
  const reqGen = nullableInt(input.generation ?? null, "generation", 1);
  const runId = nullableString(input.github_run_id ?? null, "github_run_id", 64, /^[0-9]{1,20}$/);
  const attempt = nullableInt(input.github_run_attempt ?? null, "github_run_attempt", 1);
  const suites = input.suite_results == null ? null : suiteResults(input.suite_results);
  const targetUrl = safeUrl(input.target_url);
  const summary = nullableString(input.summary ?? null, "summary", 256);
  const author = nullableString(input.author_login ?? null, "author_login", 64, /^[A-Za-z0-9_.\[\]-]+$/);
  const trigger = nullableString(input.trigger_actor ?? null, "trigger_actor", 64, /^[A-Za-z0-9_.\[\]-]+$/);
  const approvedBy = nullableString(input.approved_by ?? null, "approved_by", 64, /^[A-Za-z0-9_.\[\]-]+$/);
  const approvalMode = input.approval_mode == null
    ? null
    : member(input.approval_mode, "approval_mode", ["review", "self_env_approved"] as const);

  const db = context.env.DB;
  const now = context.now;
  const current = await db.prepare(`
    SELECT * FROM pr_gate_generations
    WHERE repo=? AND pr_number=? AND pr_head_sha=?
    ORDER BY generation DESC LIMIT 1
  `).bind(repo, prNumber, sha).first<GateRow>();
  if (!current) throw new HttpError(404, "GATE_GENERATION_NOT_FOUND");
  if (current.status === "SUPERSEDED" || (reqGen != null && reqGen !== current.generation)) {
    throw new HttpError(409, "STALE_GENERATION");
  }
  if (current.github_run_attempt != null && attempt != null && attempt < current.github_run_attempt) {
    throw new HttpError(409, "STALE_GENERATION");
  }
  const newerAttemptSameRun = current.github_run_id != null && runId === current.github_run_id
    && current.github_run_attempt != null && attempt != null && attempt > current.github_run_attempt;
  void newerAttemptSameRun;

  const terminal = nextStatus === "PASS" || nextStatus === "FAIL" || nextStatus === "CANCELLED";
  const mergedSuites = suites ?? JSON.parse(current.suite_results_json || "{}");

  await db.batch([
    db.prepare(`
      UPDATE pr_gate_generations SET
        status=?,github_run_id=COALESCE(?,github_run_id),github_run_attempt=COALESCE(?,github_run_attempt),
        suite_results_json=?,target_url=COALESCE(?,target_url),summary=COALESCE(?,summary),
        author_login=COALESCE(?,author_login),trigger_actor=COALESCE(?,trigger_actor),
        approved_by=COALESCE(?,approved_by),approval_mode=COALESCE(?,approval_mode),
        updated_at=?,finished_at=CASE WHEN ? THEN COALESCE(finished_at,?) ELSE NULL END
      WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
    `).bind(
      nextStatus, runId, attempt, JSON.stringify(mergedSuites), targetUrl, summary,
      author, trigger, approvedBy, approvalMode, now, terminal ? 1 : 0, now,
      repo, prNumber, sha, current.generation,
    ),
    ...(nextStatus === "FAIL" || nextStatus === "CANCELLED" ? [db.prepare(`
      UPDATE gitcode_dispatch_intents SET status='CANCEL_REQUESTED',updated_at=?
      WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
        AND status IN ('INTENT_CREATED','DISPATCHED','RUNNING','NEEDS_RECONCILIATION')
    `).bind(now, repo, prNumber, sha, current.generation)] : []),
  ]);

  const ghState: "pending" | "success" | "failure" =
    nextStatus === "PASS" ? "success" : (nextStatus === "FAIL" || nextStatus === "CANCELLED") ? "failure" : "pending";
  const pub = await publishGateStatus(context.env, fetchFn, now, {
    repo,
    sha,
    state: ghState,
    targetUrl: targetUrl ?? current.target_url,
    description: summary || `PR #${prNumber} gen ${current.generation}: ${nextStatus}`,
    existingCheckRunId: current.check_run_id,
  });
  if (pub.checkRunId && !current.check_run_id) {
    await db.prepare(`
      UPDATE pr_gate_generations SET check_run_id=?
      WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
    `).bind(pub.checkRunId, repo, prNumber, sha, current.generation).run();
  }
  return {
    ok: true, repo, pr_number: prNumber, pr_head_sha: sha,
    generation: current.generation, status: nextStatus, status_published: pub.published,
  };
}

export async function createGitcodeIntent(context: Context, input: Record<string, unknown>) {
  const repo = string(input.repo, "repo", 128, REPO);
  const prNumber = integer(input.pr_number, "pr_number", 1);
  const sha = string(input.pr_head_sha, "pr_head_sha", 40, SHA40);
  const githubRunId = string(input.github_run_id, "github_run_id", 64, /^[0-9]{1,20}$/);
  const githubAttempt = integer(input.github_run_attempt ?? 1, "github_run_attempt", 1);
  const targetTestId = string(input.target_test_id, "target_test_id", 64, /^id[0-9]{1,20}(?:a[1-9][0-9]{0,5})?$/);
  const gitcodeRepo = string(input.gitcode_repo, "gitcode_repo", 128, REPO);
  const gitcodeWorkflow = string(input.gitcode_workflow ?? "dispatcher_smoke.yml", "gitcode_workflow", 128, /^[A-Za-z0-9_.-]{1,128}$/);
  const gitcodeRef = string(input.gitcode_ref ?? "master", "gitcode_ref", 128, /^[A-Za-z0-9_./-]{1,128}$/);
  const ttl = integer(input.ttl_seconds ?? 14400, "ttl_seconds", 60, 28800);
  const reqGen = nullableInt(input.generation ?? null, "generation", 1);

  const db = context.env.DB;
  const gate = await db.prepare(`
    SELECT generation,status FROM pr_gate_generations
    WHERE repo=? AND pr_number=? AND pr_head_sha=?
    ORDER BY generation DESC LIMIT 1
  `).bind(repo, prNumber, sha).first<{generation: number; status: string}>();
  if (!gate) throw new HttpError(404, "GATE_GENERATION_NOT_FOUND");
  if (reqGen != null && reqGen !== gate.generation) throw new HttpError(409, "STALE_GENERATION");
  if (!GATE_ACTIVE.includes(gate.status as (typeof GATE_ACTIVE)[number])) {
    throw new HttpError(409, "GATE_NOT_ACTIVE");
  }

  const intentId = "gci_" + crypto.randomUUID();
  const now = context.now;
  await db.prepare(`
    INSERT INTO gitcode_dispatch_intents(
      intent_id,repo,pr_number,pr_head_sha,generation,github_run_id,github_run_attempt,
      target_test_id,gitcode_repo,gitcode_workflow,gitcode_ref,gitcode_run_id,status,
      last_error,created_at,updated_at,expires_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'INTENT_CREATED',NULL,?,?,?)
  `).bind(
    intentId, repo, prNumber, sha, gate.generation, githubRunId, githubAttempt,
    targetTestId, gitcodeRepo, gitcodeWorkflow, gitcodeRef, now, now, now + ttl,
  ).run();
  return {ok: true, intent_id: intentId, generation: gate.generation, status: "INTENT_CREATED"};
}

export async function updateGitcodeIntent(context: Context, intentId: string, input: Record<string, unknown>) {
  const id = string(intentId, "intent_id", 64, /^[A-Za-z0-9_-]{1,64}$/);
  const status = member(input.status, "status", [
    "DISPATCHED", "RUNNING", "COMPLETED", "CANCEL_REQUESTED", "CANCELLED", "NEEDS_RECONCILIATION", "EXPIRED", "FAILED",
  ] as const);
  const gitcodeRunId = nullableString(input.gitcode_run_id ?? null, "gitcode_run_id", 128, RUN_ID);
  const lastError = nullableString(input.last_error ?? null, "last_error", 256);
  const db = context.env.DB;
  const existing = await db.prepare("SELECT status FROM gitcode_dispatch_intents WHERE intent_id=?")
    .bind(id).first<{status: string}>();
  if (!existing) throw new HttpError(404, "INTENT_NOT_FOUND");
  await db.prepare(`
    UPDATE gitcode_dispatch_intents SET
      status=CASE WHEN status='CANCEL_REQUESTED' AND ? IN ('DISPATCHED','RUNNING') THEN 'CANCEL_REQUESTED' ELSE ? END,
      gitcode_run_id=COALESCE(?,gitcode_run_id),
      last_error=?,
      updated_at=?
    WHERE intent_id=?
  `).bind(status, status, gitcodeRunId, lastError, context.now, id).run();
  return {ok: true, intent_id: id, status};
}

export async function getPrGateStatus(context: Context) {
  const repo = context.url.searchParams.get("repo");
  const prParam = context.url.searchParams.get("pr_number");
  const shaParam = context.url.searchParams.get("sha");
  const validRepo = string(repo, "repo", 128, REPO);
  const clauses = ["repo=?"];
  const params: unknown[] = [validRepo];
  if (prParam != null) {
    clauses.push("pr_number=?");
    params.push(integer(Number(prParam), "pr_number", 1));
  }
  if (shaParam != null) {
    clauses.push("pr_head_sha=?");
    params.push(string(shaParam, "sha", 40, SHA40));
  }
  const where = clauses.join(" AND ");
  const db = context.env.DB;
  const runs = await db.prepare(`SELECT * FROM pr_gate_generations WHERE ${where} ORDER BY updated_at DESC, generation DESC LIMIT 20`)
    .bind(...params).all<GateRow>();
  const intents = await db.prepare(`SELECT * FROM gitcode_dispatch_intents WHERE ${where} ORDER BY created_at DESC LIMIT 50`)
    .bind(...params).all<IntentRow>();
  return {generations: runs.results, intents: intents.results};
}

function matchGitCodeRun(
  runs: Array<Record<string, unknown>>,
  intent: IntentRow,
): Array<{id: string}> {
  const matched: Array<{id: string}> = [];
  for (const run of runs) {
    const id = run.workflow_run_id ?? run.id;
    if (id == null) continue;
    const status = String(run.status ?? "").toLowerCase();
    if (["completed", "cancelled", "canceled", "stopped", "failed", "success", "succeeded"].includes(status)) continue;
    const wf = String(run.file_path ?? run.workflow_file ?? run.path ?? run.workflow_name ?? "");
    const ref = String(run.head_branch ?? run.ref ?? run.branch ?? "");
    if (wf && !wf.endsWith(intent.gitcode_workflow)) continue;
    if (ref && ref !== intent.gitcode_ref) continue;
    const title = String(run.title ?? run.display_title ?? run.name ?? run.run_name ?? "");
    const inputs = JSON.stringify(run.inputs ?? {});
    const targetHit = title.includes(intent.target_test_id) || inputs.includes(intent.target_test_id);
    const createdRaw = run.start_time ?? run.created_at;
    let createdAt: number | null = null;
    if (typeof createdRaw === "number") {
      createdAt = createdRaw > 1_000_000_000_000 ? Math.floor(createdRaw / 1000) : createdRaw;
    } else if (typeof createdRaw === "string" && createdRaw) {
      const parsed = Date.parse(createdRaw);
      if (Number.isFinite(parsed)) createdAt = Math.floor(parsed / 1000);
    }
    const windowOk = createdAt == null || (createdAt >= intent.created_at - 120 && createdAt <= intent.created_at + 1800);
    if (targetHit || windowOk) {
      matched.push({id: String(id)});
    }
  }
  return matched;
}

export async function reconcilePrGateIntents(context: Context, fetchFn: FetchLike = fetch) {
  const db = context.env.DB;
  const now = context.now;
  const {results: intents} = await db.prepare(`
    SELECT * FROM gitcode_dispatch_intents
    WHERE status IN (${INTENT_ACTIVE.map(() => "?").join(",")})
    ORDER BY created_at ASC LIMIT 50
  `).bind(...INTENT_ACTIVE).all<IntentRow>();
  if (!intents.length) return {ok: true, checked: 0, cancelled: 0, needs_reconciliation: 0, expired: 0};

  const prCache = new Map<string, {state: string; headSha: string} | null>();
  let cancelled = 0;
  let needsReconciliation = 0;
  let expired = 0;
  const gcToken = context.env.GITCODE_GATE_TOKEN || context.env.GITCODE_CANCEL_TOKEN;

  for (const intent of intents) {
    const parent = await db.prepare(`
      SELECT * FROM pr_gate_generations WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
    `).bind(intent.repo, intent.pr_number, intent.pr_head_sha, intent.generation).first<GateRow>();

    let shouldCancel = intent.status === "CANCEL_REQUESTED"
      || !parent
      || ["SUPERSEDED", "FAIL", "CANCELLED"].includes(parent.status)
      || now > intent.expires_at;

    const prKey = `${intent.repo}#${intent.pr_number}`;
    if (!shouldCancel) {
      if (!prCache.has(prKey)) {
        const ghAuth = await resolveGitHubAuth(context.env, intent.repo, now, fetchFn).catch(() => null);
        if (ghAuth) {
          const prResp = await fetchFn(`${GH}/repos/${intent.repo}/pulls/${intent.pr_number}`, {
            headers: ghAuth.headers, signal: AbortSignal.timeout(8000),
          }).catch(() => null);
          if (prResp?.ok) {
            const prJson = await prResp.json<{state?: string; head?: {sha?: string}}>();
            prCache.set(prKey, {state: String(prJson.state || ""), headSha: String(prJson.head?.sha || "")});
          } else {
            prCache.set(prKey, null);
          }
        } else {
          prCache.set(prKey, null);
        }
      }
      const livePr = prCache.get(prKey);
      if (livePr && (livePr.state !== "open" || livePr.headSha !== intent.pr_head_sha)) {
        shouldCancel = true;
        if (parent && GATE_ACTIVE.includes(parent.status as (typeof GATE_ACTIVE)[number])) {
          const nextGateState = livePr.state !== "open" ? "CANCELLED" : "SUPERSEDED";
          await db.prepare(`
            UPDATE pr_gate_generations SET status=?,updated_at=?,finished_at=COALESCE(finished_at,?)
            WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
          `).bind(nextGateState, now, now, intent.repo, intent.pr_number, intent.pr_head_sha, intent.generation).run();
          if (nextGateState === "CANCELLED") {
            await publishGateStatus(context.env, fetchFn, now, {
              repo: intent.repo,
              sha: intent.pr_head_sha,
              state: "failure",
              targetUrl: parent.target_url,
              description: `PR #${intent.pr_number} closed before completion`,
              existingCheckRunId: parent.check_run_id,
            }).catch(() => undefined);
          }
        }
      }
    }

    if (!shouldCancel) continue;

    let runId = intent.gitcode_run_id;
    if (!runId && gcToken) {
      const listResp = await fetchFn(
        `${GC}/repos/${intent.gitcode_repo}/actions/runs?per_page=20`,
        {headers: {"PRIVATE-TOKEN": gcToken, Accept: "application/json"}, signal: AbortSignal.timeout(8000)},
      ).catch(() => null);
      if (listResp?.ok) {
        const listData = await listResp.json<{workflow_runs?: Array<Record<string, unknown>>; runs?: Array<Record<string, unknown>>}>();
        const candidates = listData.workflow_runs ?? listData.runs ?? [];
        const matches = matchGitCodeRun(candidates, intent);
        if (matches.length === 1) {
          runId = matches[0].id;
        } else {
          const nextStatus = now > intent.expires_at ? "EXPIRED" : "NEEDS_RECONCILIATION";
          await db.prepare(`
            UPDATE gitcode_dispatch_intents SET status=?,last_error=?,updated_at=? WHERE intent_id=?
          `).bind(
            nextStatus,
            matches.length === 0 ? "GITCODE_RUN_NOT_FOUND" : `AMBIGUOUS_GITCODE_RUNS:${matches.length}`,
            now,
            intent.intent_id,
          ).run();
          if (nextStatus === "EXPIRED") expired += 1;
          else needsReconciliation += 1;
          continue;
        }
      } else {
        const nextStatus = now > intent.expires_at ? "EXPIRED" : "CANCEL_REQUESTED";
        await db.prepare(`
          UPDATE gitcode_dispatch_intents SET status=?,last_error=?,updated_at=? WHERE intent_id=?
        `).bind(nextStatus, "GITCODE_DISCOVERY_UNAVAILABLE", now, intent.intent_id).run();
        if (nextStatus === "EXPIRED") expired += 1;
        continue;
      }
    }

    if (!runId) {
      const nextStatus = now > intent.expires_at ? "EXPIRED" : "NEEDS_RECONCILIATION";
      await db.prepare(`
        UPDATE gitcode_dispatch_intents SET status=?,last_error=?,updated_at=? WHERE intent_id=?
      `).bind(nextStatus, "GITCODE_RUN_ID_UNKNOWN", now, intent.intent_id).run();
      if (nextStatus === "EXPIRED") expired += 1;
      else needsReconciliation += 1;
      continue;
    }

    if (gcToken) {
      const stopResp = await fetchFn(
        `${GC}/repos/${intent.gitcode_repo}/actions/runs/${encodeURIComponent(runId)}/stop`,
        {method: "POST", headers: {"PRIVATE-TOKEN": gcToken, Accept: "application/json"}, signal: AbortSignal.timeout(8000)},
      ).catch(() => null);
      if (stopResp && (stopResp.ok || stopResp.status === 404 || stopResp.status === 409)) {
        await db.prepare(`
          UPDATE gitcode_dispatch_intents SET status='CANCELLED',gitcode_run_id=?,last_error=NULL,updated_at=?
          WHERE intent_id=?
        `).bind(runId, now, intent.intent_id).run();
        cancelled += 1;
      } else {
        await db.prepare(`
          UPDATE gitcode_dispatch_intents SET status='CANCEL_REQUESTED',gitcode_run_id=?,last_error=?,updated_at=?
          WHERE intent_id=?
        `).bind(runId, `GITCODE_STOP_HTTP_${stopResp?.status ?? "ERR"}`, now, intent.intent_id).run();
      }
    } else {
      await db.prepare(`
        UPDATE gitcode_dispatch_intents SET status='NEEDS_RECONCILIATION',gitcode_run_id=?,last_error=?,updated_at=?
        WHERE intent_id=?
      `).bind(runId, "GITCODE_TOKEN_NOT_CONFIGURED", now, intent.intent_id).run();
      needsReconciliation += 1;
    }
  }
  return {ok: true, checked: intents.length, cancelled, needs_reconciliation: needsReconciliation, expired};
}

export async function mergePrWithGate(
  context: Context,
  input: Record<string, unknown>,
  fetchFn: FetchLike = fetch,
) {
  const repo = string(input.repo, "repo", 128, REPO);
  const prNumber = integer(input.pr_number, "pr_number", 1);
  const sha = string(input.pr_head_sha, "pr_head_sha", 40, SHA40);
  const reqGen = nullableInt(input.generation ?? null, "generation", 1);
  const commitTitle = nullableString(input.commit_title ?? null, "commit_title", 200);
  const commitMessage = nullableString(input.commit_message ?? null, "commit_message", 1024);

  const auth = await resolveGitHubAuth(context.env, repo, context.now, fetchFn);
  if (!auth) throw new HttpError(503, "GITHUB_GATE_TOKEN_REQUIRED");

  const prResp = await fetchFn(`${GH}/repos/${repo}/pulls/${prNumber}`, {
    headers: auth.headers, signal: AbortSignal.timeout(8000),
  });
  if (!prResp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub pull lookup HTTP ${prResp.status}`);
  const pr = await prResp.json<{state?: string; head?: {sha?: string}; user?: {login?: string}}>();
  if (pr.state !== "open") throw new HttpError(409, "PR_NOT_OPEN");
  if (pr.head?.sha !== sha) throw new HttpError(409, "PR_HEAD_MISMATCH");

  const db = context.env.DB;
  const gate = await db.prepare(`
    SELECT * FROM pr_gate_generations
    WHERE repo=? AND pr_number=? AND pr_head_sha=?
    ORDER BY generation DESC LIMIT 1
  `).bind(repo, prNumber, sha).first<GateRow>();
  if (!gate) throw new HttpError(409, "PR_NOT_VERIFIED_FOR_SHA");
  if (gate.status === "SUPERSEDED" || (reqGen != null && reqGen !== gate.generation)) {
    throw new HttpError(409, "STALE_GENERATION");
  }
  if (gate.status !== "PASS") throw new HttpError(409, "GATE_NOT_PASSED");

  if (gate.github_run_id) {
    const runResp = await fetchFn(`${GH}/repos/${repo}/actions/runs/${gate.github_run_id}`, {
      headers: auth.headers, signal: AbortSignal.timeout(8000),
    });
    if (!runResp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub run lookup HTTP ${runResp.status}`);
    const runInfo = await runResp.json<{status?: string; conclusion?: string | null; run_attempt?: number}>();
    if (runInfo.run_attempt != null && gate.github_run_attempt != null && runInfo.run_attempt > gate.github_run_attempt) {
      throw new HttpError(409, "STALE_GENERATION");
    }
    const statusLower = String(runInfo.status ?? "").toLowerCase();
    const conclusionLower = String(runInfo.conclusion ?? "").toLowerCase();
    if (
      ["cancelled", "failure", "timed_out", "action_required"].includes(conclusionLower)
      || (statusLower === "completed" && conclusionLower !== "success")
    ) {
      await db.prepare(`
        UPDATE pr_gate_generations SET status='CANCELLED',updated_at=?,finished_at=COALESCE(finished_at,?)
        WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
      `).bind(context.now, context.now, repo, prNumber, sha, gate.generation).run();
      await publishGateStatus(context.env, fetchFn, context.now, {
        repo,
        sha,
        state: "failure",
        targetUrl: gate.target_url,
        description: `PR #${prNumber} run cancelled/failed before merge`,
        existingCheckRunId: gate.check_run_id,
      }).catch(() => undefined);
      throw new HttpError(409, "AUTH_REVOKED_OR_RUN_CANCELLED");
    }
  }

  const reviewsResp = await fetchFn(`${GH}/repos/${repo}/pulls/${prNumber}/reviews?per_page=100`, {
    headers: auth.headers, signal: AbortSignal.timeout(8000),
  });
  if (!reviewsResp.ok) throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub reviews lookup HTTP ${reviewsResp.status}`);
  const reviews = await reviewsResp.json<Array<{
    user?: {login?: string};
    state?: string;
    commit_id?: string;
    author_association?: string;
  }>>();
  const prAuthor = String(pr.user?.login || "").toLowerCase();
  const latestByReviewer = new Map<string, {state: string; commitId: string; assoc: string}>();
  for (const rev of reviews) {
    const login = String(rev.user?.login || "").toLowerCase();
    if (!login || login === prAuthor) continue;
    latestByReviewer.set(login, {
      state: String(rev.state || ""),
      commitId: String(rev.commit_id || ""),
      assoc: String(rev.author_association || ""),
    });
  }
  const hasValidShaReview = [...latestByReviewer.values()].some(
    rev => MAINTAINER_ASSOC.has(rev.assoc) && rev.state === "APPROVED" && rev.commitId === sha,
  );
  const selfPr = trustedOwners(context.env).has(prAuthor);
  const selfEnvApproved = selfPr && gate.approval_mode === "self_env_approved";
  if (!hasValidShaReview && !selfEnvApproved) {
    await db.prepare(`
      UPDATE pr_gate_generations SET status='CANCELLED',updated_at=?,finished_at=COALESCE(finished_at,?)
      WHERE repo=? AND pr_number=? AND pr_head_sha=? AND generation=?
    `).bind(context.now, context.now, repo, prNumber, sha, gate.generation).run();
    await publishGateStatus(context.env, fetchFn, context.now, {
      repo,
      sha,
      state: "failure",
      targetUrl: gate.target_url,
      description: `PR #${prNumber} authorization missing or revoked`,
      existingCheckRunId: gate.check_run_id,
    }).catch(() => undefined);
    throw new HttpError(403, "AUTH_REVOKED_OR_RUN_CANCELLED");
  }

  const mergeResp = await fetchFn(`${GH}/repos/${repo}/pulls/${prNumber}/merge`, {
    method: "PUT",
    headers: {...auth.headers, "Content-Type": "application/json"},
    body: JSON.stringify({
      sha,
      merge_method: "merge",
      ...(commitTitle ? {commit_title: commitTitle} : {}),
      ...(commitMessage ? {commit_message: commitMessage} : {}),
    }),
    signal: AbortSignal.timeout(8000),
  });
  if (!mergeResp.ok) {
    const detail = await mergeResp.text().catch(() => "");
    if (mergeResp.status === 405 || mergeResp.status === 409) {
      throw new HttpError(409, "MERGE_REJECTED", detail.slice(0, 200) || `HTTP ${mergeResp.status}`);
    }
    throw new HttpError(502, "PLATFORM_HTTP_ERROR", `GitHub merge HTTP ${mergeResp.status}`);
  }
  const merged = await mergeResp.json<{merged?: boolean; sha?: string; message?: string}>();
  if (!merged.merged) throw new HttpError(409, "MERGE_REJECTED", merged.message || "PR was not merged");
  return {ok: true, merged: true, sha: merged.sha ?? sha, pr_number: prNumber, generation: gate.generation};
}
