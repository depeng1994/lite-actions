import type {DeviceGroup, DeviceHealth, WorkerId, WorkerStatus} from "../types/api";

export interface WorkerRow {
  worker_id: WorkerId;
  health_ok: number;
  last_heartbeat_at: number;
  last_idle_probe_at: number | null;
  last_visibility_probe_at?: number | null;
  applied_rev: number;
  active_count: number;
  queued_count: number;
}
export interface GroupRow {
  worker_id: WorkerId;
  pool_name: string;
  host_id: string;
  devices_json: string;
  occupancy_state: "idle" | "busy" | "probe_error" | null;
  probe_at: number | null;
  quarantined: number;
  visibility_state?: "pending" | "online" | "warn" | "offline";
  consecutive_failures?: number;
  last_online_at?: number | null;
  last_probe_round?: number;
  updated_at: number;
}
export interface RawAllocation {
  host_id: string;
  devices: number[];
}
export interface PoolGroup {
  devices: number[];
  verified?: boolean;
  paused?: true;
}
export interface PoolNode {
  host_id: string;
  allocation_groups?: PoolGroup[];
  device_ids?: number[];
  paused?: true;
  machine_fingerprint?: string;
  [key: string]: unknown;
}
export interface Pool {
  owner: WorkerId;
  type: string;
  enabled: boolean;
  paused?: true;
  nodes: PoolNode[];
  default_docker?: string;
  verified_multinode_allocations?: RawAllocation[][];
  [key: string]: unknown;
}
export type Pools = Record<string, Pool>;

export function offline(now: number, worker?: WorkerRow): boolean {
  return !worker || now - worker.last_heartbeat_at > 90;
}

export function workerStatus(now: number, workerId: WorkerId, latest: number, worker?: WorkerRow): WorkerStatus {
  return {
    worker_id: workerId, offline: offline(now, worker),
    healthy: worker?.health_ok === 1 && !offline(now, worker),
    last_heartbeat_at: worker?.last_heartbeat_at ?? null, applied_rev: worker?.applied_rev ?? 0,
    latest_revision: latest, active_count: worker?.active_count ?? 0, queued_count: worker?.queued_count ?? 0,
  };
}

export function overlaps(host: string, ids: number[], allocation: RawAllocation[]): boolean {
  return allocation.some(node => node.host_id === host && node.devices.some(id => ids.includes(id)));
}
export function groups(pool: Pool) {
  return pool.nodes.flatMap(node => (node.allocation_groups ?? []).map(group => ({node, group})));
}

export function aggregateHealth(items: readonly DeviceHealth[]): DeviceHealth {
  if (!items.length) return {status: "PENDING_PROBE", missed_count: 0, last_online_at: null};
  let lastOnline: number | null = null;
  let maxMissed = 0;
  let offlineCount = 0;
  let partialCount = 0;
  let warnCount = 0;
  let pendingCount = 0;
  for (const item of items) {
    if (item.last_online_at != null) lastOnline = lastOnline == null ? item.last_online_at : Math.max(lastOnline, item.last_online_at);
    if (item.missed_count > maxMissed) maxMissed = item.missed_count;
    if (item.status === "OFFLINE") offlineCount += 1;
    else if (item.status === "PARTIAL_OFFLINE") partialCount += 1;
    else if (item.status === "PROBE_WARN") warnCount += 1;
    else if (item.status === "PENDING_PROBE") pendingCount += 1;
  }
  const status: DeviceHealth["status"] = offlineCount === items.length
    ? "OFFLINE"
    : offlineCount > 0 || partialCount > 0
      ? "PARTIAL_OFFLINE"
      : warnCount > 0
        ? "PROBE_WARN"
        : pendingCount > 0
          ? "PENDING_PROBE"
          : "ONLINE";
  return {status, missed_count: maxMissed, last_online_at: lastOnline};
}

export function projectDevice(now: number, input: {
  worker?: WorkerRow;
  observation?: GroupRow;
  pool: Pool;
  node: PoolNode;
  group: PoolGroup;
  leased: boolean;
  draining?: boolean;
}): DeviceGroup {
  const {worker, observation, pool, node, group, leased, draining} = input;
  const fresh = Math.max(observation?.probe_at ?? 0, observation?.occupancy_state === "idle" ? (worker?.last_idle_probe_at ?? 0) : 0);
  const state = leased ? "LEASED" :
    (observation?.occupancy_state === "idle" && fresh > 0 && now - fresh <= 1800 ? "CONFIRMED_IDLE" : "UNKNOWN_BUSY");
  const scopes: DeviceGroup["paused_scopes"] = [];
  if (pool.enabled === false || pool.paused) scopes.push("pool");
  if (node.paused) scopes.push("node");
  if (group.paused) scopes.push("device_group");
  const visibility = observation?.visibility_state ?? "pending";
  return {
    devices: group.devices.slice().sort((a, b) => a - b), state, offline: offline(now, worker),
    quarantined: observation?.quarantined === 1, paused: scopes.length > 0, draining: Boolean(draining),
    paused_scopes: scopes,
    health: {
      status: visibility === "online" ? "ONLINE" : visibility === "warn" ? "PROBE_WARN" : visibility === "offline" ? "OFFLINE" : "PENDING_PROBE",
      missed_count: observation?.consecutive_failures ?? 0,
      last_online_at: observation?.last_online_at ?? null,
    },
    updated_at: Math.max(fresh, observation?.updated_at ?? 0, worker?.last_heartbeat_at ?? 0) || null,
    solid_blocks: [], dashed_blocks: [],
  };
}
