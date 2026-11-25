import {type Context, HttpError} from "../router";
import type {ClusterPreview, WorkerId} from "../types/api";
import {auditStatement} from "./audit";
import {pepper} from "./auth";
import {aggregateHealth, type GroupRow, groups, overlaps, type Pool, type PoolGroup, type PoolNode, type Pools, projectDevice, type RawAllocation, type WorkerRow} from "./deviceState";
import {hmac, safeHashEqual, sha256} from "../utils/crypto";
import {nodeIdentity} from "../utils/mask";
import {array, devices, hostId, integer, invalid, member, object, POOL, string} from "../utils/validate";

const ZONES = ["blue", "yellow"] as const;
const ACTIVE = ["RESERVED", "STAGING", "RUNNING", "CLEANING"];
const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,100}$/;
const HEX = /^[a-f0-9]{64}$/;
type Revision = {revision: number; config_json: string; sha256: string};
type ImpactGroup = ClusterPreview["affected_groups"][number] & {paused: boolean};
type Edit = Awaited<ReturnType<typeof plan>>;
const GROUP_KEY = ["pool_name","worker_id","host_id","devices"] as const;
const configTag = (pool: Pool, node: PoolNode, group: PoolGroup) =>
  Boolean(pool.enabled === false || pool.paused || node.paused || group.paused);

function groupKey(item: ImpactGroup) {
  return JSON.stringify(GROUP_KEY.map(key => item[key]));
}

function configGroups(pools: Pools): ImpactGroup[] {
  return Object.entries(pools).flatMap(([pool_name, pool]) => groups(pool).map(({node, group}) => ({
    worker_id: pool.owner, pool_name, host_id: node.host_id, devices: [...group.devices].sort((a,b) => a-b),
    paused: configTag(pool, node, group),
  })));
}

type DrainTarget = {
  worker_id: WorkerId;
  scope: "pool" | "node" | "device_group";
  pool_name: string;
  host_id: string | null;
  devices: number[] | null;
  target_key: string;
};

function drainTarget(pools: Pools, input: Record<string, unknown>): DrainTarget {
  const pool_name = string(input.pool, "pool", 64, POOL);
  const pool = pools[pool_name];
  if (!pool) throw new HttpError(404, "POOL_NOT_FOUND");
  const op = member(input.op, "operation", ["DRAIN", "RESUME", "DELETE_POOL", "DELETE_NODE", "DELETE_DEVICE_GROUP"] as const);
  const scope = op === "DELETE_POOL" ? "pool" : op === "DELETE_NODE" ? "node" :
    op === "DELETE_DEVICE_GROUP" ? "device_group" : member(input.scope, "scope", ["pool", "node", "device_group"] as const);
  const host_id = scope === "pool" ? null : hostId(input.host_id);
  const devicesValue = scope === "device_group" ? devices(input.devices) : null;
  const target_key = JSON.stringify([pool.owner, scope, pool_name, host_id, devicesValue]);
  return {worker_id: pool.owner, scope, pool_name, host_id, devices: devicesValue, target_key};
}

function paused(value: Record<string, unknown>) {
  if ("paused" in value && value.paused !== true) invalid("paused must be true when present");
}

