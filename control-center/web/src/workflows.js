import { beijingClock, timeFromMinutes, poolId, runs, workflowKey } from "./formal-data.js";

export const initialWorkflowHistorySize = 5;
export const workflowHistoryPageSize = 50;

export const terminalStatuses = new Set(["已取消", "成功", "失败"]);
const inFlightStatuses = new Set(["准备中", "运行中", "清理中"]);

export function actionsUrl({ platform, repo, id }) {
  const host = platform === "GitHub" ? "github.com" : "gitcode.com";
  return `https://${host}/${repo.split("/").map(encodeURIComponent).join("/")}/actions/runs/${encodeURIComponent(id)}`;
}

function roundElapsedMinutes(minutes) {
  return Number(Math.max(0, Number(minutes)).toFixed(2));
}

export function computeElapsedMinutes(item, nowMs = Date.now()) {
  if (!item) return null;
  if (item.elapsed != null) {
    return roundElapsedMinutes(item.elapsed);
  }
  if (item.start != null) {
    const endMs = item.end ?? nowMs;
    return roundElapsedMinutes((endMs - item.start) / 60000);
  }
  if (item.started != null) {
    const currentMinute = item.finished ?? beijingClock(new Date(nowMs)).now;
    return roundElapsedMinutes(currentMinute - item.started);
  }
  return null;
}

export function groupActiveRuns(cases, nodes, sourceRuns = runs, nowMs = Date.now()) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  return sourceRuns.map((run) => ({
    ...run,
    cases: cases.filter((item) => item.run === workflowKey(run)).map((item) => {
      const node = byId.get(item.node);
      const computedCards = item.allocatedCards ?? (item.node
        ? (item.groups?.reduce((sum, group) => sum + (node?.groupCards?.[group] ?? 0), 0) || item.cards || 0)
        : 0);
      const elapsed = computeElapsedMinutes(item, nowMs);
      return {
        ...item,
        start: item.start ?? timeFromMinutes(item.started),
        end: item.end ?? timeFromMinutes(item.finished),
        elapsed,
        allocatedCards: computedCards,
        allocation: node ?? item.allocation,
      };
    }),
  }));
}

const RESULT_MAP = {
  QUEUED: "排队中",
  RUNNING: "运行中",
  PASS: "成功",
  FAIL: "失败",
  CANCELLED: "已取消",
  "排队中": "排队中",
  "运行中": "运行中",
  "成功": "成功",
  "失败": "失败",
  "已取消": "已取消",
};

const activeResultLabels = new Set(["排队中", "运行中"]);

export function workflowResultLabel(value) {
  if (!value) return null;
  return RESULT_MAP[value] || null;
}

function activeWorkflowResult(cases, backendResult) {
  if (backendResult === "运行中") return "运行中";
  if (cases.some(({ status }) => status && status !== "排队中")) return "运行中";
  return "排队中";
}

export function summarizeRun(run) {
  const { cases = [] } = run;
  const backendResult = workflowResultLabel(run.resultLabel) || workflowResultLabel(run.result);
  const active = backendResult
    ? activeResultLabels.has(backendResult)
    : typeof run.sequence === "number"
      ? run.sequence >= 0
      : cases.some(({ status }) => !terminalStatuses.has(status));
  const starts = cases.flatMap(({ start }) => start == null ? [] : [start]);
  const ends = cases.flatMap(({ end }) => end == null ? [] : [end]);
  const inferredStarts = cases.flatMap((item) => item.end == null || item.elapsed == null
    ? [] : [item.end - item.elapsed * 60000]);
  const allocated = cases.filter(({ allocatedCards }) => allocatedCards > 0);
  const measured = allocated.filter(({ elapsed }) => elapsed != null);
  return {
    active,
    start: run.startedAt ?? (starts.length ? Math.min(...starts) : inferredStarts.length ? Math.min(...inferredStarts) : null),
    end: run.finishedAt ?? (!active && ends.length ? Math.max(...ends) : null),
    pools: [...new Set(cases.map((item) => {
      // Historical cases may finish without an allocation snapshot. In that
      // case the case's own formal worker/pool identity still defines its
      // resource column.
      const resource = item.allocation ?? (item.historyId && item.node ? item.node : item);
      return poolId(resource.worker, resource.pool);
    }))].sort(),
    maxCards: Math.max(0, ...cases.map(({ cards, allocatedCards }) => allocatedCards || cards)),
    partialCardHours: run.cardHoursPartial ?? (measured.length > 0 && measured.length < allocated.length),
    cardHours: run.cardHours ?? (measured.length ? measured.reduce((sum, item) => sum + item.elapsed * item.allocatedCards, 0) / 60 : null),
    result: active
      ? activeWorkflowResult(cases, backendResult)
      : (backendResult ?? (cases.every(({ status }) => status === "成功") ? "成功" : "失败")),
  };
}

function isQueued(item) {
  return item.status === "排队中"
    && !item.isTest
    && item.kind !== "worker_probe"
    && item.kind !== "test_worker_deploy"
    && item.kind !== "test_worker_cleanup";
}

export function queuedChildren(cases, run) {
  return cases.filter((item) => item.run === workflowKey(run) && isQueued(item));
}

export function currentWorkflowQueue(cases, order) {
  const waiting = new Set(cases.filter(isQueued).map(({ run }) => run));
  const inFlight = new Set(cases.filter(({ status }) => inFlightStatuses.has(status)).map(({ run }) => run));
  return order.filter((key) => waiting.has(key) && !inFlight.has(key));
}

export function moveWorkflow(cases, order, key) {
  const queue = currentWorkflowQueue(cases, order);
  const index = queue.indexOf(key);
  if (index < 1) return null;
  const predecessor = queue[index - 1];
  const nextOrder = order.map((entry) => {
    if (entry === predecessor) return key;
    return entry === key ? predecessor : entry;
  });
  const queues = new Map();
  for (const item of cases.filter(isQueued)) {
    const queueKey = `${poolId(item.worker, item.pool)}/${item.kind ?? "npu_smoke"}`;
    if (!queues.has(queueKey)) queues.set(queueKey, []);
    queues.get(queueKey).push(item);
  }

  const updated = new Map();
  for (const items of queues.values()) {
    if (!items.some(({ run }) => run === key) || !items.some(({ run }) => run === predecessor)) continue;
    const original = [...items].sort((a, b) => a.position - b.position);
    const swapped = [...original.filter(({ run }) => run === key), ...original.filter(({ run }) => run === predecessor)];
    let next = 0;
    const ordered = original.map((item) => item.run === key || item.run === predecessor ? swapped[next++] : item);
    const firstChange = ordered.findIndex((item, offset) => item.id !== original[offset].id);
    ordered.forEach((item, offset) => {
      updated.set(item.id, {
        ...item, position: offset + 1,
        // Existing forecasts are valid only for the original queue prefix.
        planned: firstChange < 0 || offset < firstChange ? item.planned : null,
      });
    });
  }
  return { order: nextOrder, cases: cases.map((item) => updated.get(item.id) ?? item), from: index + 1, to: index };
}
