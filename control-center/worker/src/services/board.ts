import {supportsCancel} from "./cancelPlatform";
import {modelMap} from "../modelMap";
import {type Context, HttpError} from "../router";
import type {CaseKind, DeviceGroup, ModelTab, Phase, Projection, Provider, RunGroup, TaskDetail, TaskSummary, Timeline, WorkerId} from "../types/api";
import {pepper} from "./auth";
import {type GroupRow, offline, overlaps, type Pools, projectDevice, type RawAllocation, type WorkerRow, workerStatus} from "./deviceState";
import {durationKey, estimate, estimates, projectSchedule, type ProjectionRow, type QueueTask} from "./schedule";
import {base64url} from "../utils/crypto";
import {gunzip} from "../utils/gzip";
import {nodeIdentity} from "../utils/mask";
import {runUrl} from "../utils/taskIdentity";

type TaskRow = Pick<TaskSummary, "task_uid" | "worker_id" | "provider" | "repo" | "logical_model" | "run_id" |
  "attempt" | "dispatch_id" | "case_id" | "test_id" | "label" | "kind" | "target_pool" | "req_nodes" | "req_devices" |
  "pr_number" | "pr_author" | "trigger_actor" | "event_type" | "workflow_file"> & {
  phase?: Phase; status?: string; queued_at?: number; finished_at?: number;
  reserved_at?: number; staging_at?: number; running_at?: number; cleaning_at?: number;
  exit_code?: number; cleanup_verified?: number; duration_sec?: number; timing_reliable: number;
  allocation_json?: string; actual_allocation_json?: string; storage_tier?: "hot" | "cold"; cold_chunk_id?: string;
  queue_pos?: number;
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
    return {
      task_uid, worker_id, provider, repo, run_id, attempt, dispatch_id, case_id, label, kind,
      pr_number, pr_author, trigger_actor, event_type, workflow_file, ...shape,
      phase: row.phase ?? null, status: row.status ?? null, queued_at: row.queued_at ?? null,
      finished_at: row.finished_at ?? null, queue_pos: row.queue_pos ?? null,
      allocation: await Promise.all(allocation(row).map(async alloc => ({...await node(alloc.host_id), devices: alloc.devices}))),
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
        .bind(row.cold_chunk_id).first<number[]>("gzip_blob");
      if (!blob) throw new HttpError(503, "TASK_DETAIL_UNAVAILABLE");
      detail = JSON.parse(await gunzip(Uint8Array.from(blob).buffer))[uid];
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
  return {workers, now: context.now, zero_card_tasks: details.filter(task => task.kind === "worker_probe"), projections};
}
