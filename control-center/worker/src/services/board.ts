import {installation, supportsCancel} from "./cancelPlatform";
import {modelMap} from "../modelMap";
import {type Context, HttpError} from "../router";
import type {SqlStatement} from "../storage/sqlStore";
import type {CaseKind, DeviceGroup, ModelRepo, ModelTab, Phase, Projection, Provider, TaskDetail, TaskSummary, Timeline, WorkflowList, WorkflowRow, WorkerId} from "../types/api";
import {pepper} from "./auth";
import {aggregateHealth, type GroupRow, offline, overlaps, type Pools, projectDevice, type RawAllocation, type WorkerRow, workerStatus} from "./deviceState";
import {refreshWorkflowRuns, type WorkflowIdentity as IngestWorkflowIdentity} from "./ingest";
import {durationKey, estimate, estimates, projectSchedule, type ProjectionRow, type QueueTask} from "./schedule";
import {base64url} from "../utils/crypto";
import {blobBuffer, gunzip} from "../utils/gzip";
import {nodeIdentity} from "../utils/mask";
import {runUrl} from "../utils/taskIdentity";
import {POOL, REPO} from "../utils/validate";

type TaskRow = Pick<TaskSummary, "task_uid" | "worker_id" | "provider" | "repo" | "logical_model" | "run_id" |
  "attempt" | "dispatch_id" | "run_number" | "display_title" | "case_id" | "test_id" | "label" | "kind" | "target_pool" | "req_nodes" | "req_devices" |
  "pr_number" | "pr_author" | "trigger_actor" | "event_type" | "workflow_file"> & {
  phase?: Phase; status?: string; queued_at?: number; finished_at?: number;
  reserved_at?: number; staging_at?: number; running_at?: number; cleaning_at?: number; started_at?: number;
  exit_code?: number; cleanup_verified?: number; duration_sec?: number; timing_reliable: number;
  allocation_json?: string; actual_allocation_json?: string; storage_tier?: "hot" | "cold"; cold_chunk_id?: string;
  queue_pos?: number; submitted_at?: number; workflow_name?: string;
};
type QueueRow = {
  worker_id: WorkerId; pool_name: string; kind: CaseKind; task_uid: string; queue_pos: number;
  candidate_group_idxs_json: string;
};
type Revision = {revision: number; config_json: string};
const ZONES: WorkerId[] = ["blue", "yellow"];
const ACTIVE_SELECT = `SELECT a.*, (SELECT queue_pos FROM queue_orders q WHERE q.task_uid=a.task_uid
  AND q.worker_id=a.worker_id AND q.pool_name=a.target_pool AND q.kind=a.kind) AS queue_pos FROM tasks_active a`;

function allocation(row: TaskRow): RawAllocation[] {
  return JSON.parse(row.allocation_json ?? row.actual_allocation_json ?? "[]");
}
function canCancel(context: Context, provider: Provider, repo: string): boolean {
  return supportsCancel(context.env, provider, repo);
}

async function summaries(context: Context, rows: TaskRow[]): Promise<TaskSummary[]> {
  const samples = await estimates(context.env.DB, rows);
  const nodes = new Map<string, ReturnType<typeof nodeIdentity>>();
  const node = (host: string) => {
    let result = nodes.get(host);
    if (!result) {
      result = nodeIdentity(pepper(context), host, Boolean(context.principal));
      nodes.set(host, result);
    }
    return result;
  };
  return Promise.all(rows.map(async row => {
    const {task_uid, worker_id, provider, repo, logical_model, run_id, attempt, dispatch_id, case_id, test_id, label,
      kind, target_pool, req_nodes, req_devices, pr_number, pr_author, trigger_actor, event_type, workflow_file} = row;
    const shape = {logical_model, test_id, target_pool, req_nodes, req_devices};
    const allocations = await Promise.all(allocation(row).map(async alloc => ({...await node(alloc.host_id), devices: alloc.devices})));
    return {
      task_uid, worker_id, provider, repo, run_id, attempt, dispatch_id, run_number: row.run_number ?? null,
      display_title: row.display_title ?? null, case_id, label, kind,
      pr_number, pr_author, trigger_actor, event_type, workflow_file, ...shape,
      phase: row.phase ?? null, status: row.status ?? null, queued_at: row.queued_at ?? null,
      finished_at: row.finished_at ?? null, queue_pos: row.queue_pos ?? null,
      duration_sec: row.duration_sec ?? null,
      submitted_at: row.submitted_at ?? row.queued_at ?? null,
      workflow_name: row.workflow_name ?? (workflow_file?.split("/").at(-1)?.replace(/\.ya?ml$/, "") ?? null),
      started_at: row.started_at ?? row.running_at ?? row.reserved_at ?? null,
      allocated_cards: allocations.reduce((sum, item) => sum + item.devices.length, 0),
      storage_tier: row.storage_tier ?? null,
      allocation: allocations,
      ...estimate(samples.get(durationKey(shape)) ?? [], context.now, row.running_at ?? row.reserved_at),
      can_move_up: row.phase === "QUEUED" && kind !== "worker_probe" && (row.queue_pos ?? 0) > 0,
      cancel_supported: Boolean(row.phase) && canCancel(context, provider, repo), run_url: runUrl(row),
    };
  }));
}

