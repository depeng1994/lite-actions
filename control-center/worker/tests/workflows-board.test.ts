import {beforeAll, expect, test} from "vitest";
import {workflows as queryWorkflows} from "../src/services/board";
import {refreshWorkflowRuns, type WorkflowIdentity} from "../src/services/ingest";
import {bindings, db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);

async function syncRuns(identities: WorkflowIdentity[]) {
  await db.batch(refreshWorkflowRuns(db, identities, now));
}

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
  await syncRuns([
    {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-a", attempt: 1},
    {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-b", attempt: 1},
  ]);
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

test("workflow history paginates complete Workflow rows in 50/50/3 pages and history_limit=0 avoids task_index scan", async () => {
  const seeded: WorkflowIdentity[] = [];
  for (let offset = 0; offset < 103; offset += 1) {
    const runId = `history-${String(offset).padStart(3, "0")}`;
    await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      `large-${offset}`, now - offset, "blue", "github", "org/large", "large", runId, 1,
      `case-${offset}`, "worker_os_blue", "OS probe", "worker_probe", "pool", 0, 0, "[]", "PASS", 1,
      now - offset - 10, "Large History",
    ).run();
    seeded.push({provider: "github", repo: "org/large", run_id: runId, attempt: 1});
  }
  await syncRuns(seeded);

  // Zero-history default query must not scan task_index or query system_meta twice.
  const queries: string[] = [];
  const tracedDb = new Proxy(db, {
    get(target, prop, receiver) {
      if (prop === "prepare") {
        return (sql: string) => {
          queries.push(sql);
          return target.prepare(sql);
        };
      }
      const value = Reflect.get(target, prop, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const zeroHistory = await queryWorkflows({
    request: new Request("https://control.test/api/v1/public/workflows?history_limit=0"),
    url: new URL("https://control.test/api/v1/public/workflows?history_limit=0"),
    env: {...bindings, DB: tracedDb},
    params: {},
    now,
  });
  expect(zeroHistory.history).toHaveLength(0);
  expect(zeroHistory.history_total).toBe(0);
  expect(queries.some(sql => /\btask_index\b/i.test(sql))).toBe(false);
  expect(queries.filter(sql => /\bcapacity_purged_before_ts\b/i.test(sql))).toHaveLength(1);

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

test("incremental workflow_runs refresh preserves unrelated workflow queue_order_rank", async () => {
  await db.batch([
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('inc-a','incaaaaaaaaa','key:inc-a','blue','github','org/inc','inc','inc-a',1,
        'case_000_a','npu_smoke','A','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 100),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('inc-b','incbbbbbbbbb','key:inc-b','blue','github','org/inc','inc','inc-b',1,
        'case_000_b','npu_smoke','B','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 50),
  ]);
  await syncRuns([
    {provider: "github", repo: "org/inc", run_id: "inc-a", attempt: 1},
    {provider: "github", repo: "org/inc", run_id: "inc-b", attempt: 1},
  ]);
  await db.prepare("UPDATE workflow_runs SET queue_order_rank=7,updated_at=12345 WHERE run_id='inc-a'").run();
  // Refreshing only inc-b must not touch inc-a's rank or updated_at.
  await db.batch(refreshWorkflowRuns(db, [{provider: "github", repo: "org/inc", run_id: "inc-b", attempt: 1}], now + 10));
  const rowA = await db.prepare("SELECT queue_order_rank,updated_at FROM workflow_runs WHERE run_id='inc-a'").first();
  expect(rowA).toEqual({queue_order_rank: 7, updated_at: 12345});
  // Refreshing inc-a while still waiting must preserve its existing queue_order_rank.
  await db.batch(refreshWorkflowRuns(db, [{provider: "github", repo: "org/inc", run_id: "inc-a", attempt: 1}], now + 20));
  expect(await db.prepare("SELECT queue_order_rank FROM workflow_runs WHERE run_id='inc-a'").first("queue_order_rank")).toBe(7);
  await db.prepare("DELETE FROM tasks_active WHERE run_id IN ('inc-a','inc-b')").run();
  await db.prepare("DELETE FROM workflow_runs WHERE run_id IN ('inc-a','inc-b')").run();
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
  await syncRuns([{provider: "github", repo: "org/card", run_id: "card-hours", attempt: 1}]);
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
  await syncRuns([{provider: "github", repo: "org/mixed", run_id: run, attempt: 1}]);
  const page = await (await request("/public/workflows?history_limit=50&repo=org%2Fmixed")).json<{
    active: Array<{sequence: number; finished_at: number | null; result: string; cases: unknown[]}>; history: unknown[];
  }>();
  expect(page.active).toHaveLength(1);
  expect(page.active[0].sequence).toBe(0);
  expect(page.active[0].finished_at).toBeNull();
  expect(page.active[0].result).toBe("RUNNING");
  expect(page.active[0].cases).toHaveLength(2);
  expect(page.history).toHaveLength(0);
});

test("active Workflow result distinguishes all-queued from running and partially completed runs", async () => {
  const repo = "org/wf-status";
  const model = "wf-status";
  const insertActive = (uid: string, runId: string, caseId: string, phase: string) => db.prepare(`
    INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,allocation_json,
      queued_at,reserved_at,running_at,timing_reliable,updated_at,submitted_at,workflow_name)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `).bind(
    uid, uid.slice(0, 12), `key:${uid}`, "blue", "github", repo, model, runId, 1,
    caseId, "npu_smoke", caseId, "npu_smoke", "pool", 1, 8, phase, "[]",
    now - 100, phase === "QUEUED" ? null : now - 50, phase === "RUNNING" ? now - 40 : null,
    1, now, now - 100, "Status Matrix",
  );
  const insertFinished = (uid: string, runId: string, caseId: string, status: string) => db.prepare(`
    INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,
      timing_reliable,submitted_at,workflow_name)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `).bind(
    uid, now - 30, "blue", "github", repo, model, runId, 1,
    caseId, "npu_smoke", caseId, "npu_smoke", "pool", 1, 8, "[]", status, 1,
    now - 100, "Status Matrix",
  );

  await db.batch([
    // 1. All cases QUEUED -> QUEUED
    insertActive("st_all_q_1", "status-all-queued", "case_001_q", "QUEUED"),
    insertActive("st_all_q_2", "status-all-queued", "case_002_q", "QUEUED"),
    // 2. Mixed QUEUED + RUNNING -> RUNNING
    insertActive("st_mix_r_1", "status-queued-running", "case_001_q", "QUEUED"),
    insertActive("st_mix_r_2", "status-queued-running", "case_002_r", "RUNNING"),
    // 3. Mixed QUEUED + RESERVED / STAGING / CLEANING -> RUNNING
    insertActive("st_res_1", "status-reserved", "case_001_q", "QUEUED"),
    insertActive("st_res_2", "status-reserved", "case_002_res", "RESERVED"),
    insertActive("st_stg_1", "status-staging", "case_001_q", "QUEUED"),
    insertActive("st_stg_2", "status-staging", "case_002_stg", "STAGING"),
    insertActive("st_cln_1", "status-cleaning", "case_001_q", "QUEUED"),
    insertActive("st_cln_2", "status-cleaning", "case_002_cln", "CLEANING"),
    // 4. Completed (PASS/FAIL) + RUNNING (0 QUEUED) -> RUNNING
    insertFinished("st_done_r_1", "status-done-running", "case_001_p", "PASS"),
    insertFinished("st_done_r_2", "status-done-running", "case_002_f", "FAIL"),
    insertActive("st_done_r_3", "status-done-running", "case_003_r", "RUNNING"),
    // 5. Completed (PASS/FAIL) + QUEUED (0 executing) -> RUNNING
    insertFinished("st_done_q_1", "status-done-queued", "case_001_p", "PASS"),
    insertActive("st_done_q_2", "status-done-queued", "case_002_q", "QUEUED"),
    // 6. All completed -> PASS / FAIL
    insertFinished("st_pass_1", "status-all-pass", "case_001_p", "PASS"),
    insertFinished("st_pass_2", "status-all-pass", "case_002_p", "PASS"),
    insertFinished("st_fail_1", "status-all-fail", "case_001_p", "PASS"),
    insertFinished("st_fail_2", "status-all-fail", "case_002_f", "FAIL"),
  ]);

  const runIds = [
    "status-all-queued", "status-queued-running", "status-reserved", "status-staging",
    "status-cleaning", "status-done-running", "status-done-queued", "status-all-pass", "status-all-fail",
  ];
  await syncRuns(runIds.map(run_id => ({provider: "github", repo, run_id, attempt: 1})));

  const page = await (await request(`/public/workflows?history_limit=50&repo=${encodeURIComponent(repo)}`)).json<{
    active: Array<{run_id: string; result: string}>;
    history: Array<{run_id: string; result: string}>;
  }>();

  const activeResult = Object.fromEntries(page.active.map(row => [row.run_id, row.result]));
  expect(activeResult).toEqual({
    "status-all-queued": "QUEUED",
    "status-queued-running": "RUNNING",
    "status-reserved": "RUNNING",
    "status-staging": "RUNNING",
    "status-cleaning": "RUNNING",
    "status-done-running": "RUNNING",
    "status-done-queued": "RUNNING",
  });
  const historyResult = Object.fromEntries(page.history.map(row => [row.run_id, row.result]));
  expect(historyResult).toEqual({
    "status-all-pass": "PASS",
    "status-all-fail": "FAIL",
  });
});

test("Workflow summaries expose partial card-hours and three-state outcomes", async () => {
  const allocation = JSON.stringify([{host_id: "198.51.100.8:22", devices: [0,1,2,3,4,5,6,7]}]);
  const summaryCases: Array<{uid: string; run: string; status: string; reliable: number; duration: number | null; alloc: string}> = [
    {uid: "partial-measured", run: "partial-run", status: "PASS", reliable: 1, duration: 60, alloc: allocation},
    {uid: "partial-unmeasured", run: "partial-run", status: "PASS", reliable: 0, duration: null, alloc: allocation},
    {uid: "outcome-pass", run: "outcome-run", status: "PASS", reliable: 1, duration: null, alloc: "[]"},
    {uid: "outcome-cancelled", run: "outcome-run", status: "CANCELLED", reliable: 0, duration: null, alloc: "[]"},
  ];
  await db.batch(summaryCases.map(({uid, run, status, reliable, duration, alloc}) => db.prepare(`INSERT INTO task_index(
      task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,case_id,test_id,label,kind,
      target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,submitted_at,workflow_name)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      uid, now - 200, "blue", "github", "org/summary", "summary", run, 1, `case_${uid.slice(0, 3)}_case`,
      "npu_smoke", uid, "npu_smoke", "pool", 1, 8, alloc, status, reliable, duration, now - 300, "Summary Workflow",
    )));
  await syncRuns([
    {provider: "github", repo: "org/summary", run_id: "partial-run", attempt: 1},
    {provider: "github", repo: "org/summary", run_id: "outcome-run", attempt: 1},
  ]);
  const page = await (await request("/public/workflows?history_limit=50&repo=org%2Fsummary")).json<{
    history: Array<{run_id: string; card_hours: number | null; card_hours_partial: boolean; result: string}>;
  }>();
  expect(page.history).toHaveLength(2);
  expect(page.history.find(row => row.run_id === "partial-run")).toMatchObject({card_hours: 0.13, card_hours_partial: true, result: "PASS"});
  expect(page.history.find(row => row.run_id === "outcome-run")).toMatchObject({result: "FAIL"});
});

test("GitCode test-worker workflow with zero-pool probe cases stays queryable by platform, model, repo, and pool without blank pool entries", async () => {
  const runId = "57d83e103f694111be5cf35420780955";
  const repo = "depeng1994/torchtitan-npu";
  const allocBlue = JSON.stringify([{host_id: "198.51.100.10:22", devices: [0,1,2,3,4,5,6,7]}]);
  const allocYellow = JSON.stringify([{host_id: "198.51.100.20:22", devices: [0,1,2,3,4,5,6,7]}]);
  await db.batch([
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,reporter_id,is_test,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,
      submitted_at,workflow_name,run_number,display_title,trigger_actor,event_type)
      VALUES(?,?,?,?,1,'gitcode',?,'torchtitan-npu',?,1,?,?,?,?,?,?,?,'[]','PASS',1,6,?,'lite-ci-dispatcher-smoke',36,NULL,'depeng1994','Manual')`).bind(
      "gc-test-probe", now - 260, "blue", "blue-test-id45747381962a1", repo, runId,
      "case_000_worker_os_blue", "worker_os_blue", "Blue OS probe", "worker_probe", "", 0, 0, now - 300,
    ),
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,reporter_id,is_test,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,
      submitted_at,workflow_name,run_number,display_title,trigger_actor,event_type)
      VALUES(?,?,?,?,1,'gitcode',?,'torchtitan-npu',?,1,?,?,?,?,?,?,?,?,?,1,180,?,'lite-ci-dispatcher-smoke',36,NULL,'depeng1994','Manual')`).bind(
      "gc-test-blue", now - 220, "blue", "blue-test-id45747381962a1", repo, runId,
      "case_003_dispatch_smoke_16p", "dispatch_smoke_16p", "16P smoke", "npu_smoke", "a3_shared_blue", 2, 8, allocBlue, "INFRA_ERROR", now - 300,
    ),
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,reporter_id,is_test,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,
      submitted_at,workflow_name,run_number,display_title,trigger_actor,event_type)
      VALUES(?,?,?,?,1,'gitcode',?,'torchtitan-npu',?,1,?,?,?,?,?,?,?,?,?,1,120,?,'lite-ci-dispatcher-smoke',36,NULL,'depeng1994','Manual')`).bind(
      "gc-test-yellow", now - 240, "yellow", "yellow-test-id45747381962a1", repo, runId,
      "case_005_dispatch_smoke_yellow_a3_8p", "dispatch_smoke_yellow_a3_8p", "Yellow 8P smoke", "npu_smoke", "a3_shared_yellow", 1, 8, allocYellow, "PASS", now - 300,
    ),
  ]);
  await syncRuns([{provider: "gitcode", repo, run_id: runId, attempt: 1, is_test: 1}]);

  const storedPools = await db.prepare("SELECT pools_json FROM workflow_runs WHERE provider='gitcode' AND run_id=? AND is_test=1")
    .bind(runId).first<string>("pools_json");
  expect(JSON.parse(storedPools ?? "[]")).toEqual(["a3_shared_blue", "a3_shared_yellow"]);

  for (const query of [
    "platform=gitcode",
    "model=torchtitan-npu",
    `repo=${encodeURIComponent(repo)}`,
    "pool=a3_shared_blue",
  ]) {
    const page = await (await request(`/public/workflows?status=history&history_limit=5&${query}`)).json<{
      history: Array<{
        provider: string; repo: string; run_id: string; run_number: number | null; is_test: boolean;
        workflow_name: string | null; result: string; pools: string[]; cases: Array<{run_url: string}>;
      }>;
      history_total: number;
    }>();
    expect(page.history_total).toBeGreaterThanOrEqual(1);
    const match = page.history.find(row => row.run_id === runId);
    expect(match).toMatchObject({
      provider: "gitcode",
      repo,
      run_id: runId,
      run_number: 36,
      is_test: true,
      workflow_name: "lite-ci-dispatcher-smoke",
      result: "FAIL",
    });
    expect(match?.pools.slice().sort()).toEqual(["a3_shared_blue", "a3_shared_yellow"]);
    expect(match?.cases).toHaveLength(3);
    expect(match?.cases[0].run_url).toBe(`https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/${runId}`);
  }
});
