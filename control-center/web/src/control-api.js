import { deviceRangeLabel, repositories as liveRepositories, runs as liveRuns, timelineRuns as liveTimelineRuns, workflowKey } from "./formal-data.js";
import { computeElapsedMinutes } from "./workflows.js";

const configuredBase = import.meta.env?.VITE_CONTROL_CENTER_API;
const API_BASE = (configuredBase || "").replace(/\/$/, "");

function apiUrl(path) {
  return `${API_BASE}/api/v1${path}`;
}

async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(payload?.message || `控制面请求失败（${response.status}）`);
    error.status = response.status;
    error.code = payload?.code;
    throw error;
  }
  return payload;
}

const shanghaiMinuteParts = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const minuteOfDay = (timestamp) => {
  if (timestamp == null) return null;
  const parts = shanghaiMinuteParts.formatToParts(new Date(timestamp * 1000));
  const hour = Number(parts.find(({type}) => type === "hour")?.value || 0) % 24;
  const minute = Number(parts.find(({type}) => type === "minute")?.value || 0);
  const second = Number(parts.find(({type}) => type === "second")?.value || 0);
  return hour * 60 + minute + second / 60;
};
const timestampMs = (timestamp) => timestamp == null ? null : timestamp * 1000;
const workerLabel = (value) => value ? value[0].toUpperCase() + value.slice(1) : "—";
const phaseLabel = (task) => ({
  QUEUED: "排队中", RESERVED: "准备中", STAGING: "准备中", RUNNING: "运行中", CLEANING: "清理中",
}[task.phase] || ({
  PASS: "成功", FAIL: "失败", TIMED_OUT: "失败", RESOURCE_TIMEOUT: "失败",
  CANCELLED: "已取消", NOT_RUN: "已取消", INFRA_ERROR: "失败",
}[task.status] || task.status || "排队中"));
const resultLabel = (value) => ({ QUEUED: "排队中", RUNNING: "运行中", PASS: "成功", FAIL: "失败" }[value] || value || "排队中");
const rangeLabel = deviceRangeLabel;
const platformLabel = (provider) => provider === "gitcode" ? "GitCode" : "GitHub";

function nodeFromIdentity(identity, worker, pool, deviceGroups = []) {
  const display = identity.host_id || identity.display_host_id || "未知节点";
  const match = display.match(/^(.*):(\d+)$/);
  const groups = deviceGroups.map((group) => rangeLabel(group.devices)).filter(Boolean);
  const groupDeviceIds = Object.fromEntries(deviceGroups.map((group) => [rangeLabel(group.devices), group.devices]));
  const groupCards = Object.fromEntries(Object.entries(groupDeviceIds).map(([group, ids]) => [group, ids.length]));
  const totalCards = deviceGroups.reduce((sum, group) => sum + group.devices.length, 0);
  return {
    id: identity.node_ref || display,
    hostId: identity.host_id || null,
    displayHost: display,
    worker: workerLabel(worker), pool,
    ip: match?.[1] || display, port: Number(match?.[2] || 22),
    cards: totalCards,
    groups,
    groupDeviceIds,
    groupCards,
    binding: "control-center",
    quarantinedGroups: deviceGroups.filter((group) => group.quarantined).map((group) => rangeLabel(group.devices)).filter(Boolean),
    unknown: deviceGroups.filter((group) => (group.state || group.occupancy_state) === "UNKNOWN_BUSY").map((group) => rangeLabel(group.devices)).filter(Boolean),
    leased: deviceGroups.filter((group) => (group.state || group.occupancy_state) === "LEASED").map((group) => rangeLabel(group.devices)).filter(Boolean),
    confirmedIdle: deviceGroups.filter((group) => (group.state || group.occupancy_state) === "CONFIRMED_IDLE").map((group) => rangeLabel(group.devices)).filter(Boolean),
    occupancyBlocks: Object.fromEntries(deviceGroups.map((group) => [rangeLabel(group.devices), group.occupancy_blocks || []])),
    health: identity.health || null,
    probes: Object.fromEntries(deviceGroups.map((group) => [rangeLabel(group.devices), {
      lastSeen: group.health?.last_online_at ? new Date(group.health.last_online_at * 1000).toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour12: false}) : null,
      missed: group.health?.missed_count ?? (group.offline ? 5 : 0),
      health: group.health || null,
    }])),
  };
}

