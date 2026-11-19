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
  expect(first.history[0].submitted_at).toBe(now - 40);
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

test("workflow history paginates complete Workflow rows in 50/50/3 pages", async () => {
  for (let offset = 0; offset < 103; offset += 1) {
    await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      `large-${offset}`, now - offset, "blue", "github", "org/large", "large", `history-${String(offset).padStart(3, "0")}`, 1,
      `case-${offset}`, "worker_os_blue", "OS probe", "worker_probe", "pool", 0, 0, "[]", "PASS", 1,
      now - offset - 10, "Large History",
    ).run();
  }
  const pages: string[] = [];
  let cursor = "";
  for (const expected of [50, 50, 3]) {
    const suffix = cursor ? `&cursor=${cursor}` : "";
    const page = await (await request(`/public/workflows?history_limit=50&repo=org%2Flarge&status=history${suffix}`)).json<{
      history: Array<{run_id: string; sequence: number}>; next_cursor: string | null; history_total: number; has_more: boolean;
    }>();
    expect(page.history).toHaveLength(expected);
    expect(page.history_total).toBe(103);
    pages.push(...page.history.map(row => row.run_id));
    expect(page.history.map(row => row.sequence)).toEqual([...page.history].sort((left, right) => right.sequence - left.sequence).map(row => row.sequence));
    cursor = page.next_cursor ?? "";
  }
  expect(new Set(pages).size).toBe(103);
  expect(cursor).toBe("");
});

test("workflow card-hours use finished duration and allocated devices", async () => {
  const allocation = JSON.stringify([{host_id: "198.51.100.88:22", devices: Array.from({length: 16}, (_, index) => index)}]);
  await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
    case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,submitted_at,workflow_name)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
    "card-hours", now - 500, "blue", "github", "org/card", "card", "card-hours", 1,
    "case-card", "npu_smoke", "16P", "npu_smoke", "pool", 1, 16, allocation, "PASS", 1, 2280,
    now - 3000, "Card Hours",
  ).run();
  const page = await (await request("/public/workflows?history_limit=50&repo=org%2Fcard&status=history")).json<{
    history: Array<{card_hours: number | null; result: string; workflow_name: string | null}>;
  }>();
  expect(page.history).toHaveLength(1);
  expect(page.history[0]).toMatchObject({card_hours: 10.13, result: "PASS", workflow_name: "Card Hours"});
});

test("mixed active and finished cases stay in one active Workflow row", async () => {
  const run = "mixed-workflow";
  await db.batch([
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,
      submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      "mixed-finished", now - 40, "blue", "github", "org/mixed", "mixed", run, 1,
      "case-finished", "npu_smoke", "Finished case", "npu_smoke", "pool", 1, 8, "[]", "PASS", 1,
      now - 100, "Mixed Workflow",
    ),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,allocation_json,running_at,
      timing_reliable,updated_at,submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      "mixed-active", "mixed-active", "mixed-active-key", "blue", "github", "org/mixed", "mixed", run, 1,
      "case-active", "npu_smoke", "Active case", "npu_smoke", "pool", 1, 8, "RUNNING",
      JSON.stringify([{host_id: "198.51.100.7:22", devices: [0,1,2,3,4,5,6,7]}]), now - 20, 1, now,
      now - 100, "Mixed Workflow",
    ),
  ]);
  const page = await (await request("/public/workflows?history_limit=50&repo=org%2Fmixed")).json<{
    active: Array<{sequence: number; cases: unknown[]}>; history: unknown[];
  }>();
  expect(page.active).toHaveLength(1);
  expect(page.active[0].sequence).toBe(0);
  expect(page.active[0].cases).toHaveLength(2);
  expect(page.history).toHaveLength(0);
});