export async function overview(context: Context) {
  const results = await context.env.DB.batch([
    context.env.DB.prepare("SELECT * FROM workers"),
    context.env.DB.prepare("SELECT COALESCE(MAX(revision),0) AS revision FROM pools_configs"),
  ]);
  const latest = Number((results[1].results?.[0] as {revision: number} | undefined)?.revision ?? 0);
  const health = (results[0].results ?? []) as unknown as WorkerRow[];
  const workers = ZONES.map(zone => workerStatus(context.now, zone, latest, health.find(row => row.worker_id === zone)));
  return {
    workers, page_refresh_sec: 30 as const,
    active_count: workers.reduce((count, worker) => count + worker.active_count, 0),
    queued_count: workers.reduce((count, worker) => count + worker.queued_count, 0),
  };
}
export async function models(context: Context) {
  const {results} = await context.env.DB.prepare(`
    SELECT logical_model,provider,repo,count(*) AS active_count FROM tasks_active GROUP BY logical_model,provider,repo
    UNION ALL SELECT DISTINCT logical_model,provider,repo,0 FROM workflow_runs
  `).all<{logical_model: string; provider: Provider; repo: string; active_count: number}>();
  const tabs = new Map<string, ModelTab>(modelMap.models.map(model => [model.slug, {
    slug: model.slug, display_name: model.display_name,
    default: model.slug === modelMap.default_model, active_count: 0,
    repos: model.repos.map(repo => ({provider: repo.provider as Provider, repo: repo.repo,
      repo_scope: repo.repo_scope as ModelRepo["repo_scope"],
      cancel_supported: canCancel(context, repo.provider as Provider, repo.repo)})),
  }]));
  for (const row of results) {
    const {logical_model: slug, provider, repo, active_count} = row;
    if (!tabs.has(slug)) tabs.set(slug, {slug, display_name: slug, default: false, active_count: 0, repos: []});
    const tab = tabs.get(slug)!;
    tab.active_count += active_count;
    if (!tab.repos.some(item => item.provider === provider && item.repo === repo)) {
      tab.repos.push({provider, repo, cancel_supported: canCancel(context, provider, repo)});
    }
  }
  if (context.principal && context.env.GITHUB_APP_ID) {
    const repos = [...tabs.values()].flatMap(tab => tab.repos).filter(repo => repo.provider === "github");
    await Promise.allSettled(repos.slice(0, 25).map(row => installation(context.env, row.repo, context.now)));
    for (const row of repos) row.cancel_supported = canCancel(context, row.provider, row.repo);
  }
  return {default_model: modelMap.default_model, models: [...tabs.values()]};
}

type WorkflowRunRecord = {
  provider: Provider; repo: string; run_id: string; attempt: number;
  logical_model: string; submitted_at: number | null; workflow_name: string | null;
  run_number: number | null; display_title: string | null; workflow_file: string | null;
  pr_number: number | null; pr_author: string | null; trigger_actor: string | null;
  event_type: string | null; is_active: number; has_in_flight: number;
  has_waiting_resource_case: number; active_case_count: number; total_case_count: number;
  queue_order_rank: number | null; first_started_at: number | null; finished_at: number | null;
  result_state: "QUEUED" | "PASS" | "FAIL"; pools_json: string; max_cards: number;
  card_seconds_finished: number; has_unmeasured_allocated: number;
};

function workflowIdentity(row: {provider: Provider; repo: string; run_id: string; attempt: number}) {
  return JSON.stringify([row.provider, row.repo, row.run_id, row.attempt]);
}

