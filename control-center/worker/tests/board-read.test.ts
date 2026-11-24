import {beforeAll, expect, test} from "vitest";
import type {Timeline, WorkflowList} from "../src/types/api";
import {gzip} from "../src/utils/gzip";
import {db, migrate, request, sessionCookie, signIn} from "./db";

beforeAll(migrate);
const now = Math.floor(Date.now() / 1000);

test("history pages across storage tiers exactly once and reads cold details", async () => {
  const coldTime = now - 31 * 86400;
  const chunk = await gzip(JSON.stringify({"t_000": {running_at: coldTime - 91, queued_at: coldTime - 180}}));
  await db.prepare(`INSERT INTO task_details_cold VALUES ('chunk','lite-actions',0,?,?,1,'hash',?,100,?,?)`)
    .bind(coldTime, coldTime, chunk, chunk.byteLength, now).run();
  const rows = Array.from({length: 103}, (_, i) => `t_${String(i).padStart(3,"0")}`);
  await db.batch([
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,storage_tier,cold_chunk_id)
      SELECT value,CASE WHEN key=0 THEN ? ELSE ? + key END,'blue','github','depeng1994/lite-actions','lite-actions',value,1,
      'case_000_a','a','A','npu_smoke','pool',1,8,'[]','PASS',1,
      CASE WHEN key=0 THEN 'cold' ELSE 'hot' END,CASE WHEN key=0 THEN 'chunk' END FROM json_each(?)`)
      .bind(coldTime, now - 200, JSON.stringify(rows)),
    db.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,logical_model,submitted_at,is_active,
      has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,pools_json,max_cards,result_state,
      first_started_at,finished_at,card_seconds_finished,has_unmeasured_allocated,updated_at)
      SELECT 'github','depeng1994/lite-actions',value,1,'lite-actions',CASE WHEN key=0 THEN ? ELSE ? + key END,0,
      0,0,0,1,'["pool"]',8,'PASS',CASE WHEN key=0 THEN ? - 91 ELSE ? + key - 10 END,
      CASE WHEN key=0 THEN ? ELSE ? + key END,80,0,? FROM json_each(?)`)
      .bind(coldTime, now - 200, coldTime, now - 200, coldTime, now - 200, now, JSON.stringify(rows)),
  ]);
  const found = [];
  let cursor: string | null = null;
  for (const expected of [50, 50, 3]) {
    const response: Response = await request("/public/workflows?model=lite-actions&history_limit=50" + (cursor ? "&cursor=" + cursor : ""));
    expect(response.status).toBe(200);
    const page: WorkflowList = await response.json();
    const tasks = page.history.flatMap(run => run.cases);
    expect(tasks).toHaveLength(expected);
    found.push(...tasks.map(task => task.task_uid));
    cursor = page.next_cursor;
  }
  expect(found).toEqual(rows.reverse());
  expect(cursor).toBeNull();
  const detail = await request("/public/tasks/t_000");
  expect(await detail.json()).toMatchObject({task_uid: "t_000", running_at: coldTime - 91});
});