function buildProjectionIndex(timeline) {
  const byTask = new Map();
  for (const projection of timeline?.projections || []) {
    byTask.set(projection.task_uid, {projection, nodeId: null, groups: []});
  }
  for (const worker of timeline?.workers || []) {
    for (const pool of worker.pools || []) {
      for (const node of pool.nodes || []) {
        const nodeKey = node.node_ref || node.host_id || node.display_host_id;
        for (const group of node.device_groups || []) {
          const label = rangeLabel(group.devices);
          for (const block of group.dashed_blocks || []) {
            const entry = byTask.get(block.task_uid) || {projection: block, nodeId: null, groups: []};
            entry.projection = block;
            entry.nodeId ??= nodeKey;
            if (label && !entry.groups.includes(label)) entry.groups.push(label);
            byTask.set(block.task_uid, entry);
          }
        }
      }
    }
  }
  return byTask;
}

export function resourceInventories(timeline, cluster) {
  const nodes = [];
  const appliedByNode = new Map();
  const nodeKey = (worker, pool, identity) => JSON.stringify([worker, pool, identity.host_id ?? identity.node_ref]);
  const groupKey = (group) => JSON.stringify([...group.devices].sort((a, b) => a - b));
  for (const worker of timeline?.workers || []) {
    for (const pool of worker.pools || []) {
      for (const node of pool.nodes || []) {
        const entry = nodeFromIdentity(node, worker.worker_id, pool.pool_name, node.device_groups || []);
        appliedByNode.set(nodeKey(worker.worker_id, pool.pool_name, node), {
          id: entry.id,
          groups: new Map((node.device_groups || []).map((group) => [groupKey(group), group.occupancy_blocks])),
        });
        nodes.push(entry);
      }
    }
  }
  const clusterNodes = [];
  for (const [poolName, pool] of Object.entries(cluster?.config || {})) {
    for (const node of pool.nodes || []) {
      // The authenticated cluster response supplies host_id, while the timeline uses node_ref for task allocations.
      const applied = appliedByNode.get(nodeKey(pool.owner, poolName, node));
      const node_ref = node.node_ref || applied?.id;
      const groups = (node.allocation_groups || []).map((group) => ({
        ...group, occupancy_blocks: group.occupancy_blocks ?? applied?.groups.get(groupKey(group)),
      }));
      clusterNodes.push(nodeFromIdentity({...node, node_ref}, pool.owner, poolName, groups));
    }
  }
  return {nodes, clusterNodes, nodesByRef: new Map(nodes.map((node) => [node.id, node]))};
}

function clusterViewConfig(cluster) {
  const flags = {};
  for (const [poolName, pool] of Object.entries(cluster?.config || {})) {
    const worker = workerLabel(pool.owner);
    const poolKey = `${worker}/${poolName}`;
    const poolFlag = pool.draining ? "排空中" : (pool.enabled === false || pool.paused ? "暂停" : null);
    if (poolFlag) flags[poolKey] = poolFlag;
    for (const node of pool.nodes || []) {
      const nodeKey = node.node_ref || node.host_id || node.display_host_id;
      const nodeFlag = node.draining ? "排空中" : (node.paused ? "暂停" : poolFlag);
      if (nodeFlag) flags[nodeKey] = nodeFlag;
      if (node.host_id && nodeFlag) flags[node.host_id] = nodeFlag;
      for (const group of node.allocation_groups || []) {
        const groupKey = rangeLabel(group.devices);
        const groupFlag = group.draining ? "排空中" : (group.paused ? "暂停" : nodeFlag);
        if (groupFlag && groupKey) {
          flags[`${nodeKey}/${groupKey}`] = groupFlag;
          if (node.host_id) flags[`${node.host_id}/${groupKey}`] = groupFlag;
        }
      }
    }
  }
  const applied = Object.fromEntries((cluster?.workers || []).map((worker) => [workerLabel(worker.worker_id), worker.applied_rev || 0]));
  const workerStatus = Object.fromEntries((cluster?.workers || []).map((worker) => [workerLabel(worker.worker_id), {
    offline: Boolean(worker.offline), status: worker.status, error: worker.error_msg ?? null,
    desired: worker.desired_rev ?? cluster?.revision ?? 0,
    lastHeartbeatAt: worker.last_heartbeat_at ?? null,
  }]));
  return {desired: cluster?.revision || 0, applied, flags, workerStatus, pending: null};
}

