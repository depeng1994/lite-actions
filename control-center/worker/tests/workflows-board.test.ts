import {beforeAll, expect, test} from "vitest";
import {db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);

beforeAll(async () => {
  await migrate();
  for (const [runId, finishedAt] of [["run-a", now - 60], ["run-b", now - 30]] as const) {
    await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      `uid-${runId}`, finishedAt, "blue", "github", "depeng1994/lite-actions", "lite-actions", runId, 1,
      `case-${runId}`, "worker_os_blue", "OS probe", "worker_probe", "", 0, 0, "[]", "PASS", 1,
      finishedAt - 10, "Dispatcher Smoke",
    ).run();
  }
});

test("workflow history honors the 50-row limit and cursor", async () => {
  const first = await (await request("/public/workflows?history_limit=1")).json<{
    history: Array<{run_id: string; sequence: number; submitted_at: number; workflow_name: string}>; next_cursor: string | null; has_more: boolean;
  }>();
  expect(first.history).toHaveLength(1);
  expect(first.history[0].sequence).toBe(-1);
  expect(first.history[0].workflow_name).toBe("Dispatcher Smoke");
  expect(first.history[0].submitted_at).toBe(now - 70);
  expect(first.has_more).toBe(true);
  expect(first.next_cursor).toBeTruthy();
  const second = await (await request(`/public/workflows?history_limit=1&cursor=${first.next_cursor}`)).json<{
    history: Array<{run_id: string; sequence: number}>;
  }>();
  expect(second.history).toHaveLength(1);
  expect(second.history[0].sequence).toBe(-2);
  expect(second.history[0].run_id).not.toBe(first.history[0].run_id);
});

test("workflow filters and timeline metadata are live API fields", async () => {
  const filtered = await (await request("/public/workflows?history_limit=50&repo=depeng1994%2Flite-actions&status=history")).json<{
    active: unknown[]; history: Array<{repo: string}>;
  }>();
  expect(filtered.active).toHaveLength(0);
  expect(filtered.history.every(row => row.repo === "depeng1994/lite-actions")).toBe(true);
  const timeline = await (await request("/public/timeline")).json<{
    tasks_by_uid: Record<string, unknown>; queued_by_pool: Record<string, unknown[]>;
  }>();
  expect(timeline.tasks_by_uid).toEqual({});
  expect(timeline.queued_by_pool).toEqual({});
});
