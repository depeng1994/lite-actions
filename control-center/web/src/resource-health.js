import { currentDate } from "./formal-data.js";

function groupHealth(node, group) {
  const probe = node.probes?.[group];
  if (!probe) return { status: "待探测", tone: "gray", lastSeen: null };
  if (probe.health) {
    const { status, missed_count: missed, last_online_at: onlineAt } = probe.health;
    const lastSeen = onlineAt ? new Date(onlineAt * 1000).toLocaleTimeString("zh-CN", {hour12: false}) : null;
    if (status === "OFFLINE") return { status: "已离线", tone: "red", lastSeen, missed };
    if (status === "PROBE_WARN") return { status: `探测异常 ${missed}/5`, tone: "amber", lastSeen, missed };
    if (status === "ONLINE") return { status: "在线", tone: "teal", lastSeen, missed: 0 };
    return { status: "待探测", tone: "gray", lastSeen, missed: 0 };
  }
  const { missed, lastSeen } = probe;
  if (missed >= 5) return { status: "已离线", tone: "red", lastSeen, missed };
  if (missed > 0) return { status: `探测异常 ${missed}/5`, tone: "amber", lastSeen, missed };
  return { status: "在线", tone: "teal", lastSeen };
}

export function resourceHealth(nodes, group) {
  const groups = nodes.flatMap((node) => (group ? [group] : node.groups).map((id) => groupHealth(node, id)));
  if (group && groups.length === 1) return groups[0];
  const lastSeen = groups.flatMap((entry) => entry.lastSeen ?? []).sort().at(-1) ?? null;
  const offline = groups.filter(({ status }) => status === "已离线").length;
  if (groups.length && offline === groups.length) return { status: "已离线", tone: "red", lastSeen };
  if (offline) return { status: "部分离线", tone: "red", lastSeen };
  if (groups.some(({ missed }) => missed > 0)) return { status: "探测异常", tone: "amber", lastSeen };
  if (!groups.length || groups.some(({ status }) => status === "待探测")) return { status: "待探测", tone: "gray", lastSeen };
  return { status: "在线", tone: "teal", lastSeen };
}

export function lastSeenDateTime(time) {
  return time ? `${currentDate} ${time}` : "—";
}