function validateGroup(raw: unknown, inventory: number[]): PoolGroup {
  const group = object(raw, "allocation_group");
  paused(group);
  const ids = devices(group.devices);
  if (ids.some(id => !inventory.includes(id))) invalid("device group outside inventory");
  if (typeof group.verified !== "boolean") invalid("group verified");
  if (Object.keys(group).some(key => !["devices", "verified", "paused"].includes(key))) invalid("group fields");
  return {...group, devices: ids, verified: group.verified} as PoolGroup;
}
function validateNode(raw: unknown, requireFingerprint: boolean) {
  const node = object(raw, "node");
  const allowed = ["host_id","ssh","host_ssh","broadcast_ssh","node_ip","network_interface","device_ids","allocation_groups",
    "capabilities","machine_fingerprint","environments","paused","occupancy_policy","priority","pythonpath"];
  if (Object.keys(node).some(key => !allowed.includes(key))) invalid("node fields");
  paused(node);
  hostId(node.host_id);
  string(node.ssh, "ssh", 101, ALIAS);
  if (node.host_ssh != null) string(node.host_ssh, "host_ssh", 101, ALIAS);
  if (node.broadcast_ssh != null) for (const alias of array(node.broadcast_ssh, "broadcast_ssh", 32)) string(alias, "broadcast SSH", 101, ALIAS);
  if (node.node_ip != null || requireFingerprint) {
    const ip = string(node.node_ip, "node_ip", 15, /^\d{1,3}(?:\.\d{1,3}){3}$/);
    hostId(ip + ":22");
  }
  if (node.network_interface != null || requireFingerprint) string(node.network_interface, "network_interface", 32, /^[a-zA-Z0-9_.-]+$/);
  const inventory = devices(node.device_ids);
  const allocation = array(node.allocation_groups, "allocation_groups", 64);
  const seen = new Set<string>();
  for (const raw of allocation) {
    const key = JSON.stringify(validateGroup(raw, inventory).devices);
    if (seen.has(key)) invalid("duplicate allocation group");
    seen.add(key);
  }
  if (requireFingerprint || node.machine_fingerprint !== undefined) string(node.machine_fingerprint, "machine fingerprint", 64, HEX);
  if (node.occupancy_policy !== undefined) member(node.occupancy_policy, "occupancy_policy", ["wait","preempt"] as const);
  if (node.pythonpath != null) string(node.pythonpath, "pythonpath", 512, /^\/[^:\r\n\0]+$/);
  if (node.priority != null) integer(node.priority, "priority", -10000, 10000);
  if (node.capabilities != null) {
    const entries = array(node.capabilities, "capabilities", 2);
    if (new Set(entries).size !== entries.length) invalid("duplicate capabilities");
    entries.forEach(kind => member(kind, "capability", ["train","npu_smoke"] as const));
  }
  for (const [name, unknown] of Object.entries(object(node.environments ?? {}, "environments"))) {
    string(name, "environment name", 128, /^[A-Za-z0-9_.:-]+$/);
    const env = object(unknown, "environment");
    if (Object.keys(env).some(key => !["container_id","image_id","image_ref"].includes(key))) invalid("environment fields");
    string(env.container_id, "container id", 64, HEX);
    string(env.image_id, "image id", 71, /^sha256:[a-f0-9]{64}$/);
    string(env.image_ref, "image ref", 256, /^[A-Za-z0-9._:/@-]+$/);
  }
  return node as unknown as PoolNode;
}

function validatePools(pools: Pools) {
  if (Object.keys(pools).length > 32) invalid("too many pools");
  const hosts = new Set<string>();
  const fingerprints = new Set<string>();
  for (const [name, pool] of Object.entries(pools)) {
    string(name, "pool", 64, /^[a-zA-Z0-9_.-]+$/);
    paused(pool);
    member(pool.owner, "worker", ZONES);
    const type = member(pool.type, "pool type", ["ascend-a3", "ascend-a5"] as const);
    if (pool.process_indexing !== (type === "ascend-a3" ? "card-die" : "card")) invalid("process_indexing");
    if (typeof pool.enabled !== "boolean" || (pool.force_reclaim !== undefined && typeof pool.force_reclaim !== "boolean")) {
      invalid("pool flags");
    }
    const allowed = ["type","owner","enabled","paused","force_reclaim","process_indexing","nodes",
      "max_jobs_per_host","notes","verified_multinode_allocations","default_docker"];
    if (Object.keys(pool).some(key => !allowed.includes(key))) invalid("pool fields");
    if (pool.max_jobs_per_host != null) integer(pool.max_jobs_per_host, "max_jobs_per_host", 1, 64);
    if (pool.default_docker !== undefined) string(pool.default_docker, "default_docker", 128, /^[A-Za-z0-9_.:-]+$/);
    if (pool.notes != null) string(pool.notes, "notes", 2048);
    const groupsByNode = new Map<string, Set<string>>();
    for (const raw of array(pool.nodes, "nodes", 64)) {
      const node = validateNode(raw, false);
      if (hosts.has(node.host_id) || (node.machine_fingerprint && fingerprints.has(node.machine_fingerprint))) {
        throw new HttpError(409, "DUPLICATE_NODE");
      }
      hosts.add(node.host_id);
      if (node.machine_fingerprint) fingerprints.add(node.machine_fingerprint);
      groupsByNode.set(node.host_id, new Set(node.allocation_groups?.filter(group => group.verified)
        .map(group => JSON.stringify(group.devices)) ?? []));
    }
    for (const raw of array(pool.verified_multinode_allocations ?? [], "multi-node allocations", 96)) {
      const bundle = array(raw, "bundle", 16);
      if (bundle.length < 2) invalid("multi-node bundle");
      const used = new Set<string>();
      for (const row of bundle) {
        const entry = object(row, "bundle group");
        const host = hostId(entry.host_id);
        devices(entry.devices);
        if (used.has(host) || !groupsByNode.get(host)?.has(JSON.stringify(entry.devices))) invalid("multi-node unverified group");
        used.add(host);
      }
    }
  }
}

