const COMMAND_STATUS_LABELS = {
  PENDING: "待受理",
  ACKED: "已受理",
  APPLIED: "已应用",
  PARTIAL_APPLIED: "部分生效",
  NOOP: "无变化",
  CONFLICT: "冲突",
  FAILED: "失败",
  EXPIRED: "已过期",
};

const COMMAND_STATUS_TONES = {
  PENDING: "amber",
  ACKED: "amber",
  APPLIED: "teal",
  PARTIAL_APPLIED: "amber",
  NOOP: "gray",
  CONFLICT: "red",
  FAILED: "red",
  EXPIRED: "gray",
};

export function commandStatusLabel(status) {
  return COMMAND_STATUS_LABELS[status] || status || "待受理";
}

export function commandStatusTone(status) {
  return COMMAND_STATUS_TONES[status] || "amber";
}