function clusterPoolNames(cluster) {
  return Object.entries(cluster?.config || {}).map(([pool, value]) => ({pool, worker: workerLabel(value.owner)}));
}

function taskToCase(task, nodesByRef, projectionIndex, runKey, isHistory = false, index = 0) {
  const allocations = task.allocation || [];
  const allocation = allocations[0];
  const displayAllocHost = allocation ? (allocation.host_id || allocation.display_host_id || "未知节点") : null;
  const node = allocation ? nodesByRef.get(allocation.node_ref) || {
    id: allocation.node_ref || displayAllocHost,
    displayHost: displayAllocHost,
    hostId: allocation.host_id || null,
    worker: workerLabel(task.worker_id), pool: task.target_pool,
    ip: (displayAllocHost || "未知节点").replace(/:\d+$/, ""),
    port: Number((displayAllocHost || "").match(/:(\d+)$/)?.[1] || 22),
    cards: allocation.devices?.length || task.allocated_cards || task.req_devices,
  } : null;
  const groups = task.allocation?.map((item) => rangeLabel(item.devices)).filter(Boolean) || [];
  const estimate = task.estimated_sec == null ? null : Math.max(1, Math.round(task.estimated_sec / 60));
  const elapsed = task.duration_sec == null ? null : computeElapsedMinutes({ elapsed: task.duration_sec / 60 });
  const projected = projectionIndex?.get(task.task_uid);
  const projection = projected?.projection;
  const scheduled = projection?.projection_status === "SCHEDULED";
  const planned = scheduled ? minuteOfDay(projection.est_start) : null;
  const finished = isHistory || (task.phase == null && task.finished_at != null);
  return {
    id: task.task_uid || `${runKey}-${task.case_id}`,
    taskUid: task.task_uid || null,
    historyId: finished ? (task.task_uid || `${runKey}-${task.case_id}`) : undefined,
    run: runKey, name: task.label || task.test_id || task.case_id, status: phaseLabel(task),
    kind: task.kind,
    reporterId: task.reporter_id || task.worker_id,
    isTest: Boolean(task.is_test),
    worker: workerLabel(task.worker_id), pool: task.target_pool, nodes: task.req_nodes,
    cards: task.allocated_cards || task.req_nodes * task.req_devices || task.req_devices,
    allocatedCards: task.allocated_cards ?? 0,
    node: node?.id, allocation: node, allocations, groups,
    started: minuteOfDay(task.started_at), finished: minuteOfDay(task.finished_at),
    timelineStarted: task.started_at == null ? null : task.started_at / 60,
    timelineFinished: task.finished_at == null ? null : task.finished_at / 60,
    start: task.started_at == null ? null : task.started_at * 1000,
    end: task.finished_at == null ? null : task.finished_at * 1000,
    elapsed, estimate, samples: task.sample_count || 0,
    estimateStatus: task.estimate_status || "NO_ESTIMATE",
    position: task.queue_pos == null ? index + 1 : task.queue_pos + 1,
    planned,
    timelinePlanned: scheduled && projection.est_start != null ? projection.est_start / 60 : null,
    timelinePlannedEnd: scheduled && projection.est_end != null ? projection.est_end / 60 : null,
    projectionStatus: projection?.projection_status || null,
    plannedNode: projected?.nodeId || node?.id || null,
    plannedGroups: projected?.groups?.length ? projected.groups : groups,
    storageTier: task.storage_tier || null,
    runUrl: task.run_url || null,
    archived: isHistory || task.storage_tier === "cold",
  };
}