type ClusterOp = "PAUSE" | "RESUME" | "DRAIN" | "ADD_POOL" | "ADD_NODE" | "ADD_DEVICE_GROUP" |
  "DELETE_POOL" | "DELETE_NODE" | "DELETE_DEVICE_GROUP" | "SET_OCCUPANCY_POLICY";

function operate(old: Pools, input: Record<string, unknown>): Pools {
  const pools = structuredClone(old);
  const op = member(input.op, "operation", ["PAUSE","RESUME","DRAIN","ADD_POOL","ADD_NODE","ADD_DEVICE_GROUP",
    "DELETE_POOL","DELETE_NODE","DELETE_DEVICE_GROUP","SET_OCCUPANCY_POLICY"] as const) as ClusterOp;
  const name = string(input.pool, "pool", 64, POOL);
  if (!name || ["__proto__","constructor","prototype"].includes(name)) invalid("pool");
  const pool = Object.hasOwn(pools, name) ? pools[name] : undefined;
  if (op === "ADD_POOL") {
    if (pool) throw new HttpError(409, "POOL_EXISTS");
    const value = object(input.value, "pool value");
    for (const node of array(value.nodes, "nodes", 64)) validateNode(node, true);
    pools[name] = value as unknown as Pool;
    return pools;
  }
  if (!pool) throw new HttpError(404, "POOL_NOT_FOUND");
  if (op === "ADD_NODE") {
    pool.nodes.push(validateNode(input.value, true));
    return pools;
  }
  if (op === "DELETE_POOL") {
    delete pools[name];
    return pools;
  }

  const scope = op === "PAUSE" || op === "RESUME" || op === "DRAIN"
    ? member(input.scope, "scope", ["pool","node","device_group"] as const)
    : op === "DELETE_NODE" || op === "SET_OCCUPANCY_POLICY" ? "node" : "device_group";
  let target: Pool | PoolNode | PoolGroup = pool;
  if (scope !== "pool") {
    const host = hostId(input.host_id);
    const node = pool.nodes.find(item => item.host_id === host);
    if (!node) throw new HttpError(404, "NODE_NOT_FOUND");
    target = node;
    if (op === "ADD_DEVICE_GROUP") {
      (node.allocation_groups ??= []).push(validateGroup(input.value, node.device_ids ?? []));
      return pools;
    }
    if (op === "SET_OCCUPANCY_POLICY") {
      const policy = member(
        input.occupancy_policy ?? object(input.value, "occupancy_policy").occupancy_policy,
        "occupancy_policy",
        ["wait", "preempt"] as const,
      );
      node.occupancy_policy = policy;
      return pools;
    }
    if (op === "DELETE_NODE") {
      pool.nodes = pool.nodes.filter(item => item !== node);
      pool.verified_multinode_allocations = pool.verified_multinode_allocations?.filter(bundle => !bundle.some(part => part.host_id === host));
      return pools;
    }
    if (scope === "device_group") {
      const wanted = JSON.stringify(devices(input.devices));
      const group = node.allocation_groups?.find(item => JSON.stringify([...item.devices].sort((a,b) => a-b)) === wanted);
      if (!group) throw new HttpError(404, "GROUP_NOT_FOUND");
      target = group;
      if (op === "DELETE_DEVICE_GROUP") {
        node.allocation_groups = node.allocation_groups!.filter(item => item !== group);
        pool.verified_multinode_allocations = pool.verified_multinode_allocations?.filter(bundle =>
          !bundle.some(part => part.host_id === host && JSON.stringify([...part.devices].sort((a,b) => a-b)) === wanted));
        return pools;
      }
    }
  }
  if (op === "PAUSE" || op === "DRAIN") {
    if (scope === "pool") {
      pool.enabled = false;
      delete pool.paused;
    } else {
      target.paused = true;
    }
  } else if (op === "RESUME") {
    if (scope === "pool") {
      pool.enabled = true;
      delete pool.paused;
    } else {
      delete target.paused;
    }
  }
  return pools;
}