function workflowScope(provider: Provider, repo: string): ModelRepo["repo_scope"] {
  for (const model of modelMap.models) {
    const found = model.repos.find(entry => entry.provider === provider && entry.repo.toLowerCase() === repo.toLowerCase());
    if (found?.repo_scope) return found.repo_scope as ModelRepo["repo_scope"];
  }
  return undefined;
}

function buildWorkflowRow(
  run: WorkflowRunRecord,
  cases: TaskSummary[],
  sequence: number,
  now: number,
  reliableMap: Map<string, boolean>,
): WorkflowRow {
  const history = run.is_active === 0;
  const inFlight = run.has_in_flight === 1;
  const allocated = cases.filter(item => item.allocated_cards > 0);
  const measured = allocated.filter(item =>
    item.duration_sec != null ||
    (item.started_at != null && (item.finished_at != null || (item.phase != null && item.phase !== "QUEUED")) && reliableMap.get(item.task_uid)),
  );
  const cardSeconds = measured.reduce((sum, item) => {
    const sec = item.duration_sec ?? Math.max(0, (item.finished_at ?? now) - (item.started_at ?? now));
    return sum + item.allocated_cards * Math.max(0, sec);
  }, 0);
  const pools = [...new Set(cases.map(item => item.target_pool).filter(Boolean))];
  const fallbackPools = (JSON.parse(run.pools_json || "[]") as string[]).filter(Boolean);
  const maxCards = cases.length
    ? Math.max(0, ...cases.map(item => item.allocated_cards || item.req_nodes * item.req_devices))
    : run.max_cards;
  const starts = cases.map(item => item.started_at).filter((v): v is number => v != null);
  return {
    provider: run.provider, repo: run.repo, run_id: run.run_id, attempt: run.attempt,
    sequence,
    submitted_at: run.submitted_at,
    run_number: run.run_number ?? null,
    display_title: run.display_title ?? null,
    repo_scope: workflowScope(run.provider, run.repo),
    logical_model: run.logical_model,
    workflow_name: run.workflow_name ?? run.workflow_file?.split("/").at(-1)?.replace(/\.ya?ml$/, "") ?? null,
    workflow_file: run.workflow_file,
    pools: pools.length ? pools : fallbackPools,
    max_cards: maxCards,
    result: run.result_state,
    trigger_actor: run.trigger_actor,
    event_type: run.event_type,
    pr_number: run.pr_number,
    pr_author: run.pr_author,
    started_at: starts.length ? Math.min(...starts) : run.first_started_at,
    finished_at: history ? run.finished_at : null,
    card_hours: measured.length && cardSeconds > 0 ? Number((cardSeconds / 3600).toFixed(2)) : null,
    card_hours_as_of_now: !history && inFlight && measured.length > 0 && cardSeconds > 0,
    card_hours_partial: measured.length > 0 && measured.length < allocated.length,
    cancel_supported: !history && cases.some(item => item.cancel_supported),
    can_move_up: !history && sequence >= 2,
    cases,
  };
}

function parseWorkflowCursor(cursor: string | null) {
  if (!cursor) return null;
  try {
    const decoded = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(cursor.replaceAll("-", "+").replaceAll("_", "/")), c => c.charCodeAt(0))));
    if (!Array.isArray(decoded) || decoded.length !== 5) throw new Error();
    const [finished, provider, repo, runId, attempt] = decoded as [number, string, string, string, number];
    if (!Number.isSafeInteger(finished) || typeof provider !== "string" || typeof repo !== "string" ||
      typeof runId !== "string" || !Number.isSafeInteger(attempt)) throw new Error();
    return {finished, provider, repo, runId, attempt};
  } catch {
    throw new HttpError(400, "INVALID_CURSOR");
  }
}