function workflowMetadata(row) {
  return {
    id: row.run_id, number: row.run_number, title: row.display_title, attempt: row.attempt, model: row.logical_model,
    isTest: Boolean(row.is_test), reporterIds: row.reporter_ids || [],
    platform: platformLabel(row.provider), repo: row.repo,
    owner: row.repo_scope || (row.repo?.startsWith("depeng1994/") ? "个人仓" : "组织仓"),
    author: row.pr_author, actor: row.trigger_actor || "—", pr: row.pr_number,
    trigger: row.event_type === "pull_request" ? "PR" : row.event_type === "workflow_dispatch" ? "手动" : "事件",
    action: row.workflow_name || row.workflow_file?.replace(/^.*\//, "").replace(/\.ya?ml$/, "") || "Workflow",
    commit: "—", submitted: timestampMs(row.submitted_at),
  };
}

function runFromWorkflow(row, nodesByRef, projectionIndex, archived = false) {
  const run = {
    ...workflowMetadata(row),
    startedAt: timestampMs(row.started_at), finishedAt: timestampMs(row.finished_at),
    sequence: row.sequence,
    result: row.result || null,
    resultLabel: resultLabel(row.result),
    cardHours: row.card_hours == null ? null : Number(row.card_hours),
    cardHoursAsOfNow: Boolean(row.card_hours_as_of_now),
    cardHoursPartial: Boolean(row.card_hours_partial),
    cancelSupported: Boolean(row.cancel_supported),
    canMoveUp: Boolean(row.can_move_up),
  };
  run.cases = (row.cases || []).map((task, index) => taskToCase(task, nodesByRef, projectionIndex, workflowKey(run), archived, index));
  return run;
}

function groupTimelineTasks(tasks, nodesByRef) {
  const groups = new Map();
  for (const task of tasks) {
    const platform = platformLabel(task.provider);
    const key = workflowKey({platform, repo: task.repo, id: task.run_id, attempt: task.attempt, isTest: Boolean(task.is_test)});
    let run = groups.get(key);
    if (!run) {
      run = {
        ...workflowMetadata(task),
        startedAt: null, finishedAt: null, sequence: -1, result: "成功", resultLabel: "成功",
        cardHours: null, cardHoursAsOfNow: false, cardHoursPartial: false, cancelSupported: false, canMoveUp: false, cases: [],
      };
      groups.set(key, run);
    }
    const item = taskToCase(task, nodesByRef, null, key, true, run.cases.length);
    run.cases.push(item);
    if (item.start != null && (run.startedAt == null || item.start < run.startedAt)) run.startedAt = item.start;
    if (item.end != null && (run.finishedAt == null || item.end > run.finishedAt)) run.finishedAt = item.end;
    if (item.status !== "成功") {
      run.result = "失败";
      run.resultLabel = "失败";
    }
  }
  return [...groups.values()].sort((left, right) => (right.finishedAt || 0) - (left.finishedAt || 0));
}

function applyLiveActive(snapshot) {
  liveRepositories.splice(0, liveRepositories.length, ...snapshot.repositories);
  liveRuns.splice(0, liveRuns.length, ...snapshot.runs);
  liveTimelineRuns.splice(0, liveTimelineRuns.length, ...snapshot.timelineRuns);
}

export async function loadControlCenter(access = "public") {
  try {
    const [overview, timeline, models, cluster, workflowSnapshot] = await Promise.all([
      request(`/${access}/overview`),
      request(`/${access}/timeline?history_hours=72&compact_history=1`),
      request(`/${access}/models`),
      request(`/${access}/cluster`),
      request(`/${access}/workflows?history_limit=0`),
    ]);
    if (!timeline?.workers?.length && !models?.models?.length) return null;
    const modelTabs = models.models || [];
    const {nodes, clusterNodes, nodesByRef} = resourceInventories(timeline, cluster);
    const projectionIndex = buildProjectionIndex(timeline);
    const activeRuns = workflowSnapshot?.active?.length
      ? workflowSnapshot.active.map((row) => runFromWorkflow(row, nodesByRef, projectionIndex))
      : [];
    const cases = activeRuns.flatMap((run) => run.cases);
    const caseIds = new Set(cases.map(({id}) => id));
    const historyTasks = timeline.history_task_uids?.map((uid) => timeline.tasks_by_uid?.[uid]).filter(Boolean)
      ?? timeline.history_tasks ?? [];
    const timelineHistoryRuns = groupTimelineTasks(historyTasks.filter((task) => !caseIds.has(task.task_uid)), nodesByRef);
    const timelineCases = [...cases, ...timelineHistoryRuns.flatMap((run) => run.cases)];
    const repositories = modelTabs.map((model) => ({name: model.slug, sources: (model.repos || []).map((repo) => repo.repo)}));
    const workflowOrder = activeRuns
      .filter((run) => (run.sequence ?? 0) > 0)
      .sort((a, b) => a.sequence - b.sequence)
      .map(workflowKey);
    const snapshot = {
      overview, timeline, cluster,
      queueVersion: workflowSnapshot?.queue_version || 0,
      truncatedByCapacity: Boolean(workflowSnapshot?.truncated_by_capacity),
      purgedNotice: workflowSnapshot?.purged_notice || null,
      clusterConfig: clusterViewConfig(cluster),
      clusterHistory: cluster?.history || [],
      clusterPools: clusterPoolNames(cluster),
      nodes, clusterNodes, cases, timelineCases, timelineRuns: [...activeRuns, ...timelineHistoryRuns], workflowOrder, repositories, runs: activeRuns,
    };
    applyLiveActive(snapshot);
    return snapshot;
  } catch (error) {
    console.info("控制面 API 暂不可用", error);
    return null;
  }
}

export async function loadMoreHistory(access, {cursor, limit, signal}) {
  const query = new URLSearchParams({status: "history", history_limit: limit});
  if (cursor) query.set("cursor", cursor);
  const [timeline, workflowSnapshot] = await Promise.all([
    request(`/${access}/timeline`, {signal}),
    request(`/${access}/workflows?${query}`, {signal}),
  ]);
  const {nodesByRef} = resourceInventories(timeline);
  const runs = (workflowSnapshot?.history || []).map((row) => runFromWorkflow(row, nodesByRef, null, true));
  return {
    runs,
    cursor: workflowSnapshot?.next_cursor || null,
    hasMore: Boolean(workflowSnapshot?.has_more),
    historyTotal: workflowSnapshot?.history_total ?? runs.length,
    truncatedByCapacity: Boolean(workflowSnapshot?.truncated_by_capacity),
    purgedNotice: workflowSnapshot?.purged_notice || null,
  };
}

export async function getTaskDetail(access = "public", taskUid) {
  return request(`/${access}/tasks/${encodeURIComponent(taskUid)}`);
}
export async function getSession() { try { return await request("/auth/me"); } catch { return {authenticated: false}; } }
export async function login(username, secret) { return request("/auth/login", {method: "POST", body: {username, secret}}); }
export async function logout() { return request("/auth/logout", {method: "POST"}); }
export async function cancelWorkflow(run) {
  return request("/admin/cancellations", {method: "POST", body: {provider: run.platform === "GitCode" ? "gitcode" : "github", repo: run.repo, run_id: String(run.id), attempt: run.attempt}});
}
export async function cancellationDetail(cancelId) { return request(`/admin/cancellations/${encodeURIComponent(cancelId)}`); }
export async function moveWorkflowUp(run, queueVersion) {
  return request("/admin/workflows/move-up", {method: "POST", body: {
    provider: run.platform === "GitCode" ? "gitcode" : "github", repo: run.repo,
    run_id: String(run.id), attempt: run.attempt, queue_version: queueVersion,
    idempotency_key: crypto.randomUUID(),
  }});
}
export async function priorityOpDetail(opId) { return request(`/admin/workflow-priority-ops/${encodeURIComponent(opId)}`); }
export async function listAudits(limit = 50) { return request(`/admin/audits?limit=${limit}`); }
export async function clusterPreview(baseRevision, operation) { return request("/admin/cluster/preview", {method: "POST", body: {base_revision: baseRevision, operation}}); }
export async function clusterPublish(baseRevision, operation, previewHash) { return request("/admin/cluster/publish", {method: "POST", body: {base_revision: baseRevision, operation, preview_hash: previewHash}}); }
export async function clusterRollback(baseRevision, targetRevision) { return request("/admin/cluster/rollback", {method: "POST", body: {base_revision: baseRevision, target_revision: targetRevision}}); }
export async function listUsers() { return request("/admin/users"); }
export async function createUser(username) { return request("/admin/users", {method: "POST", body: {username}}); }
export async function resetUserKey(userId) { return request(`/admin/users/${encodeURIComponent(userId)}/reset-key`, {method: "POST"}); }
export async function deleteUser(userId) { return request(`/admin/users/${encodeURIComponent(userId)}`, {method: "DELETE"}); }

export const controlApi = {
  request, getTaskDetail, getSession, login, logout, cancelWorkflow, cancellationDetail,
  moveWorkflowUp, priorityOpDetail, listAudits, clusterPreview, clusterPublish, clusterRollback,
  listUsers, createUser, resetUserKey, deleteUser, loadMoreHistory,
};
