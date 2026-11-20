import {installation, supportsCancel} from "./cancelPlatform";
import {modelMap} from "../modelMap";
import {type Context, HttpError} from "../router";
import type {CaseKind, DeviceGroup, ModelRepo, ModelTab, Phase, Projection, Provider, RunGroup, TaskDetail, TaskSummary, Timeline, WorkflowList, WorkflowRow, WorkerId} from "../types/api";
import {pepper} from "./auth";
import {type GroupRow, offline, overlaps, type Pools, projectDevice, type RawAllocation, type WorkerRow, workerStatus} from "./deviceState";
import {durationKey, estimate, estimates, projectSchedule, type ProjectionRow, type QueueTask} from "./schedule";
import {base64url} from "../utils/crypto";
import {blobBuffer, gunzip} from "../utils/gzip";
import {nodeIdentity} from "../utils/mask";
import {runUrl} from "../utils/taskIdentity";
import {POOL, REPO} from "../utils/validate";

type TaskRow = Pick<TaskSummary, "task_uid" | "worker_id" | "provider" | "repo" | "logical_model" | "run_id" |
  "attempt" | "dispatch_id" | "case_id" | "test_id" | "label" | "kind" | "target_pool" | "req_nodes" | "req_devices" |
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
      task_uid, worker_id, provider, repo, run_id, attempt, dispatch_id, case_id, label, kind,
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
  const latest = Number((results[1].results[0] as {revision: number} | undefined)?.revision ?? 0);
  const health = results[0].results as unknown as WorkerRow[];
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
    UNION ALL SELECT logical_model,provider,repo,0 FROM task_index GROUP BY logical_model,provider,repo
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
  return {models: [...tabs.values()]};
}

function byRun(tasks: TaskSummary[]): RunGroup[] {
  const bins = new Map<string, RunGroup>();
  for (const task of tasks) {
    const {provider, repo, run_id, attempt} = task;
    const key = JSON.stringify([provider, repo, run_id, attempt]);
    if (!bins.has(key)) bins.set(key, {provider, repo, run_id, attempt, tasks: []});
    bins.get(key)!.tasks.push(task);
  }
  return [...bins.values()];
}

function workflowIdentity(task: TaskSummary) {
  return JSON.stringify([task.provider, task.repo, task.run_id, task.attempt]);
}

function workflowName(task: TaskSummary) {
  return task.workflow_name ?? task.workflow_file?.split("/").at(-1)?.replace(/\.ya?ml$/, "") ?? null;
}

function workflowScope(task: TaskSummary) {
  return task.repo.startsWith("depeng1994/") ? "个人仓" as const : "组织仓" as const;
}