export async function workflows(context: Context): Promise<WorkflowList> {
  const legacyLimit = context.url.searchParams.get("limit");
  const requestedHistory = context.url.searchParams.get("history_limit");
  const historyLimit = Number(requestedHistory ?? legacyLimit ?? 0);
  if (!Number.isSafeInteger(historyLimit) || historyLimit < 0 || historyLimit > 50) throw new HttpError(400, "INVALID_LIMIT");
  const model = context.url.searchParams.get("model");
  const repo = context.url.searchParams.get("repo");
  const platform = context.url.searchParams.get("platform");
  const pool = context.url.searchParams.get("pool");
  const status = context.url.searchParams.get("status");
  if (model && !/^[A-Za-z0-9_.-]{1,128}$/.test(model)) throw new HttpError(400, "INVALID_MODEL");
  if (repo && !REPO.test(repo)) throw new HttpError(400, "INVALID_REPO");
  if (platform && platform !== "github" && platform !== "gitcode") throw new HttpError(400, "INVALID_PLATFORM");
  if (pool && !POOL.test(pool)) throw new HttpError(400, "INVALID_POOL");
  if (status && !["active", "history"].includes(status)) throw new HttpError(400, "INVALID_STATUS");
  const cursor = parseWorkflowCursor(context.url.searchParams.get("cursor"));
  const {DB: db} = context.env;
  const showActive = !status || status === "active";
  const showHistory = (!status || status === "history") && historyLimit > 0;

  // Backfill only active tasks missing from workflow_runs (e.g. direct SQL test
  // inserts). Never scan task_index on this path.
  const missingActive = await db.prepare(`
    SELECT DISTINCT a.provider,a.repo,a.run_id,a.attempt FROM tasks_active a
    WHERE NOT EXISTS (
      SELECT 1 FROM workflow_runs w
      WHERE w.provider=a.provider AND w.repo=a.repo AND w.run_id=a.run_id AND w.attempt=a.attempt
    )
  `).all<IngestWorkflowIdentity>();
  if (missingActive.results.length) {
    await db.batch(refreshWorkflowRuns(db, missingActive.results, context.now));
  }

  const filterClauses: string[] = [];
  const filterParams: unknown[] = [];
  if (model) { filterClauses.push("logical_model=?"); filterParams.push(model); }
  if (repo) { filterClauses.push("repo=?"); filterParams.push(repo); }
  if (platform) { filterClauses.push("provider=?"); filterParams.push(platform); }
  if (pool) { filterClauses.push("EXISTS (SELECT 1 FROM json_each(pools_json) WHERE value=?)"); filterParams.push(pool); }
  const filterSql = filterClauses.length ? " AND " + filterClauses.join(" AND ") : "";

  const metaStatements: SqlStatement[] = [
    db.prepare(`SELECT MAX(
      COALESCE((SELECT queue_version FROM workflow_queue_meta WHERE id=1), 0),
      COALESCE((SELECT MAX(queue_ver) FROM queue_orders), 0)
    ) AS version`),
    db.prepare("SELECT meta_value FROM system_meta WHERE meta_key='capacity_purged_before_ts'"),
  ];
  if (showActive) {
    metaStatements.push(db.prepare(`
      SELECT * FROM workflow_runs WHERE is_active=1
      ORDER BY has_in_flight DESC,
        CASE WHEN queue_order_rank IS NULL THEN 1 ELSE 0 END,
        queue_order_rank ASC,
        COALESCE(submitted_at, 9223372036854775807) ASC,
        provider ASC, repo ASC, run_id ASC, attempt ASC
    `));
  }
  const cursorSql = cursor
    ? ` AND (COALESCE(finished_at,0)<? OR (COALESCE(finished_at,0)=? AND (
        provider>? OR (provider=? AND (repo>? OR (repo=? AND (run_id>? OR (run_id=? AND attempt>?)))))
      )))`
    : "";
  const cursorParams = cursor
    ? [cursor.finished, cursor.finished, cursor.provider, cursor.provider, cursor.repo, cursor.repo, cursor.runId, cursor.runId, cursor.attempt]
    : [];
  if (showHistory) {
    metaStatements.push(
      db.prepare(`
        SELECT * FROM workflow_runs WHERE is_active=0${filterSql}${cursorSql}
        ORDER BY finished_at DESC, provider ASC, repo ASC, run_id ASC, attempt ASC
        LIMIT ?
      `).bind(...filterParams, ...cursorParams, historyLimit + 1),
      db.prepare(`SELECT COUNT(*) AS count FROM workflow_runs WHERE is_active=0${filterSql}`).bind(...filterParams),
    );
  }
  const metaResults = await db.batch(metaStatements);
  let idx = 0;
  const queueVersion = Number((metaResults[idx++].results?.[0] as {version?: number} | undefined)?.version ?? 0);
  const purged = Boolean((metaResults[idx++].results?.[0] as {meta_value?: string} | undefined)?.meta_value);
  const allActiveRuns = showActive ? ((metaResults[idx++].results ?? []) as unknown as WorkflowRunRecord[]) : [];
  const rawHistoryRuns = showHistory ? ((metaResults[idx++].results ?? []) as unknown as WorkflowRunRecord[]) : [];
  const historyTotal = showHistory ? Number((metaResults[idx++].results?.[0] as {count?: number} | undefined)?.count ?? 0) : 0;

  // Assign global waiting sequence before filtering so filters never renumber.
  let waitingSeq = 0;
  const activeWithSeq = allActiveRuns.map(run => {
    const seq = run.has_in_flight === 1 || run.has_waiting_resource_case === 0 ? 0 : ++waitingSeq;
    return {run, sequence: seq};
  }).filter(({run}) => {
    const pools = JSON.parse(run.pools_json || "[]") as string[];
    return (!model || run.logical_model === model) && (!repo || run.repo === repo) &&
      (!platform || run.provider === platform) && (!pool || pools.includes(pool));
  });

  const hasMore = showHistory && rawHistoryRuns.length > historyLimit;
  const pageHistoryRuns = showHistory ? rawHistoryRuns.slice(0, historyLimit) : [];

  // Load case rows only for the visible workflows on this response.
  const activeRunIdentities = activeWithSeq.map(({run}) => ({provider: run.provider, repo: run.repo, run_id: run.run_id, attempt: run.attempt}));
  const mixedActiveIdentities = activeWithSeq
    .filter(({run}) => run.total_case_count > run.active_case_count)
    .map(({run}) => ({provider: run.provider, repo: run.repo, run_id: run.run_id, attempt: run.attempt}));
  const historyRunIdentities = pageHistoryRuns.map(run => ({provider: run.provider, repo: run.repo, run_id: run.run_id, attempt: run.attempt}));

  const detailStatements: SqlStatement[] = [];
  const activeCasesIdx = activeRunIdentities.length ? detailStatements.push(db.prepare(`
    SELECT a.*, (SELECT queue_pos FROM queue_orders q WHERE q.task_uid=a.task_uid
      AND q.worker_id=a.worker_id AND q.pool_name=a.target_pool AND q.kind=a.kind) AS queue_pos
    FROM tasks_active a JOIN json_each(?) k
      ON a.provider=json_extract(k.value,'$.provider') AND a.repo=json_extract(k.value,'$.repo')
      AND a.run_id=json_extract(k.value,'$.run_id') AND a.attempt=json_extract(k.value,'$.attempt')
    ORDER BY a.queued_at,a.task_uid
  `).bind(JSON.stringify(activeRunIdentities))) - 1 : -1;

  const indexTargets = [...mixedActiveIdentities, ...historyRunIdentities];
  const indexCasesIdx = indexTargets.length ? detailStatements.push(db.prepare(`
    SELECT i.* FROM task_index i JOIN json_each(?) k
      ON i.provider=json_extract(k.value,'$.provider') AND i.repo=json_extract(k.value,'$.repo')
      AND i.run_id=json_extract(k.value,'$.run_id') AND i.attempt=json_extract(k.value,'$.attempt')
    ORDER BY i.finished_at DESC, i.task_uid
  `).bind(JSON.stringify(indexTargets))) - 1 : -1;

  const historyRanksIdx = historyRunIdentities.length ? detailStatements.push(db.prepare(`
    SELECT r.provider,r.repo,r.run_id,r.attempt,
      1 + (SELECT COUNT(*) FROM workflow_runs w WHERE w.is_active=0 AND (
        COALESCE(w.finished_at,0)>COALESCE(r.finished_at,0) OR (
          COALESCE(w.finished_at,0)=COALESCE(r.finished_at,0) AND (
            w.provider<r.provider OR (w.provider=r.provider AND (
              w.repo<r.repo OR (w.repo=r.repo AND (
                w.run_id<r.run_id OR (w.run_id=r.run_id AND w.attempt<r.attempt)
              ))
            ))
          )
        )
      )) AS rank
    FROM workflow_runs r JOIN json_each(?) k
      ON r.provider=json_extract(k.value,'$.provider') AND r.repo=json_extract(k.value,'$.repo')
      AND r.run_id=json_extract(k.value,'$.run_id') AND r.attempt=json_extract(k.value,'$.attempt')
  `).bind(JSON.stringify(historyRunIdentities))) - 1 : -1;

  const detailResults = detailStatements.length ? await db.batch(detailStatements) : [];
  const rawActiveCases = activeCasesIdx >= 0 ? ((detailResults[activeCasesIdx].results ?? []) as unknown as TaskRow[]) : [];
  const rawIndexCases = indexCasesIdx >= 0 ? ((detailResults[indexCasesIdx].results ?? []) as unknown as TaskRow[]) : [];
  const rawRanks = historyRanksIdx >= 0
    ? ((detailResults[historyRanksIdx].results ?? []) as unknown as Array<WorkflowRunRecord & {rank: number}>)
    : [];

  const allRawCases = [...rawActiveCases, ...rawIndexCases];
  const reliableMap = new Map(allRawCases.map(row => [row.task_uid, row.timing_reliable === 1]));
  const allSummaries = allRawCases.length ? await summaries(context, allRawCases) : [];
  const casesByWorkflow = new Map<string, TaskSummary[]>();
  for (const item of allSummaries) {
    const key = workflowIdentity(item);
    (casesByWorkflow.get(key) ?? (casesByWorkflow.set(key, []), casesByWorkflow.get(key)!)).push(item);
  }
  const rankByWorkflow = new Map(rawRanks.map(row => [workflowIdentity(row), -Number(row.rank)]));

  const activeRows = activeWithSeq.map(({run, sequence}) =>
    buildWorkflowRow(run, casesByWorkflow.get(workflowIdentity(run)) ?? [], sequence, context.now, reliableMap));
  const historyRows = pageHistoryRuns.map(run =>
    buildWorkflowRow(run, casesByWorkflow.get(workflowIdentity(run)) ?? [], rankByWorkflow.get(workflowIdentity(run)) ?? -1, context.now, reliableMap));

  const last = historyRows.at(-1);
  const next_cursor = hasMore && last ? base64url(new TextEncoder().encode(JSON.stringify([
    last.finished_at ?? 0, last.provider, last.repo, last.run_id, last.attempt,
  ]))) : null;

  return {
    queue_version: queueVersion,
    active: showActive ? activeRows : [],
    history: showHistory ? historyRows : [],
    history_total: showHistory ? historyTotal : 0,
    next_cursor,
    has_more: hasMore,
    truncated_by_capacity: purged,
    purged_notice: purged ? "更早的记录已因容量清理" : null,
  };
}