function assertMigration(effective: Pools, next: Pools) {
  const owners = new Map<string, WorkerId>();
  for (const pool of Object.values(effective)) for (const node of pool.nodes) {
    owners.set("host:" + node.host_id, pool.owner);
    if (node.machine_fingerprint) owners.set("fp:" + node.machine_fingerprint, pool.owner);
  }
  for (const pool of Object.values(next)) for (const node of pool.nodes) {
    const before = [owners.get("host:" + node.host_id), owners.get("fp:" + node.machine_fingerprint)];
    if (before.some(owner => owner && owner !== pool.owner)) throw new HttpError(409, "SOURCE_WORKER_NOT_DRAINED");
  }
}

async function plan(context: Context, input: Record<string, unknown>, rollback = false) {
  const db = context.env.DB;
  const base_revision = integer(input.base_revision, "base_revision", 1);
  const base = await db.prepare("SELECT revision,config_json FROM pools_configs ORDER BY revision DESC LIMIT 1").first<Revision>();
  if (!base || base_revision !== base.revision) throw new HttpError(409, "REVISION_CONFLICT");
  const old = JSON.parse(base.config_json) as Pools;
  let rollbackFrom: number | null = null;
  let next: Pools;
  const operation = rollback ? null : object(input.operation, "operation");
  const operationName = operation ? String(operation.op) : null;
  const target = operation && ["DRAIN", "RESUME", "DELETE_POOL", "DELETE_NODE", "DELETE_DEVICE_GROUP"].includes(operationName ?? "")
    ? drainTarget(old, operation) : null;
  if (rollback) {
    rollbackFrom = integer(input.target_revision, "target_revision", 1);
    const restored = await db.prepare("SELECT config_json FROM pools_configs WHERE revision=?").bind(rollbackFrom).first<string>("config_json");
    if (!restored) throw new HttpError(404, "CONFIG_NOT_FOUND");
    next = JSON.parse(restored);
  } else {
    next = operate(old, operation!);
  }
  validatePools(next);
  assertMigration(old, next);
  const effective = await db.prepare(`
    WITH zones(id) AS (VALUES ('blue'),('yellow'))
    SELECT zones.id AS worker_id,p.config_json FROM zones LEFT JOIN workers w ON w.worker_id=zones.id
    JOIN pools_configs p ON p.revision=COALESCE(NULLIF(w.applied_rev,0),1)
  `).all<{worker_id: WorkerId; config_json: string}>();
  for (const {worker_id, config_json} of effective.results) {
    const owned = Object.fromEntries(Object.entries(JSON.parse(config_json) as Pools).filter(([,pool]) => pool.owner === worker_id));
    assertMigration(owned, next);
  }

  const before = configGroups(old);
  const after = configGroups(next);
  const prior = new Map(before.map(group => [groupKey(group), group]));
  const following = new Map(after.map(group => [groupKey(group), group]));
  const removed = before.filter(group => !following.has(groupKey(group)));
  const isDrain = operationName === "DRAIN";
  const matchesTarget = (group: ImpactGroup) => {
    if (!target || group.worker_id !== target.worker_id || group.pool_name !== target.pool_name) return false;
    if (target.scope === "pool") return true;
    if (target.scope === "node") return group.host_id === target.host_id;
    return group.host_id === target.host_id && JSON.stringify(group.devices) === JSON.stringify(target.devices);
  };
  const affected = [
    ...before.filter(group => !following.has(groupKey(group)) || following.get(groupKey(group))!.paused !== group.paused ||
      (isDrain && matchesTarget(group))),
    ...after.filter(group => !prior.has(groupKey(group))),
  ];
  const affected_groups = affected.map(({paused: _, ...group}) => group);
  const affected_combinations = Object.entries(old).flatMap(([pool_name, pool]) =>
    (pool.verified_multinode_allocations ?? []).filter(bundle => affected.some(group =>
      group.pool_name === pool_name && overlaps(group.host_id, group.devices, bundle)))
      .map(bundle => ({pool_name, allocation: bundle})));
  const active_leases: string[] = [];
  const drain_cancel_tasks: string[] = [];
  const drain = new Map<WorkerId, string[]>();
  const blocking_reasons: string[] = [];
  if (affected.length) {
    const flags = await db.prepare("SELECT worker_id,pool_name,host_id,devices_json FROM device_group_states WHERE quarantined=1").all<{
      worker_id: WorkerId; pool_name: string; host_id: string; devices_json: string;
    }>();
    if (removed.some(group => flags.results.some(row => row.worker_id === group.worker_id &&
      row.pool_name === group.pool_name && row.host_id === group.host_id &&
      (JSON.parse(row.devices_json) as number[]).some(device => group.devices.includes(device))))) {
      blocking_reasons.push("NODE_QUARANTINED");
    }
    const info = await db.prepare(`
      SELECT a.task_uid,a.task_key,a.phase,a.worker_id,a.reporter_id,a.is_test,a.target_pool,a.allocation_json,q.candidate_group_idxs_json
      FROM tasks_active a LEFT JOIN queue_orders q ON q.task_uid=a.task_uid AND q.worker_id=a.worker_id
        AND q.pool_name=a.target_pool AND q.kind=a.kind ORDER BY a.task_uid
    `).all<{task_uid: string; task_key: string; phase: string; worker_id: WorkerId; reporter_id: string | null; is_test: number; target_pool: string;
      allocation_json: string; candidate_group_idxs_json: string | null}>();
    for (const row of info.results) {
      const relevant = affected.filter(group => group.worker_id === row.worker_id && group.pool_name === row.target_pool);
      if (!relevant.length) continue;
      if (ACTIVE.includes(row.phase) && relevant.some(group => overlaps(group.host_id, group.devices,
        JSON.parse(row.allocation_json) as RawAllocation[]))) {
        active_leases.push(row.task_uid);
      }
      if (row.phase !== "QUEUED") continue;
      const candidates = JSON.parse(row.candidate_group_idxs_json ?? "{}").options as number[][] | undefined;
      const oldPoolGroups = before.filter(group => group.pool_name === row.target_pool);
      const groupUnavailable = (member: ImpactGroup) => {
        const afterGroup = following.get(groupKey(member));
        return !afterGroup || (isDrain && afterGroup.paused);
      };
      const lostAll = candidates?.length
        ? candidates.every(bundle => bundle.some(index => {
            const member = oldPoolGroups[index];
            return member && groupUnavailable(member);
          }))
        : (isDrain && oldPoolGroups.length > 0 && oldPoolGroups.every(groupUnavailable));
      const drainedWholePool = isDrain && target?.scope === "pool" && target.pool_name === row.target_pool;
      if (!Object.hasOwn(next, row.target_pool) || drainedWholePool || lostAll) {
        drain_cancel_tasks.push(row.task_uid);
        if ((row.reporter_id ?? row.worker_id) === row.worker_id && row.is_test !== 1) {
          const keys = drain.get(row.worker_id) ?? [];
          keys.push(row.task_key);
          drain.set(row.worker_id, keys);
        }
      }
    }
  }
  if (target && operationName?.startsWith("DELETE")) {
    const drainRow = await db.prepare(`SELECT revision FROM cluster_drain_targets WHERE target_key=?`)
      .bind(target.target_key).first<{revision: number}>();
    const applied = await db.prepare("SELECT applied_rev FROM workers WHERE worker_id=?")
      .bind(target.worker_id).first<number>("applied_rev");
    if (!drainRow || (applied ?? 0) < drainRow.revision) blocking_reasons.push("RESOURCE_NOT_DRAINED");
    if (active_leases.length) blocking_reasons.push("ACTIVE_LEASES_REMAINING");
    const targetGroups = configGroups(old).filter(group => group.pool_name === target.pool_name);
    const targetIndexes = target.scope === "pool" ? null : targetGroups.flatMap((group, index) => {
      if (target.scope === "node" && group.host_id === target.host_id) return [index];
      if (target.scope === "device_group" && group.host_id === target.host_id &&
          JSON.stringify(group.devices) === JSON.stringify(target.devices)) return [index];
      return [];
    });
    const queuedRows = await db.prepare(`SELECT q.candidate_group_idxs_json FROM tasks_active a
      LEFT JOIN queue_orders q ON q.task_uid=a.task_uid AND q.worker_id=a.worker_id
        AND q.pool_name=a.target_pool AND q.kind=a.kind
      WHERE a.worker_id=? AND a.target_pool=? AND a.phase='QUEUED'`)
      .bind(target.worker_id, target.pool_name).all<{candidate_group_idxs_json: string | null}>();
    const queued = queuedRows.results.filter(row => {
      if (targetIndexes === null) return true;
      if (!row.candidate_group_idxs_json) return true;
      try {
        const options = JSON.parse(row.candidate_group_idxs_json).options as number[][];
        return !options.length || options.some(bundle => bundle.some(index => targetIndexes.includes(index)));
      } catch {
        return true;
      }
    }).length;
    if (queued > 0) blocking_reasons.push("QUEUED_TASKS_REMAINING");
    if (!blocking_reasons.includes("NODE_QUARANTINED")) {
      const quarantine = target.scope === "pool"
        ? db.prepare(`SELECT COUNT(*) AS n FROM device_group_states
          WHERE worker_id=? AND pool_name=? AND quarantined=1`).bind(target.worker_id, target.pool_name)
        : target.scope === "node"
          ? db.prepare(`SELECT COUNT(*) AS n FROM device_group_states
            WHERE worker_id=? AND pool_name=? AND host_id=? AND quarantined=1`)
            .bind(target.worker_id, target.pool_name, target.host_id)
          : db.prepare(`SELECT COUNT(*) AS n FROM device_group_states
            WHERE worker_id=? AND pool_name=? AND host_id=? AND devices_json=? AND quarantined=1`)
            .bind(target.worker_id, target.pool_name, target.host_id, JSON.stringify(target.devices));
      const quarantined = await quarantine.first<number>("n");
      if ((quarantined ?? 0) > 0) blocking_reasons.push("NODE_QUARANTINED");
    }
  }
  const config_json = JSON.stringify(next);
  if (new TextEncoder().encode(config_json).length > 120000) invalid("configuration too large");
  const sha = await sha256(config_json);
  const edit = {base_revision, affected_groups, affected_combinations, active_leases, drain_cancel_tasks};
  const preview_hash = await hmac(pepper(context), JSON.stringify([sha, edit]));
  const zones = ZONES.filter(zone => {
    if (isDrain && target?.worker_id === zone) return true;
    const owned = (pools: Pools) => Object.entries(pools).filter(([, pool]) => pool.owner === zone);
    return JSON.stringify(owned(old)) !== JSON.stringify(owned(next));
  });
  return {
    preview: {...edit, blocking_reasons, preview_hash}, config_json, sha, zones, drain, rollbackFrom,
    drainTarget: target, drainAction: operationName === "DRAIN" ? "DRAIN" :
      operationName === "RESUME" ? "RESUME" : operationName?.startsWith("DELETE") ? "DELETE" : null,
  };
}

