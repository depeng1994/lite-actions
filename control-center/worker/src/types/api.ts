export type WorkerId = "blue" | "yellow";
export type Provider = "github" | "gitcode";
export type Role = "admin" | "super";
export type Phase = "QUEUED" | "RESERVED" | "STAGING" | "RUNNING" | "CLEANING";
export type CaseKind = "train" | "npu_smoke" | "worker_probe";
export type Occupancy = "LEASED" | "UNKNOWN_BUSY" | "CONFIRMED_IDLE";
export type CommandStatus = "PENDING" | "ACKED" | "APPLIED" | "NOOP" | "CONFLICT" | "FAILED" | "EXPIRED";

export interface ApiError {
  code: string;
  message: string;
}
export type Session = {authenticated: false} | {authenticated: true; username: string; role: Role};
export interface User {
  user_id: string;
  username: string;
  created_at: number;
  updated_at: number;
}
export interface IssuedKey {
  user_id: string;
  username: string;
  secret_once: string;
}
export interface RunIdentity {
  provider: Provider;
  repo: string;
  run_id: string;
  attempt: number;
}
export interface NodeIdentity {
  node_ref: string;
  display_host_id: string;
  host_id?: string;
}
export interface Allocation extends NodeIdentity {
  devices: number[];
}
export type Projection = {
  task_uid: string;
  projection_status: "SCHEDULED";
  est_start: number;
  est_end: number;
  uncertain_release: boolean;
} | {
  task_uid: string;
  projection_status: "NO_ESTIMATE" | "WAITING_EXTERNAL";
};
export interface SolidBlock {
  task_uid: string;
  start_at: number;
  group_count: number;
  is_primary_row: boolean;
  total_cards: number;
}
export interface DeviceGroup {
  devices: number[];
  state: Occupancy;
  offline: boolean;
  quarantined: boolean;
  paused: boolean;
  paused_scopes: ("pool" | "node" | "device_group")[];
  updated_at: number | null;
  health: {status: DeviceHealthStatus; missed_count: number; last_online_at: number | null};
  solid_blocks: SolidBlock[];
  dashed_blocks: Projection[];
}
export interface WorkerStatus {
  worker_id: WorkerId;
  offline: boolean;
  healthy: boolean;
  last_heartbeat_at: number | null;
  applied_rev: number;
  latest_revision: number;
  active_count: number;
  queued_count: number;
}
export interface Overview {
  workers: WorkerStatus[];
  active_count: number;
  queued_count: number;
  page_refresh_sec: 30;
}
export interface Timeline {
  workers: (WorkerStatus & {
    pools: {
      pool_name: string;
      type: string;
      enabled: boolean;
      paused: boolean;
      nodes: (NodeIdentity & {device_groups: DeviceGroup[]})[];
    }[];
  })[];
  zero_card_tasks: TaskSummary[];
  projections: Projection[];
  tasks_by_uid: Record<string, TaskSummary>;
  queued_by_pool: Record<string, TaskSummary[]>;
  now: number;
}
export interface ModelRepo {
  provider: Provider;
  repo: string;
  cancel_supported: boolean;
  repo_scope?: "组织仓" | "个人仓";
}
export interface ModelTab {
  slug: string;
  display_name: string;
  default: boolean;
  active_count: number;
  repos: ModelRepo[];
}
export interface TaskSummary extends RunIdentity {
  task_uid: string;
  worker_id: WorkerId;
  logical_model: string;
  dispatch_id: string | null;
  run_number: number | null;
  case_id: string;
  test_id: string;
  label: string;
  kind: CaseKind;
  target_pool: string;
  req_nodes: number;
  req_devices: number;
  pr_number: number | null;
  pr_author: string | null;
  trigger_actor: string | null;
  event_type: string | null;
  workflow_file: string | null;
  phase: Phase | null;
  status: string | null;
  queued_at: number | null;
  finished_at: number | null;
  allocation: Allocation[];
  estimated_sec: number | null;
  sample_count: number;
  estimate_status: "ESTIMATED" | "NO_ESTIMATE" | "EXCEEDED";
  cancel_supported: boolean;
  can_move_up: boolean;
  queue_pos: number | null;
  duration_sec?: number | null;
  submitted_at: number | null;
  workflow_name: string | null;
  started_at: number | null;
  allocated_cards: number;
  storage_tier?: "hot" | "cold" | null;
  run_url: string;
}
export interface TaskDetail extends TaskSummary {
  reserved_at: number | null;
  staging_at: number | null;
  running_at: number | null;
  cleaning_at: number | null;
  duration_sec: number | null;
  timing_reliable: boolean;
  exit_code: number | null;
  cleanup_verified: boolean | null;
}
export interface RunGroup extends RunIdentity {
  tasks: TaskSummary[];
}
export interface ModelRuns {
  active: RunGroup[];
  history: RunGroup[];
  next_cursor: string | null;
  has_more: boolean;
  truncated_by_capacity: boolean;
  purged_notice: string | null;
}
export type ClusterScope = "pool" | "node" | "device_group";
export type ClusterOperation =
  | { op: "PAUSE" | "RESUME"; scope: ClusterScope; pool: string; host_id?: string; devices?: number[] }
  | { op: "ADD_POOL" | "ADD_NODE" | "ADD_DEVICE_GROUP"; pool: string; host_id?: string; value: unknown }
  | { op: "DELETE_POOL" | "DELETE_NODE" | "DELETE_DEVICE_GROUP"; pool: string; host_id?: string; devices?: number[] };