function workflowRows(tasks: TaskSummary[], history: boolean): WorkflowRow[] {
  const groups = new Map<string, TaskSummary[]>();
  for (const task of tasks) {
    const key = workflowIdentity(task);
    groups.set(key, [...(groups.get(key) ?? []), task]);
  }
  const rows = [...groups.values()].map((cases) => {
    const first = cases[0];
    const active = cases.some(item => item.phase != null);
    const inFlight = cases.some(item => item.phase != null && item.phase !== "QUEUED");
    const queued = cases.some(item => item.phase === "QUEUED" && item.kind !== "worker_probe");
    const passed = cases.every(item => item.status === "PASS");
    const failed = cases.some(item => item.status && item.status !== "PASS");
    const finished = cases.map(item => item.finished_at ?? 0).filter(Boolean);
    const submitted = cases.map(item => item.submitted_at ?? item.queued_at ?? 0).filter(Boolean);
    const started = cases.map(item => item.queued_at ?? 0).filter(Boolean);
    const startedAt = cases.map(item => item.started_at ?? 0).filter(Boolean);
    const cards = Math.max(0, ...cases.map(item => item.allocated_cards));
    const measured = cases.filter(item => item.allocated_cards > 0 &&
      (item.duration_sec != null || (item.started_at != null && (item.finished_at != null || inFlight))) &&
      (item.duration_sec != null || item.estimate_status !== "NO_ESTIMATE"));
    const durations = measured.reduce((sum, item) => sum + Math.max(0,
      item.duration_sec ?? ((item.finished_at ?? (inFlight ? Date.now() / 1000 : item.started_at!)) - item.started_at!)), 0);
    return {
      provider: first.provider, repo: first.repo, run_id: first.run_id, attempt: first.attempt,
      sequence: history ? 0 : queued ? 0 : 0,
      submitted_at: submitted.length ? Math.min(...submitted) : null,
      repo_scope: workflowScope(first), logical_model: first.logical_model,
      workflow_name: workflowName(first), workflow_file: first.workflow_file,
      pools: [...new Set(cases.map(item => item.target_pool))], max_cards: cards,
      result: (history ? (passed ? "PASS" : "FAIL") : "QUEUED") as WorkflowRow["result"],
      trigger_actor: first.trigger_actor, event_type: first.event_type,
      pr_number: first.pr_number, pr_author: first.pr_author,
      started_at: startedAt.length ? Math.min(...startedAt) : null,
      finished_at: finished.length ? Math.max(...finished) : null,
      card_hours: durations && measured.length ? Number((measured.reduce((sum, item) => sum + item.allocated_cards *
        Math.max(0, item.duration_sec ?? ((item.finished_at ?? (inFlight ? Date.now() / 1000 : item.started_at!)) - item.started_at!)), 0) / 3600).toFixed(2)) : null,
      card_hours_as_of_now: !history && inFlight,
      card_hours_partial: measured.length > 0 && measured.length < cases.filter(item => item.allocated_cards > 0).length,
      cancel_supported: cases.some(item => item.cancel_supported),
      can_move_up: cases.some(item => item.can_move_up), cases,
    };
  });
  if (history) {
    rows.sort((left, right) => (right.finished_at ?? 0) - (left.finished_at ?? 0) ||
      workflowIdentity(left.cases[0]).localeCompare(workflowIdentity(right.cases[0])));
    rows.forEach((row, index) => { row.sequence = -(index + 1); });
  } else {
    const queue = rows.filter(row => row.cases.some(item => item.phase === "QUEUED" && item.kind !== "worker_probe"))
      .sort((left, right) => (left.submitted_at ?? Number.MAX_SAFE_INTEGER) - (right.submitted_at ?? Number.MAX_SAFE_INTEGER) ||
        workflowIdentity(left.cases[0]).localeCompare(workflowIdentity(right.cases[0])));
    queue.forEach((row, index) => { row.sequence = index + 1; });
    rows.forEach(row => { if (row.cases.some(item => item.phase != null && item.phase !== "QUEUED")) row.sequence = 0; });
  }
  return rows;
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
  const {DB: db} = context.env;
  const result = await db.batch<TaskRow>([
    db.prepare(ACTIVE_SELECT + " ORDER BY a.queued_at,a.task_uid"),
    // The requested history_limit is a Workflow count. Fetching a fixed task
    // cap can silently truncate a Workflow when one run has many cases.
    db.prepare("SELECT * FROM task_index ORDER BY finished_at DESC,provider,repo,run_id,attempt,task_uid"),
    db.prepare("SELECT COUNT(DISTINCT provider||'/'||repo||'/'||run_id||'/'||attempt) AS count FROM task_index"),
    db.prepare("SELECT COALESCE(MAX(queue_ver),0) AS version FROM queue_orders"),
  ]);
  const filter = (row: WorkflowRow) => (!model || row.logical_model === model) && (!repo || row.repo === repo) &&
    (!platform || row.provider === platform) && (!pool || row.pools.includes(pool));
  const active = await summaries(context, result[0].results as unknown as TaskRow[]);
  // A Workflow can have completed child cases while another child is still
  // active. Merge those historical cases into the active row so one Workflow
  // never appears in both tables.
  const historical = await summaries(context, result[1].results as unknown as TaskRow[]);
  const activeKeys = new Set(active.map(workflowIdentity));
  const mixedHistory = historical.filter(task => activeKeys.has(workflowIdentity(task)));
  const activeRows = workflowRows([...active, ...mixedHistory], false).filter(filter);
  const historicalOnly = historical.filter(task => !activeKeys.has(workflowIdentity(task)));
  const allHistoryRows = workflowRows(historicalOnly, true).filter(filter);
  const cursor = context.url.searchParams.get("cursor");
  let historyRows = allHistoryRows;
  if (cursor) {
    try {
      const decoded = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(cursor.replaceAll("-", "+").replaceAll("_", "/")), c => c.charCodeAt(0))));
      if (!Array.isArray(decoded) || decoded.length !== 5) throw new Error();
      const [cursorFinished, cursorProvider, cursorRepo, cursorRun, cursorAttempt] = decoded as [number, string, string, string, number];
      if (!Number.isSafeInteger(cursorFinished) || typeof cursorProvider !== "string" || typeof cursorRepo !== "string" ||
        typeof cursorRun !== "string" || !Number.isSafeInteger(cursorAttempt)) throw new Error();
      const cursorWorkflow = JSON.stringify([cursorProvider, cursorRepo, cursorRun, cursorAttempt]);
      historyRows = historyRows.filter(row => {
        if ((row.finished_at ?? 0) !== cursorFinished) return (row.finished_at ?? 0) < cursorFinished;
        return JSON.stringify([row.provider, row.repo, row.run_id, row.attempt]) > cursorWorkflow;
      });
    } catch { throw new HttpError(400, "INVALID_CURSOR"); }
  }
  const visibleHistory = historyRows.slice(0, historyLimit);
  const last = visibleHistory.at(-1);
  const next_cursor = last ? base64url(new TextEncoder().encode(JSON.stringify([
    last.finished_at ?? 0, last.provider, last.repo, last.run_id, last.attempt,
  ]))) : null;
  const showActive = !status || status === "active";
  const showHistory = !status || status === "history";
  return {
    queue_version: Number((result[3].results[0] as {version?: number} | undefined)?.version ?? 0),
    active: showActive ? activeRows : [], history: showHistory ? visibleHistory : [],
    history_total: showHistory ? allHistoryRows.length : 0,
    next_cursor: showHistory && historyRows.length > historyLimit ? next_cursor : null,
    has_more: showHistory && historyRows.length > historyLimit, truncated_by_capacity: Boolean(await db.prepare("SELECT 1 FROM system_meta WHERE meta_key='capacity_purged_before_ts'").first()),
    purged_notice: Boolean(await db.prepare("SELECT 1 FROM system_meta WHERE meta_key='capacity_purged_before_ts'").first()) ? "更早的记录已因容量清理" : null,
  };
}
function readCursor(cursor: string | null, slug: string): [number, string] {
  if (!cursor) return [Number.MAX_SAFE_INTEGER, "~"];
  try {
    if (!/^[A-Za-z0-9_-]{1,300}$/.test(cursor)) throw new Error();
    const [model, finished, uid] = JSON.parse(atob(cursor.replaceAll("-", "+").replaceAll("_", "/")));
    if (model === slug && Number.isSafeInteger(finished) && finished >= 0 && typeof uid === "string" && uid.length <= 80) {
      return [finished, uid];
    }
  } catch {
    // Invalid cursors are never interpolated into SQL.
  }
  throw new HttpError(400, "INVALID_CURSOR");
}
export async function modelRuns(context: Context) {
  const slug = context.params.slug;
  if (!/^[A-Za-z0-9_.-]{1,128}$/.test(slug)) throw new HttpError(400, "INVALID_MODEL");
  const [finished, uid] = readCursor(context.url.searchParams.get("cursor"), slug);
  const limit = Number(context.url.searchParams.get("limit") ?? 50);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 50) throw new HttpError(400, "INVALID_LIMIT");
  const {DB: db} = context.env;
  const result = await db.batch<TaskRow>([
    db.prepare(ACTIVE_SELECT + " WHERE a.logical_model=? ORDER BY a.queued_at,a.task_uid").bind(slug),
    db.prepare(`SELECT * FROM task_index WHERE logical_model=? AND (finished_at,task_uid)<(?,?)
        ORDER BY finished_at DESC,task_uid DESC LIMIT ?`).bind(slug, finished, uid, limit + 1),
  ]);
  const [active, history] = result.map(entry => entry.results);
  const has_more = history.length > limit;
  const visible = history.slice(0, limit);
  const last = visible.at(-1);
  const truncated = !has_more && Boolean(await db.prepare(
    "SELECT meta_value FROM system_meta WHERE meta_key='capacity_purged_before_ts'",
  ).first("meta_value"));
  const [activeTasks, historicalTasks] = await Promise.all([summaries(context, active), summaries(context, visible)]);
  return {
    active: byRun(activeTasks), history: byRun(historicalTasks), has_more,
    next_cursor: has_more && last ? base64url(new TextEncoder().encode(JSON.stringify([slug, last.finished_at, last.task_uid]))) : null,
    truncated_by_capacity: truncated, purged_notice: truncated ? "更早的记录已因容量清理" : null,
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
  const results = await db.batch([
    db.prepare("SELECT * FROM workers"),
    db.prepare(`SELECT revision,config_json FROM pools_configs WHERE revision IN
      (SELECT MAX(revision) FROM pools_configs UNION SELECT MIN(revision) FROM pools_configs UNION SELECT applied_rev FROM workers)
      ORDER BY revision`),
    db.prepare(ACTIVE_SELECT + " ORDER BY a.task_uid"),
    db.prepare("SELECT * FROM device_group_states"),
    db.prepare("SELECT * FROM queue_orders ORDER BY worker_id,pool_name,kind,queue_pos,task_uid"),
  ]);
  const [health, versions, tasks, observations, queueRows] = results.map(result => result.results) as unknown as
    [WorkerRow[], Revision[], TaskRow[], GroupRow[], QueueRow[]];
  const latest = versions.at(-1)?.revision ?? 0;
  const details = await summaries(context, tasks);
  const byUid = new Map(tasks.map((task, i) => [task.task_uid, {task, detail: details[i], allocation: allocation(task)}]));
  const leases = [...byUid.values()].filter(lease => lease.allocation.length && lease.task.phase !== "QUEUED");
  const probe = new Map(observations.map(row => [JSON.stringify([row.worker_id, row.pool_name, row.host_id, row.devices_json]), row]));
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
          const allocated = leases.filter(lease => lease.task.worker_id === zone && lease.task.target_pool === pool_name
            && overlaps(node.host_id, ids, lease.allocation));
          const observation = probe.get(JSON.stringify([zone, pool_name, node.host_id, JSON.stringify(ids)]));
          const display = projectDevice(context.now, {worker, pool, node, group, observation, leased: Boolean(allocated.length)});
          let release_at = context.now;
          let uncertain = false;
          for (const {task, detail, allocation: allocatedNodes} of allocated) {
            const start_at = task.running_at ?? task.reserved_at ?? context.now;
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
        nodes.push({...identity, device_groups});
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
      poolTree.push({pool_name, type: pool.type, enabled: pool.enabled, paused: Boolean(pool.paused), nodes});
    }
    return {...status, offline: offline(context.now, worker), pools: poolTree};
  }));
  for (const [uid, rows] of blocks) {
    for (const row of rows) {
      const block = row.solid_blocks.find(item => item.task_uid === uid);
      if (block) block.group_count = rows.length;
    }
  }
  const tasks_by_uid = Object.fromEntries(details.map(task => [task.task_uid, task]));
  const queued_by_pool: Record<string, TaskSummary[]> = {};
  for (const task of details.filter(item => item.phase === "QUEUED")) {
    const key = `${task.worker_id}/${task.target_pool}`;
    (queued_by_pool[key] ??= []).push(task);
  }
  for (const tasksForPool of Object.values(queued_by_pool)) tasksForPool.sort((left, right) =>
    (left.queue_pos ?? Number.MAX_SAFE_INTEGER) - (right.queue_pos ?? Number.MAX_SAFE_INTEGER));
  return {workers, now: context.now, zero_card_tasks: details.filter(task => task.kind === "worker_probe"), projections,
    tasks_by_uid, queued_by_pool};
}