async function publish(context: Context, edit: Edit) {
  const db = context.env.DB;
  const revision = edit.preview.base_revision + 1;
  const actor = context.principal!.username;
  const command_ids = edit.zones.map(() => "cmd_" + crypto.randomUUID());
  const target = edit.drainTarget;
  const drainMutation = target && edit.drainAction === "DRAIN"
    ? db.prepare(`INSERT INTO cluster_drain_targets(target_key,worker_id,scope,pool_name,host_id,devices_json,revision,requested_by,created_at)
      VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET revision=excluded.revision,
      requested_by=excluded.requested_by,created_at=excluded.created_at`)
      .bind(target.target_key, target.worker_id, target.scope, target.pool_name, target.host_id,
        target.devices ? JSON.stringify(target.devices) : null, revision, actor, context.now)
    : target && (edit.drainAction === "RESUME" || edit.drainAction === "DELETE")
      ? db.prepare("DELETE FROM cluster_drain_targets WHERE target_key=?").bind(target.target_key)
      : null;
  const status = await db.batch([
    db.prepare(`INSERT INTO pools_configs(revision,sha256,config_json,diff_summary_json,created_by,created_at,rollback_from_rev)
      VALUES (?,?,?,?,?,?,?)`).bind(revision, edit.sha, edit.config_json, JSON.stringify(edit.preview),
      actor, context.now, edit.rollbackFrom),
    ...edit.zones.map((zone, i) => db.prepare(`
      INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
      VALUES (?,?,'SYNC_CONFIG',?,'PENDING',?,?)`).bind(command_ids[i], zone,
      JSON.stringify({revision, sha256: edit.sha, drain_action: edit.drainAction,
        drain_target: target?.target_key ?? null, drain_cancel_keys: edit.drain.get(zone) ?? []}), actor, context.now)),
    ...(drainMutation ? [drainMutation] : []),
    await auditStatement(context, edit.rollbackFrom ? "ROLLBACK_CLUSTER" : "PUBLISH_CLUSTER", "configuration", String(revision),
      {summary: {sha256: edit.sha, affected_workers: edit.zones}}),
  ]).catch(error => {
    if (String(error).includes("UNIQUE constraint failed: pools_configs.revision")) throw new HttpError(409, "REVISION_CONFLICT");
    throw error;
  });
  if (status[0].meta.changes !== 1) throw new HttpError(409, "REVISION_CONFLICT");
  return {revision, sha256: edit.sha, command_ids};
}

