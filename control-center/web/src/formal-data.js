const shanghaiDateFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
});
const shanghaiLabelFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", dateStyle: "full",
});
const shanghaiTimeParts = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
});
const timelineDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const timelineDateTimeWithSeconds = new Intl.DateTimeFormat("en-US", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

export function beijingClock(date = new Date()) {
  const parts = shanghaiTimeParts.formatToParts(date);
  const hour = Number(parts.find(({type}) => type === "hour")?.value || 0) % 24;
  const minute = Number(parts.find(({type}) => type === "minute")?.value || 0);
  const nowMinute = hour * 60 + minute;
  return {
    currentDate: shanghaiDateFormatter.format(date),
    dateLabel: shanghaiLabelFormatter.format(date),
    now: nowMinute,
    timelineStart: Math.max(0, nowMinute - 50),
    timelineEnd: Math.min(24 * 60, nowMinute + 100),
  };
}

export function timelineClock(date = new Date()) {
  const nowMs = date.getTime();
  const now = Math.floor(nowMs / 60000);
  return {
    dateLabel: shanghaiLabelFormatter.format(date),
    now,
    nowMs,
    timelineStart: now - 72 * 60,
    timelineEnd: now + 60,
  };
}

const initialClock = beijingClock();
export const currentDate = initialClock.currentDate;
export const dateLabel = initialClock.dateLabel;
export const now = initialClock.now;
export const timelineStart = initialClock.timelineStart;
export const timelineEnd = initialClock.timelineEnd;
export const workers = ["Blue", "Yellow"];

// Repository tabs are configuration metadata. Runtime tasks, nodes, accounts and history
// are loaded from the control-center API and intentionally start empty.
export const repositories = [
  {name: "vllm-ascend", sources: []},
  {name: "torchtitan-npu", sources: []},
  {name: "lite-actions", sources: []},
];
export const runs = [];
export const timelineRuns = [];
export const initialQueueOrder = [];
export const initialCases = [];
export const initialNodes = [];
export const accounts = [];

export const statusTone = {
  "排队中": "amber", "准备中": "blue", "运行中": "teal", "清理中": "amber",
  "成功": "teal", "失败": "red", "已取消": "gray",
};

export function workflowKey({platform, repo, id, attempt, isTest}) {
  return isTest ? JSON.stringify([platform, repo, id, attempt, 1]) : JSON.stringify([platform, repo, id, attempt]);
}

export function clock(minute) {
  if (minute == null) return "暂无估算";
  const normalized = ((Math.floor(minute) % 1440) + 1440) % 1440;
  return [Math.floor(normalized / 60), normalized % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
}

export function timelineLabel(minute, withSeconds = false) {
  if (minute == null) return "暂无估算";
  const formatter = withSeconds ? timelineDateTimeWithSeconds : timelineDateTime;
  const parts = Object.fromEntries(formatter.formatToParts(new Date(Math.round(minute * 60000)))
    .map(({type, value}) => [type, value]));
  const dateTime = `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  return withSeconds ? `${dateTime}:${parts.second}` : dateTime;
}

export function poolId(worker, pool) {
  return `${worker}/${pool}`;
}

export function displayNodeHost(node) {
  if (!node) return null;
  if (typeof node === "string") return node;
  return node.displayHost || node.hostId || node.host_id || node.display_host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : node.id || node.node_ref || null);
}

export function deviceRangeLabel(devices) {
  if (!devices?.length) return null;
  const sorted = [...devices].sort((a, b) => a - b);
  return `${sorted[0]}–${sorted.at(-1)}`;
}

export function allocationEntries(item, detail = null, nodes = []) {
  const raw = detail?.allocation?.length
    ? detail.allocation
    : item?.allocations?.length
      ? item.allocations
      : null;
  if (raw) {
    return raw.map((alloc) => {
      const matched = nodes.find((node) => node.id === alloc.node_ref || (alloc.host_id && node.hostId === alloc.host_id));
      const host = displayNodeHost(alloc) || (matched && displayNodeHost(matched)) || "未知节点";
      const sorted = alloc.devices?.length ? [...alloc.devices].sort((a, b) => a - b) : [];
      return {
        host,
        group: deviceRangeLabel(sorted),
        devicesText: sorted.length ? sorted.join(",") : null,
      };
    });
  }
  const fallback = item?.allocation || (item?.historyId ? item?.node : null);
  if (!fallback) return [];
  const resolved = typeof fallback === "string"
    ? (nodes.find((node) => node.id === fallback) || fallback)
    : fallback;
  const host = displayNodeHost(resolved);
  if (!host) return [];
  const group = (item?.groups || []).filter(Boolean).join("、") || null;
  return [{ host, group, devicesText: group }];
}

export function formatAllocation(item, detail = null, nodes = []) {
  const entries = allocationEntries(item, detail, nodes);
  if (!entries.length) return null;
  return entries.map(({ host, group }) => (group ? `${host} ${group}` : host)).join("、");
}

export function targetFor(node, group) {
  const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : node.id);
  const labelHost = displayNodeHost(node);
  return group
    ? {id: `${node.id}/${group}`, label: `${labelHost} · 设备组 ${group}`, worker: node.worker, pool: node.pool, node: hostId, nodeId: node.id, group, type: "设备组"}
    : {id: node.id, label: labelHost, worker: node.worker, pool: node.pool, node: hostId, nodeId: node.id, type: "节点"};
}

export function flagFor(config, node, group) {
  const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : null);
  return config.flags[poolId(node.worker, node.pool)]
    || config.flags[node.id]
    || (hostId && config.flags[hostId])
    || (group ? config.flags[`${node.id}/${group}`] : null)
    || (group && hostId ? config.flags[`${hostId}/${group}`] : null);
}

export function plannedTime(item, config, nodes) {
  if (item.projectionStatus === "NO_ESTIMATE" || item.projectionStatus === "WAITING_EXTERNAL") return null;
  if (item.planned == null) return null;
  if (!item.plannedNode) return item.planned;
  const node = nodes.find(({id}) => id === item.plannedNode);
  if (!node || item.plannedGroups?.some((group) => flagFor(config, node, group))) return null;
  return item.planned;
}

export function impactOf(target, cases, nodes) {
  const affectedNodes = nodes.filter((node) => {
    const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : null);
    return node.worker === target.worker && node.pool === target.pool && (!target.node || target.node === node.id || target.node === hostId || target.nodeId === node.id);
  });
  const leases = cases.filter((item) => {
    if (!["准备中", "运行中", "清理中"].includes(item.status)) return false;
    if (item.allocations?.length) {
      return item.allocations.some((alloc) => affectedNodes.some((node) => node.id === alloc.node_ref || (node.hostId && node.hostId === alloc.host_id))
        && (!target.group || deviceRangeLabel(alloc.devices) === target.group));
    }
    return item.node && affectedNodes.some((node) => node.id === item.node) && (!target.group || item.groups.includes(target.group));
  });
  const queue = cases.filter((item) => item.status === "排队中" && item.worker === target.worker && item.pool === target.pool && (!target.group || !item.plannedGroups?.length || item.plannedGroups.includes(target.group)));
  return {affectedNodes, leases, queue};
}

export const dateFormatter = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short",
});

export const dateTimeSecondFormatter = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium",
});

export function timeFromMinutes(minute) {
  return minute == null ? null : Date.parse(`${beijingClock().currentDate}T00:00:00+08:00`) + minute * 60000;
}
