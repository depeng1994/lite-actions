import { history as liveHistory, repositories as liveRepositories, runs as liveRuns, workflowKey } from "./formal-data.js";

const configuredBase = import.meta.env.VITE_CONTROL_CENTER_API;
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

const minuteOfDay = (timestamp) => {
  if (timestamp == null) return null;
  const date = new Date(timestamp * 1000);
  return date.getHours() * 60 + date.getMinutes() + date.getSeconds() / 60;
};
const workerLabel = (value) => value ? value[0].toUpperCase() + value.slice(1) : "—";
const phaseLabel = (task) => ({
  QUEUED: "排队中", RESERVED: "准备中", STAGING: "准备中", RUNNING: "运行中", CLEANING: "清理中",
}[task.phase] || ({ PASS: "成功", FAIL: "失败", CANCELLED: "已取消", INFRA_ERROR: "失败" }[task.status] || task.status || "排队中"));
const rangeLabel = (devices) => {
  if (!devices?.length) return null;
  const sorted = [...devices].sort((a, b) => a - b);
  return `${sorted[0]}–${sorted.at(-1)}`;
};
const platformLabel = (provider) => provider === "gitcode" ? "GitCode" : "GitHub";

function nodeFromIdentity(identity, worker, pool, deviceGroups = []) {
  const display = identity.display_host_id || identity.host_id || "未知节点";
  const match = display.match(/^(.*):(\d+)$/);
  const groups = deviceGroups.map((group) => rangeLabel(group.devices)).filter(Boolean);
  return {
    id: identity.node_ref || display,
    worker: workerLabel(worker), pool,
    ip: match?.[1] || display, port: Number(match?.[2] || 22),
    cards: deviceGroups.reduce((sum, group) => sum + group.devices.length, 0),
    groups: groups.length ? groups : ["0–7"], binding: "control-center",
    unknown: deviceGroups.filter((group) => group.state === "UNKNOWN_BUSY").map((group) => rangeLabel(group.devices)).filter(Boolean),
    probes: Object.fromEntries(deviceGroups.map((group) => [rangeLabel(group.devices), {
      lastSeen: group.updated_at ? new Date(group.updated_at * 1000).toLocaleTimeString("zh-CN", {hour12: false}) : null,
      missed: group.offline ? 5 : 0,
      health: group.health || null,
    }])),
  };
}

function timelineToNodes(timeline) {
  const nodes = [];
  for (const worker of timeline?.workers || []) {
    for (const pool of worker.pools || []) {
      for (const node of pool.nodes || []) nodes.push(nodeFromIdentity(node, worker.worker_id, pool.pool_name, node.device_groups || []));
    }
  }
  return nodes;
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
      const nodeFlag = pool.draining ? "排空中" : (node.paused ? "暂停" : poolFlag);
      if (nodeFlag) flags[nodeKey] = nodeFlag;
      for (const group of node.allocation_groups || []) {
        const groupKey = rangeLabel(group.devices);
        const groupFlag = pool.draining ? "排空中" : (group.paused ? "暂停" : nodeFlag);
        if (groupFlag) flags[`${nodeKey}/${groupKey}`] = groupFlag;
      }
    }
  }
  const applied = Object.fromEntries((cluster?.workers || []).map((worker) => [workerLabel(worker.worker_id), worker.applied_rev || 0]));
  return {desired: cluster?.revision || 0, applied, flags, pending: null};
}

function clusterPoolNames(cluster) {
  return Object.entries(cluster?.config || {}).map(([pool, value]) => ({pool, worker: workerLabel(value.owner)}));
}