export async function clusterEdit(context: Context, input: Record<string, unknown>, mode: "preview" | "publish" | "rollback") {
  const edit = await plan(context, input, mode === "rollback");
  if (mode === "preview") return edit.preview;
  if (edit.preview.blocking_reasons?.length) {
    throw new HttpError(409, edit.preview.blocking_reasons[0]);
  }
  if (mode === "publish" && !safeHashEqual(edit.preview.preview_hash, typeof input.preview_hash === "string" ? input.preview_hash : undefined)) {
    throw new HttpError(409, "PREVIEW_CHANGED");
  }
  return publish(context, edit);
}

export async function cluster(context: Context) {
  const db = context.env.DB;
  const results = await db.batch([
    db.prepare("SELECT revision,config_json,sha256 FROM pools_configs ORDER BY revision DESC LIMIT 1"),
    db.prepare("SELECT * FROM workers"),
    db.prepare(`SELECT c.worker_id,c.status,c.result_json,c.error_msg,c.payload_json FROM commands c
      WHERE c.cmd_type='SYNC_CONFIG' AND c.ordinal=(SELECT MAX(ordinal) FROM commands
        WHERE worker_id=c.worker_id AND cmd_type='SYNC_CONFIG')`),
    db.prepare("SELECT target_key,worker_id,scope,pool_name,host_id,devices_json,revision FROM cluster_drain_targets"),
    db.prepare("SELECT * FROM device_group_states"),
    db.prepare(`SELECT worker_id,target_pool,allocation_json FROM tasks_active
      WHERE phase IN ('RESERVED','STAGING','RUNNING','CLEANING')`),
    ...(context.principal ? [db.prepare(`SELECT revision,sha256,created_at,created_by,rollback_from_rev
      FROM pools_configs ORDER BY revision DESC LIMIT 50`)] : []),
  ]);
  const revision = results[0].results?.[0] as Revision | undefined;
  const pools = JSON.parse(revision?.config_json ?? "{}") as Pools;
  const workerRows = (results[1].results ?? []) as unknown as WorkerRow[];
  const commands = (results[2].results ?? []) as {worker_id: WorkerId; status: string; result_json: string; error_msg: string; payload_json: string}[];
  const drains = (results[3].results ?? []) as {target_key: string; worker_id: WorkerId; scope: string; pool_name: string;
    host_id: string | null; devices_json: string | null; revision: number}[];
  const observations = (results[4].results ?? []) as unknown as GroupRow[];
  const activeLeases = ((results[5].results ?? []) as Array<{worker_id: WorkerId; target_pool: string; allocation_json: string}>)
    .map(row => ({worker_id: row.worker_id, target_pool: row.target_pool, allocation: JSON.parse(row.allocation_json || "[]") as RawAllocation[]}));
  const probeMap = new Map(observations.map(row => [JSON.stringify([row.worker_id, row.pool_name, row.host_id, row.devices_json]), row]));

  const isPoolDraining = (poolName: string) =>
    drains.some(row => row.scope === "pool" && row.pool_name === poolName);
  const isNodeDraining = (poolName: string, host: string) =>
    isPoolDraining(poolName) || drains.some(row => row.scope === "node" && row.pool_name === poolName && row.host_id === host);
  const isGroupDraining = (poolName: string, host: string, ids: number[]) => {
    const key = JSON.stringify([...ids].sort((a, b) => a - b));
    return isNodeDraining(poolName, host) || drains.some(row =>
      row.scope === "device_group" && row.pool_name === poolName && row.host_id === host && row.devices_json === key);
  };
  const projectGroupMeta = (poolName: string, pool: Pool, node: PoolNode, group: PoolGroup) => {
    const ids = [...group.devices].sort((a, b) => a - b);
    const worker = workerRows.find(row => row.worker_id === pool.owner);
    const observation = probeMap.get(JSON.stringify([pool.owner, poolName, node.host_id, JSON.stringify(ids)]));
    const leased = activeLeases.some(lease => lease.worker_id === pool.owner && lease.target_pool === poolName
      && overlaps(node.host_id, ids, lease.allocation));
    const draining = isGroupDraining(poolName, node.host_id, ids);
    const projected = projectDevice(context.now, {worker, observation, pool, node, group, leased, draining});
    return {
      draining,
      paused: projected.paused,
      paused_scopes: projected.paused_scopes,
      occupancy_state: projected.state,
      quarantined: projected.quarantined,
      health: projected.health,
      last_online_at: projected.health.last_online_at,
      updated_at: projected.updated_at,
    };
  };

  const config = context.principal
    ? Object.fromEntries(Object.entries(pools).map(([poolName, value]) => {
        const nodes = value.nodes.map(node => {
          const allocation_groups = (node.allocation_groups ?? []).map(group => ({
            ...group,
            ...projectGroupMeta(poolName, value, node, group),
          }));
          const nodeHealth = aggregateHealth(allocation_groups.map(group => group.health));
          return {
            ...node,
            draining: isNodeDraining(poolName, node.host_id),
            quarantined: allocation_groups.some(group => group.quarantined),
            health: nodeHealth,
            last_online_at: nodeHealth.last_online_at,
            allocation_groups,
          };
        });
        const poolHealth = aggregateHealth(nodes.map(node => node.health));
        return [poolName, {
          ...value,
          paused: value.enabled === false || Boolean(value.paused),
          draining: isPoolDraining(poolName),
          health: poolHealth,
          last_online_at: poolHealth.last_online_at,
          nodes,
        }];
      }))
    : Object.fromEntries(await Promise.all(Object.entries(pools).map(async ([poolName, value]) => {
        const nodes = await Promise.all(value.nodes.map(async node => {
          const allocation_groups = (node.allocation_groups ?? []).map(group => {
            const meta = projectGroupMeta(poolName, value, node, group);
            return {
              devices: group.devices,
              verified: group.verified,
              paused: meta.paused,
              paused_scopes: meta.paused_scopes,
              draining: meta.draining,
              occupancy_state: meta.occupancy_state,
              quarantined: meta.quarantined,
              health: meta.health,
              last_online_at: meta.last_online_at,
              updated_at: meta.updated_at,
            };
          });
          const nodeHealth = aggregateHealth(allocation_groups.map(group => group.health));
          return {
            ...await nodeIdentity(pepper(context), node.host_id, false),
            paused: Boolean(node.paused),
            draining: isNodeDraining(poolName, node.host_id),
            quarantined: allocation_groups.some(group => group.quarantined),
            health: nodeHealth,
            last_online_at: nodeHealth.last_online_at,
            environments: Object.keys(object(node.environments ?? {}, "environments")),
            capabilities: node.capabilities ?? ["train","npu_smoke"],
            allocation_groups,
          };
        }));
        const poolHealth = aggregateHealth(nodes.map(node => node.health));
        return [poolName, {
          owner: value.owner, type: value.type, enabled: value.enabled,
          paused: value.enabled === false || Boolean(value.paused),
          draining: isPoolDraining(poolName),
          health: poolHealth,
          last_online_at: poolHealth.last_online_at,
          nodes,
        }];
      })));
  return {revision: revision?.revision ?? 0, sha256: revision?.sha256 ?? null, config,
    workers: ZONES.map(worker_id => {
      const worker = workerRows.find(row => row.worker_id === worker_id);
      const command = commands.find(row => row.worker_id === worker_id);
      return {worker_id, applied_rev: worker?.applied_rev ?? 0,
        desired_rev: command ? JSON.parse(command.payload_json).revision : revision?.revision ?? 0,
        code_sha: worker?.code_sha || null,
        last_heartbeat_at: worker?.last_heartbeat_at ?? null,
        offline: !worker || context.now - worker.last_heartbeat_at > 90, status: command?.status ?? null,
        ...(context.principal ? {error_msg: command?.error_msg ?? null} : {}),
      };
    }), ...(context.principal ? {history: results[6]?.results ?? []} : {}),
  };
}