export async function taskDetail(context: Context): Promise<TaskDetail> {
  const {DB: db} = context.env;
  const uid = context.params.taskUid;
  let row = await db.prepare(ACTIVE_SELECT + " WHERE a.task_uid=?").bind(uid).first<TaskRow>();
  if (!row) {
    row = await db.prepare("SELECT * FROM task_index WHERE task_uid=?").bind(uid).first<TaskRow>();
    if (!row) throw new HttpError(404, "TASK_NOT_FOUND");
    let detail;
    if (row.storage_tier === "hot") {
      const hot = await db.prepare("SELECT detail_json FROM task_details_hot WHERE task_uid=?").bind(uid).first<string>("detail_json");
      detail = hot ? JSON.parse(hot) : {};
    } else {
      const blob = await db.prepare("SELECT gzip_blob FROM task_details_cold WHERE chunk_id=?")
        .bind(row.cold_chunk_id).first<ArrayBuffer | ArrayBufferView | number[]>("gzip_blob");
      if (!blob) throw new HttpError(503, "TASK_DETAIL_UNAVAILABLE");
      detail = JSON.parse(await gunzip(blobBuffer(blob)))[uid];
      if (!detail) throw new HttpError(503, "TASK_DETAIL_UNAVAILABLE");
    }
    for (const field of ["queued_at","reserved_at","staging_at","running_at","cleaning_at"] as const) {
      if (typeof detail[field] === "number") row[field] = detail[field];
    }
  }
  const [summary] = await summaries(context, [row]);
  return {...summary, reserved_at: row.reserved_at ?? null, staging_at: row.staging_at ?? null,
    running_at: row.running_at ?? null, cleaning_at: row.cleaning_at ?? null, duration_sec: row.duration_sec ?? null,
    exit_code: row.exit_code ?? null, timing_reliable: row.timing_reliable === 1,
    cleanup_verified: row.cleanup_verified == null ? null : row.cleanup_verified === 1};
}

