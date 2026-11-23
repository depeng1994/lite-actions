import type {SqlStore} from "../storage/sqlStore";
import type {DeviceGroup, Projection, TaskSummary} from "../types/api";

type Shape = Pick<TaskSummary, "logical_model" | "test_id" | "target_pool" | "req_nodes" | "req_devices">;
type Estimate = Pick<TaskSummary, "estimated_sec" | "sample_count" | "estimate_status">;

export function durationKey(task: Shape) {
  return JSON.stringify([task.logical_model, task.test_id, task.target_pool, task.req_nodes, task.req_devices]);
}

export async function estimates(db: SqlStore, tasks: Shape[]) {
  const keys = [...new Set(tasks.map(durationKey))];
  if (!keys.length) return new Map<string, number[]>();
  const {results} = await db.prepare(`
    SELECT k.value AS shape, (
      SELECT json_group_array(duration_sec) FROM (
        SELECT duration_sec FROM duration_samples WHERE logical_model=json_extract(k.value,'$[0]')
          AND test_id=json_extract(k.value,'$[1]') AND target_pool=json_extract(k.value,'$[2]')
          AND req_nodes=json_extract(k.value,'$[3]') AND req_devices=json_extract(k.value,'$[4]')
        ORDER BY finished_at DESC,id DESC LIMIT 10
      )
    ) AS samples FROM json_each(?) k
  `).bind(JSON.stringify(keys)).all<{shape: string; samples: string}>();
  return new Map(results.map(row => [row.shape, JSON.parse(row.samples) as number[]]));
}

export function estimate(samples: number[], now: number, started?: number | null): Estimate {
  const sorted = samples.slice(0, 10).sort((a, b) => a - b);
  const count = sorted.length;
  if (!count) return {estimated_sec: null, sample_count: 0, estimate_status: "NO_ESTIMATE"};
  const median = (sorted[Math.floor(count / 2)] + sorted[Math.floor((count - 1) / 2)]) / 2;
  return {estimated_sec: median, sample_count: count,
    estimate_status: started != null && now - started > median ? "EXCEEDED" : "ESTIMATED"};
}

export type QueueTask = {task_uid: string; estimated_sec: number | null; options: number[][]};
export type ProjectionRow = Pick<DeviceGroup, "state" | "paused" | "quarantined" | "offline"> &
  {release_at: number; uncertain: boolean; peers?: number[]};

export function projectSchedule(queues: QueueTask[][], groups: ProjectionRow[], now: number) {
  const free = groups.map(group => ({
    blocked: group.state === "UNKNOWN_BUSY" || group.paused || group.quarantined || group.offline,
    time: Math.max(group.release_at, now), uncertain: group.uncertain,
  }));
  const output: {projection: Projection; groupIndexes: number[]}[] = [];
  for (const queue of queues) {
    let noEstimate = false;
    for (const task of queue) {
      const {task_uid, estimated_sec} = task;
      if (noEstimate || estimated_sec === null) {
        noEstimate = true;
        output.push({projection: {task_uid, projection_status: "NO_ESTIMATE"}, groupIndexes: []});
        continue;
      }
      let best: {at: number; indexes: number[]; uncertain: boolean} | undefined;
      for (const indexes of task.options) {
        if (!indexes.length || indexes.some(index => !free[index] || free[index].blocked)) continue;
        const at = Math.max(...indexes.map(index => free[index].time));
        if (!best || at < best.at) best = {at, indexes, uncertain: indexes.some(index => free[index].uncertain)};
      }
      if (!best) {
        output.push({projection: {task_uid, projection_status: "WAITING_EXTERNAL"}, groupIndexes: []});
        continue;
      }
      const end = best.at + estimated_sec;
      const occupied = new Set(best.indexes.flatMap(index => [index, ...(groups[index].peers ?? [])]));
      for (const index of occupied) {
        free[index].time = Math.max(free[index].time, end);
        free[index].uncertain ||= best.uncertain;
      }
      output.push({projection: {task_uid, projection_status: "SCHEDULED", est_start: best.at, est_end: end,
        uncertain_release: best.uncertain}, groupIndexes: best.indexes});
    }
  }
  return output;
}