test("public timeline and workflow views share allocations while admin can read real IPs", async () => {
  const hosts = ["198.51.100.1:22", "198.51.100.2:22"];
  const devices = [0,1,2,3,4,5,6,7];
  const pool = {pool: {owner: "blue", enabled: true, type: "ascend-a3",
    nodes: hosts.map(host_id => ({host_id, allocation_groups: [{devices, verified: true}]}))}};
  await db.prepare("INSERT INTO pools_configs VALUES(1,'hash',?,'{}','test',?,NULL)").bind(JSON.stringify(pool), now).run();
  await db.prepare(`INSERT INTO workers(worker_id,instance_id,applied_rev,code_sha,health_ok,active_count,queued_count,
    last_heartbeat_at,last_idle_probe_at,last_egress_used,updated_at)
    VALUES('blue','01234567',1,'sha',1,2,0,?,NULL,0,?)`).bind(now,now).run();
  const tasks = [["t_active", "npu_smoke", 2, 8, hosts.map(host_id => ({host_id, devices}))],
    ["t_probe", "worker_probe", 0, 0, []]];
  await db.batch([
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,allocation_json,running_at,timing_reliable,updated_at)
      SELECT json_extract(value,'$[0]'),json_extract(value,'$[0]'),json_extract(value,'$[0]'),'blue','gitcode','cann/torchtitan-npu',
      'torchtitan-npu','gc_1',1,'case_000_a','a','A',json_extract(value,'$[1]'),'pool',json_extract(value,'$[2]'),
      json_extract(value,'$[3]'),'RUNNING',json_extract(value,'$[4]'),?,1,? FROM json_each(?)`)
      .bind(now-20,now,JSON.stringify(tasks)),
    db.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,logical_model,submitted_at,is_active,
      has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,pools_json,max_cards,result_state,
      first_started_at,finished_at,card_seconds_finished,has_unmeasured_allocated,updated_at)
      VALUES('gitcode','cann/torchtitan-npu','gc_1',1,'torchtitan-npu',?,1,1,0,2,2,'["pool"]',16,'QUEUED',?,NULL,0,0,?)`)
      .bind(now-30, now-20, now),
  ]);
  const pub = await request("/public/timeline");
  expect(pub.headers.get("Cache-Control")).toBe("public, max-age=10");
  const text = await pub.text();
  expect(text).not.toMatch(/198\.51\.100|"host_id"/);
  const timeline: Timeline = JSON.parse(text);
  const poolObj = timeline.workers[0].pools[0];
  const nodes = poolObj.nodes;
  expect(poolObj.draining).toBe(false);
  expect(poolObj.health.status).toBe("PENDING_PROBE");
  expect(nodes[0].health.status).toBe("PENDING_PROBE");
  expect(nodes[0].device_groups[0].draining).toBe(false);
  expect(timeline.zero_card_tasks.map(task => task.task_uid)).toEqual(["t_probe"]);
  const modelsResp = await (await request("/public/models")).json<{default_model: string; models: unknown[]}>();
  expect(modelsResp.default_model).toBe("vllm-ascend");
  expect(modelsResp.models.length).toBeGreaterThanOrEqual(3);
  expect(nodes.map(node => node.device_groups[0].solid_blocks[0])).toMatchObject([
    {task_uid: "t_active", total_cards: 16, is_primary_row: true, group_count: 2},
    {task_uid: "t_active", total_cards: 16, is_primary_row: false, group_count: 2},
  ]);
  const wf = await (await request("/public/workflows?model=torchtitan-npu&history_limit=0")).json<WorkflowList>();
  const activeTask = wf.active[0].cases.find(task => task.task_uid === "t_active")!;
  expect(activeTask.allocation.map(node => node.node_ref)).toEqual(nodes.map(node => node.node_ref));
  const cookie = sessionCookie(await signIn());
  const admin = await request("/admin/timeline", {cookie});
  expect(admin.headers.get("Cache-Control")).toBe("private, no-store");
  expect((await admin.json<Timeline>()).workers[0].pools[0].nodes.map(node => node.host_id)).toEqual(hosts);
});

test("timeline includes completed tasks from the requested history window", async () => {
  const finished = now - 60 * 60;
  const devices = [0,1,2,3,4,5,6,7];
  await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
    case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,
    storage_tier,started_at,workflow_name)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?)`)
    .bind("t_history_timeline", finished, "blue", "gitcode", "cann/torchtitan-npu", "torchtitan-npu", "history-run", 1,
      "case_history", "history", "Historical task", "npu_smoke", "pool", 1, 8,
      JSON.stringify([{host_id: "198.51.100.1:22", devices}]), "PASS", 1, "hot", finished - 120, "history-workflow").run();

  const timeline = await (await request("/public/timeline?history_hours=72")).json<Timeline>();
  expect(timeline.history_tasks.map(task => task.task_uid)).toContain("t_history_timeline");
  const group = timeline.workers[0].pools[0].nodes[0].device_groups[0];
  expect(group.state).toBe("LEASED");
  expect(group.solid_blocks).toMatchObject([
    {task_uid: "t_active", total_cards: 16, group_count: 2},
    {task_uid: "t_history_timeline", total_cards: 8, group_count: 1},
  ]);
});