export async function timeline(context: Context): Promise<Timeline> {
  const {DB: db} = context.env;
  const requestedHistoryHours = context.url.searchParams.get("history_hours");
  const historyHours = Number(requestedHistoryHours ?? 0);
  if (!Number.isSafeInteger(historyHours) || historyHours < 0 || historyHours > 72) {
    throw new HttpError(400, "INVALID_HISTORY_HOURS");
  }
  const statements = [
    db.prepare("SELECT * FROM workers"),
    db.prepare(`SELECT revision,config_json FROM pools_configs WHERE revision IN
      (SELECT MAX(revision) FROM pools_configs UNION SELECT MIN(revision) FROM pools_configs UNION SELECT applied_rev FROM workers)
      ORDER BY revision`),
    db.prepare(ACTIVE_SELECT + " ORDER BY a.task_uid"),
    db.prepare("SELECT * FROM device_group_states"),
    db.prepare("SELECT * FROM queue_orders ORDER BY worker_id,pool_name,kind,queue_pos,task_uid"),
    db.prepare("SELECT worker_id,scope,pool_name,host_id,devices_json FROM cluster_drain_targets"),
  ];
  if (historyHours > 0) {
    statements.push(db.prepare(`SELECT * FROM task_index
      WHERE finished_at >= ? AND finished_at <= ?
      ORDER BY finished_at DESC, task_uid DESC
      LIMIT 5000`).bind(context.now - historyHours * 3600, context.now));
  }
  const results = await db.batch(statements);
  const [health, versions, tasks, observations, queueRows, drains] = results.slice(0, 6).map(result => result.results ?? []) as unknown as
    [WorkerRow[], Revision[], TaskRow[], GroupRow[], QueueRow[], Array<{worker_id: WorkerId; scope: string; pool_name: string; host_id: string | null; devices_json: string | null}>];
  const historyTasks = historyHours > 0 ? (results[6]?.results ?? []) as unknown as TaskRow[] : [];
  const latest = versions.at(-1)?.revision ?? 0;
  const details = await summaries(context, tasks);
  const historyDetails = historyTasks.length ? await summaries(context, historyTasks) : [];
  const byUid = new Map(tasks.map((task, i) => [task.task_uid, {task, detail: details[i], allocation: allocation(task)}]));
  const leases = [...byUid.values()].filter(lease => lease.allocation.length && lease.task.phase !== "QUEUED");
  const historyLeases = historyTasks.map((task, index) => ({task, detail: historyDetails[index], allocation: allocation(task)}))
    .filter(lease => lease.allocation.length && lease.task.started_at != null && lease.task.finished_at != null);
  const probe = new Map(observations.map(row => [JSON.stringify([row.worker_id, row.pool_name, row.host_id, row.devices_json]), row]));
  const isPoolDraining = (zone: WorkerId, poolName: string) =>
    drains.some(row => row.worker_id === zone && row.scope === "pool" && row.pool_name === poolName);
  const isNodeDraining = (zone: WorkerId, poolName: string, host: string) =>
    isPoolDraining(zone, poolName) || drains.some(row => row.worker_id === zone && row.scope === "node" && row.pool_name === poolName && row.host_id === host);
  const isGroupDraining = (zone: WorkerId, poolName: string, host: string, ids: number[]) => {
    const key = JSON.stringify(ids);
    return isNodeDraining(zone, poolName, host) || drains.some(row =>
      row.worker_id === zone && row.scope === "device_group" && row.pool_name === poolName && row.host_id === host && row.devices_json === key);
  };
  const blocks = new Map<string, DeviceGroup[]>();
  const projections: Projection[] = [];

  const workers = await Promise.all(ZONES.map(async zone => {
    const worker = health.find(row => row.worker_id === zone);
    const revision = versions.find(row => row.revision === worker?.applied_rev) ?? versions[0];
    const pools = JSON.parse(revision?.config_json ?? "{}") as Pools;
    const status = workerStatus(context.now, zone, latest, worker);
    const entries = Object.entries(pools).filter(([, pool]) => pool.owner === zone);
    const poolTree: Timeline["workers"][number]["pools"] = [];
    for (const [pool_name, pool] of entries) {
      const rows: DeviceGroup[] = [];
      const states: ProjectionRow[] = [];
      const physical: RawAllocation[] = [];
      const nodes: Timeline["workers"][number]["pools"][number]["nodes"] = [];
      for (const node of pool.nodes) {
        const identity = await nodeIdentity(pepper(context), node.host_id, Boolean(context.principal));
        const device_groups: DeviceGroup[] = [];
        for (const group of node.allocation_groups ?? []) {
          const ids = group.devices.slice().sort((a, b) => a - b);
          const allocated = [...leases, ...historyLeases].filter(lease => lease.task.worker_id === zone && lease.task.target_pool === pool_name
            && overlaps(node.host_id, ids, lease.allocation));
          const observation = probe.get(JSON.stringify([zone, pool_name, node.host_id, JSON.stringify(ids)]));
          const groupDraining = isGroupDraining(zone, pool_name, node.host_id, ids);
          const display = projectDevice(context.now, {
            worker, pool, node, group, observation, leased: Boolean(allocated.length), draining: groupDraining,
          });
          let release_at = context.now;
          let uncertain = false;
          for (const {task, detail, allocation: allocatedNodes} of allocated) {
            const start_at = task.running_at ?? task.reserved_at ?? task.started_at ?? context.now;
            const ending = detail.estimated_sec === null ? context.now : start_at + detail.estimated_sec;
            release_at = Math.max(release_at, ending);
            uncertain ||= detail.estimated_sec === null || ending <= context.now || task.phase === "CLEANING";
            const related = blocks.get(task.task_uid) ?? [];
            blocks.set(task.task_uid, related);
            display.solid_blocks.push({
              task_uid: task.task_uid, start_at, total_cards: allocatedNodes.reduce((count, node) => count + node.devices.length, 0),
              is_primary_row: related.length === 0, group_count: 0,
            });
            related.push(display);
          }
          physical.push({host_id: node.host_id, devices: ids});
          states.push({...display, paused: display.paused || !pool.enabled || !group.verified, release_at, uncertain});
          rows.push(display);
          device_groups.push(display);
        }
        nodes.push({
          ...identity,
          draining: isNodeDraining(zone, pool_name, node.host_id),
          health: aggregateHealth(device_groups.map(item => item.health)),
          device_groups,
        });
      }
      for (let index = 0; index < states.length; index++) {
        const {host_id, devices} = physical[index];
        states[index].peers = physical.flatMap((other, i) => i !== index && overlaps(host_id, devices, [other]) ? [i] : []);
      }
      const queues = new Map<CaseKind, QueueTask[]>();
      for (const queued of queueRows) {
        if (queued.worker_id !== zone || queued.pool_name !== pool_name) continue;
        const entry = byUid.get(queued.task_uid);
        if (!entry || entry.task.phase !== "QUEUED") continue;
        const candidates = JSON.parse(queued.candidate_group_idxs_json) as {rev: number; options: number[][]};
        if (candidates.rev !== (worker?.applied_rev ?? 0)) continue;
        const queue = queues.get(queued.kind) ?? [];
        queues.set(queued.kind, queue);
        queue.push({task_uid: queued.task_uid, estimated_sec: entry.detail.estimated_sec, options: candidates.options});
      }
      for (const {projection, groupIndexes} of projectSchedule([...queues.values()], states, context.now)) {
        projections.push(projection);
        for (const index of groupIndexes) rows[index].dashed_blocks.push(projection);
      }
      poolTree.push({
        pool_name,
        type: pool.type,
        enabled: pool.enabled,
        paused: pool.enabled === false || Boolean(pool.paused),
        draining: isPoolDraining(zone, pool_name),
        health: aggregateHealth(nodes.map(node => node.health)),
        nodes,
      });
    }
    return {...status, offline: offline(context.now, worker), pools: poolTree};
  }));
  for (const [uid, rows] of blocks) {
    for (const row of rows) {
      const block = row.solid_blocks.find(item => item.task_uid === uid);
      if (block) block.group_count = rows.length;
    }
  }
  const tasks_by_uid = Object.fromEntries([...details, ...historyDetails].map(task => [task.task_uid, task]));
  const queued_by_pool: Record<string, TaskSummary[]> = {};
  for (const task of details.filter(item => item.phase === "QUEUED")) {
    const key = `${task.worker_id}/${task.target_pool}`;
    (queued_by_pool[key] ??= []).push(task);
  }
  for (const tasksForPool of Object.values(queued_by_pool)) tasksForPool.sort((left, right) =>
    (left.queue_pos ?? Number.MAX_SAFE_INTEGER) - (right.queue_pos ?? Number.MAX_SAFE_INTEGER));
  return {workers, now: context.now, zero_card_tasks: details.filter(task => task.kind === "worker_probe"), projections,
    tasks_by_uid, queued_by_pool, history_tasks: historyDetails};
}
