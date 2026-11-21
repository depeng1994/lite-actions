const current = new Date();
const shanghaiFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
});
const minuteAtShanghai = () => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(current);
  return Number(parts.find(({type}) => type === "hour")?.value || 0) * 60
    + Number(parts.find(({type}) => type === "minute")?.value || 0);
};

export const currentDate = shanghaiFormatter.format(current);
export const dateLabel = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", dateStyle: "full"}).format(current);
export const now = minuteAtShanghai();
export const timelineStart = Math.max(0, now - 50);
export const timelineEnd = Math.min(24 * 60, now + 100);
export const workers = ["Blue", "Yellow"];

// Repository tabs are configuration metadata. Runtime tasks, nodes, accounts and history
// are loaded from the control-center API and intentionally start empty.
export const repositories = [
  {name: "vllm-ascend", sources: []},
  {name: "torchtitan-npu", sources: []},
  {name: "lite-actions", sources: []},
];
export const runs = [];
export const history = [];
export const initialQueueOrder = [];
export const initialCases = [];
export const initialNodes = [];
export const accounts = [];

export const statusTone = {
  "排队中": "amber", "准备中": "blue", "运行中": "teal", "清理中": "amber",
  "成功": "teal", "失败": "red", "已取消": "gray",
};

export function workflowKey({platform, repo, id, attempt}) {
  return JSON.stringify([platform, repo, id, attempt]);
}

export function clock(minute) {
  if (minute == null) return "暂无估算";
  return [Math.floor(minute / 60), Math.floor(minute % 60)]
    .map((part) => String(part).padStart(2, "0")).join(":");
}

export function poolId(worker, pool) {
  return `${worker}/${pool}`;
}

export function targetFor(node, group) {
  const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : node.id);
  return group
    ? {id: `${node.id}/${group}`, label: `${node.id} · 设备组 ${group}`, worker: node.worker, pool: node.pool, node: hostId, group, type: "设备组"}
    : {id: node.id, label: node.id, worker: node.worker, pool: node.pool, node: hostId, type: "节点"};
}

export function flagFor(config, node, group) {
  const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : null);
  return config.flags[poolId(node.worker, node.pool)]
    || config.flags[node.id]
    || (hostId && config.flags[hostId])
    || config.flags[`${node.id}/${group}`]
    || (hostId && config.flags[`${hostId}/${group}`]);
}

export function plannedTime(item, config, nodes) {
  if (item.planned == null) return null;
  const node = nodes.find(({id}) => id === item.plannedNode);
  if (!node || item.plannedGroups.some((group) => flagFor(config, node, group))) return null;
  return item.planned;
}

export function impactOf(target, cases, nodes) {
  const affectedNodes = nodes.filter((node) => {
    const hostId = node.hostId || node.host_id || (node.ip && node.port ? `${node.ip}:${node.port}` : null);
    return node.worker === target.worker && node.pool === target.pool && (!target.node || target.node === node.id || target.node === hostId);
  });
  const leases = cases.filter((item) => item.node && affectedNodes.some((node) => node.id === item.node) && (!target.group || item.groups.includes(target.group)) && ["准备中", "运行中", "清理中"].includes(item.status));
  const queue = cases.filter((item) => item.status === "排队中" && item.worker === target.worker && item.pool === target.pool && (!target.group || !item.plannedGroups || item.plannedGroups.includes(target.group)));
  return {affectedNodes, leases, queue};
}

export const dateFormatter = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short",
});

export function timeFromMinutes(minute) {
  return minute == null ? null : Date.parse(`${currentDate}T00:00:00+08:00`) + minute * 60000;
}