export interface ClusterPreview {
  base_revision: number;
  preview_hash: string;
  affected_groups: {worker_id: WorkerId; pool_name: string; host_id: string; devices: number[]}[];
  drain_cancel_tasks: string[];
  active_leases: string[];
  blocking_reasons?: string[];
}
export interface ClusterSync {
  revision: number;
  sha256: string;
  command_ids: string[];
}
export type CancellationStatus =
  "SUBMITTED" | "FORCE_CANCEL_PENDING" | "PLATFORM_DONE" | "COMPLETED" | "COMPLETED_WITH_QUARANTINE" | "FAILED";
export interface Cancellation extends RunIdentity {
  cancel_id: string;
  status: CancellationStatus;
  created_at: number;
  submitted_at: number | null;
  force_cancel_sent_at: number | null;
  platform_conclusion: string | null;
  platform_error: string | null;
}

export type DeviceHealthStatus = "PENDING_PROBE" | "ONLINE" | "PROBE_WARN" | "OFFLINE" | "PARTIAL_OFFLINE";
export interface DeviceHealth {
  status: DeviceHealthStatus;
  missed_count: number;
  last_online_at: number | null;
}

export type WorkflowResult = "QUEUED" | "PASS" | "FAIL";
export interface WorkflowRow extends RunIdentity {
  sequence: number;
  submitted_at: number | null;
  run_number: number | null;
  repo_scope?: "组织仓" | "个人仓";
  logical_model: string;
  workflow_name: string | null;
  workflow_file: string | null;
  pools: string[];
  max_cards: number;
  result: WorkflowResult;
  trigger_actor: string | null;
  event_type: string | null;
  pr_number: number | null;
  pr_author: string | null;
  started_at: number | null;
  finished_at: number | null;
  card_hours: number | null;
  card_hours_as_of_now: boolean;
  card_hours_partial: boolean;
  cancel_supported: boolean;
  can_move_up: boolean;
  active_cancel_id?: string;
  cases: TaskSummary[];
}
export interface WorkflowList {
  queue_version: number;
  active: WorkflowRow[];
  history: WorkflowRow[];
  history_total: number;
  next_cursor: string | null;
  has_more: boolean;
  truncated_by_capacity: boolean;
  purged_notice: string | null;
}
export interface WorkflowMoveUpRequest {
  provider: Provider;
  repo: string;
  run_id: string;
  attempt: number;
  queue_version: number;
  idempotency_key?: string;
}
export interface WorkflowPriorityOp {
  op_id: string;
  queue_version: number;
  status: "PENDING" | "NOOP" | "APPLIED" | "CONFLICT" | "FAILED";
}