function taskToCase(task, nodesByRef, runKey, isHistory = false, index = 0) {
  const allocation = task.allocation?.[0];
  const node = allocation ? nodesByRef.get(allocation.node_ref) : null;
  const groups = task.allocation?.map((item) => rangeLabel(item.devices)).filter(Boolean) || [];
  const estimate = task.estimated_sec == null ? null : Math.max(1, Math.round(task.estimated_sec / 60));
  return {
    id: task.task_uid || `${runKey}-${task.case_id}`,
    historyId: isHistory ? (task.task_uid || `${runKey}-${task.case_id}`) : undefined,
    run: runKey, name: task.label || task.test_id || task.case_id, status: phaseLabel(task),
    worker: workerLabel(task.worker_id), pool: task.target_pool, nodes: task.req_nodes, cards: task.req_devices,
    node: node?.id, groups, started: minuteOfDay(task.running_at || task.queued_at) ?? 0,
    finished: minuteOfDay(task.finished_at), estimate, samples: task.sample_count || 0,
    position: task.queue_pos == null ? index + 1 : task.queue_pos + 1, plannedNode: node?.id,
    plannedGroups: groups, archived: isHistory,
  };
}

function taskRun(task, model, tasks, isHistory, nodesByRef, index) {
  const run = {
    id: task.run_id, attempt: task.attempt, model, platform: platformLabel(task.provider), repo: task.repo,
    owner: task.repo?.startsWith("depeng1994/") ? "个人仓" : "组织仓", author: task.pr_author,
    actor: task.trigger_actor || "—", pr: task.pr_number, trigger: task.event_type === "pull_request" ? "PR" : task.event_type === "workflow_dispatch" ? "手动" : "事件",
    action: task.workflow_file?.replace(/^.*\//, "").replace(/\.ya?ml$/, "") || "Smoke", commit: task.dispatch_id || "—",
    submitted: minuteOfDay(task.queued_at) ?? 0,
  };
  run.cases = tasks.map((item, caseIndex) => taskToCase(item, nodesByRef, workflowKey(run), isHistory, caseIndex));
  return run;
}

function runsFromModel(model, payload, nodesByRef) {
  const make = (groups, isHistory) => groups.flatMap((group, index) => {
    const first = group.tasks?.[0];
    return first ? taskRun(first, model, group.tasks, isHistory, nodesByRef, index) : [];
  });
  return { active: make(payload?.active || [], false), history: make(payload?.history || [], true) };
}

function runFromWorkflow(row, nodesByRef, archived = false) {
  const run = {
    id: row.run_id, attempt: row.attempt, model: row.logical_model,
    platform: platformLabel(row.provider), repo: row.repo,
    owner: row.repo?.startsWith("depeng1994/") ? "个人仓" : "组织仓",
    author: row.pr_author, actor: row.trigger_actor || "—", pr: row.pr_number,
    trigger: row.event_type === "pull_request" ? "PR" : row.event_type === "workflow_dispatch" ? "手动" : "事件",
    action: row.workflow_name || row.workflow_file?.replace(/^.*\//, "").replace(/\.ya?ml$/, "") || "Workflow",
    commit: "—", submitted: minuteOfDay(row.submitted_at) ?? 0,
    cancelSupported: Boolean(row.cancel_supported),
  };
  run.cases = (row.cases || []).map((task, index) => taskToCase(task, nodesByRef, workflowKey(run), archived, index));
  return run;
}

function applyLiveArrays(snapshot) {
  liveRepositories.splice(0, liveRepositories.length, ...snapshot.repositories);
  liveRuns.splice(0, liveRuns.length, ...snapshot.runs);
  liveHistory.splice(0, liveHistory.length, ...snapshot.history);
}

function nodesFromTimeline(timeline) {
  const nodes = timelineToNodes(timeline);
  return {nodes, nodesByRef: new Map(nodes.map((node) => [node.id, node]))};
}

export async function loadControlCenter(access = "public") {
  try {
    const [overview, timeline, models, cluster, workflowSnapshot] = await Promise.all([
      request(`/${access}/overview`), request(`/${access}/timeline`), request(`/${access}/models`), request(`/${access}/cluster`), request(`/${access}/workflows?limit=50`),
    ]);
    if (!timeline?.workers?.length && !models?.models?.length) return null;
    const modelTabs = models.models || [];
    const {nodes, nodesByRef} = nodesFromTimeline(timeline);
    const activeRuns = workflowSnapshot?.active?.length
      ? workflowSnapshot.active.map((row) => runFromWorkflow(row, nodesByRef))
      : [];
    const historyRuns = workflowSnapshot?.history?.length
      ? workflowSnapshot.history.map((row) => runFromWorkflow(row, nodesByRef, true))
      : [];
    const cases = activeRuns.flatMap((run) => run.cases);
    const repositories = modelTabs.map((model) => ({name: model.slug, sources: (model.repos || []).map((repo) => repo.repo)}));
    const workflowOrder = activeRuns.filter((run) => run.cases.some((item) => item.status === "排队中"))
      .sort((a, b) => a.submitted - b.submitted).map(workflowKey);
    const snapshot = {overview, timeline, cluster, queueVersion: workflowSnapshot?.queue_version || 0,
      historyCursor: workflowSnapshot?.next_cursor || null, historyHasMore: Boolean(workflowSnapshot?.has_more),
      clusterConfig: clusterViewConfig(cluster), clusterPools: clusterPoolNames(cluster), nodes, cases, workflowOrder, repositories, runs: activeRuns, history: historyRuns};
    applyLiveArrays(snapshot);
    return snapshot;
  } catch (error) {
    console.info("控制面 API 暂不可用", error);
    return null;
  }
}

export async function loadMoreHistory(access = "public", cursor = null) {
  if (!cursor) return {runs: [], cursor: null, hasMore: false};
  const [timeline, workflowSnapshot] = await Promise.all([
    request(`/${access}/timeline`), request(`/${access}/workflows?history_limit=50&cursor=${encodeURIComponent(cursor)}`),
  ]);
  const {nodes, nodesByRef} = nodesFromTimeline(timeline);
  const runs = (workflowSnapshot?.history || []).map((row) => runFromWorkflow(row, nodesByRef, true));
  liveHistory.push(...runs);
  return {runs, nodes, cursor: workflowSnapshot?.next_cursor || null, hasMore: Boolean(workflowSnapshot?.has_more)};
}

export async function getSession() { try { return await request("/auth/me"); } catch { return {authenticated: false}; } }
export async function login(username, secret) { return request("/auth/login", {method: "POST", body: {username, secret}}); }
export async function logout() { return request("/auth/logout", {method: "POST"}); }
export async function cancelWorkflow(run) {
  return request("/admin/cancellations", {method: "POST", body: {provider: run.platform === "GitCode" ? "gitcode" : "github", repo: run.repo, run_id: String(run.id), attempt: run.attempt}});
}
export async function moveUp(taskUid) { return request(`/admin/tasks/${encodeURIComponent(taskUid)}/move-up`, {method: "POST", body: {}}); }
export async function moveWorkflowUp(run, queueVersion) {
  return request("/admin/workflows/move-up", {method: "POST", body: {
    provider: run.platform === "GitCode" ? "gitcode" : "github", repo: run.repo,
    run_id: String(run.id), attempt: run.attempt, queue_version: queueVersion,
    idempotency_key: crypto.randomUUID(),
  }});
}
export async function clusterPreview(baseRevision, operation) { return request("/admin/cluster/preview", {method: "POST", body: {base_revision: baseRevision, operation}}); }
export async function clusterPublish(baseRevision, operation, previewHash) { return request("/admin/cluster/publish", {method: "POST", body: {base_revision: baseRevision, operation, preview_hash: previewHash}}); }
export async function listUsers() { return request("/admin/users"); }
export async function createUser(username) { return request("/admin/users", {method: "POST", body: {username}}); }
export async function resetUserKey(userId) { return request(`/admin/users/${encodeURIComponent(userId)}/reset-key`, {method: "POST"}); }
export async function deleteUser(userId) { return request(`/admin/users/${encodeURIComponent(userId)}`, {method: "DELETE"}); }

export const controlApi = {request, getSession, login, logout, cancelWorkflow, moveUp, moveWorkflowUp, clusterPreview, clusterPublish, listUsers, createUser, resetUserKey, deleteUser, loadMoreHistory};
