import {beforeAll, expect, test} from "vitest";
import type {ModelRuns, Timeline} from "../src/types/api";
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
  await db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
    case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,storage_tier,cold_chunk_id)
    SELECT value,CASE WHEN key=0 THEN ? ELSE ? END,'blue','github','depeng1994/lite-actions','lite-actions','12',1,
    'case_000_a','a','A','npu_smoke','pool',1,8,'[]','PASS',1,
    CASE WHEN key=0 THEN 'cold' ELSE 'hot' END,CASE WHEN key=0 THEN 'chunk' END FROM json_each(?)`)
    .bind(coldTime, now - 20, JSON.stringify(rows)).run();
  const found = [];
  let cursor: string | null = null;
  for (const expected of [50, 50, 3]) {
    const response: Response = await request("/public/models/lite-actions/runs" + (cursor ? "?cursor=" + cursor : ""));
    expect(response.status).toBe(200);
    const page: ModelRuns = await response.json();
    const tasks = page.history.flatMap(run => run.tasks);
    expect(tasks).toHaveLength(expected);
    found.push(...tasks.map(task => task.task_uid));
    cursor = page.next_cursor;
  }
  expect(found).toEqual(rows.reverse());
  expect(cursor).toBeNull();
  const detail = await request("/public/tasks/t_000");
  expect(await detail.json()).toMatchObject({task_uid: "t_000", running_at: coldTime - 91});
});

test("public timeline and model views share allocations while admin can read real IPs", async () => {
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
  await db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
    attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,allocation_json,running_at,timing_reliable,updated_at)
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[0]'),json_extract(value,'$[0]'),'blue','gitcode','cann/torchtitan-npu',
    'torchtitan-npu','gc_1',1,'case_000_a','a','A',json_extract(value,'$[1]'),'pool',json_extract(value,'$[2]'),
    json_extract(value,'$[3]'),'RUNNING',json_extract(value,'$[4]'),?,1,? FROM json_each(?)`)
    .bind(now-20,now,JSON.stringify(tasks)).run();
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
  const model = await (await request("/public/models/torchtitan-npu/runs")).json<ModelRuns>();
  expect(model.active[0].tasks[0].allocation.map(node => node.node_ref)).toEqual(nodes.map(node => node.node_ref));
  const cookie = sessionCookie(await signIn());
  const admin = await request("/admin/timeline", {cookie});
  expect(admin.headers.get("Cache-Control")).toBe("private, no-store");
  expect((await admin.json<Timeline>()).workers[0].pools[0].nodes.map(node => node.host_id)).toEqual(hosts);
});
