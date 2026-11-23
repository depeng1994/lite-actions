import {beforeAll, expect, test} from "vitest";
import {archive, capacity, databaseSize, putMeta} from "../src/services/retention";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";
import type {Context} from "../src/router";
import type {WorkflowList} from "../src/types/api";

const now = Math.floor(Date.now() / 1000);
const context: Context = {request: new Request("https://test.invalid"), url: new URL("https://test.invalid"), env: bindings, params: {}, now};
const day = 86400;

beforeAll(migrate);
async function seed(count: number, prefix: string, finish: (index: number) => number) {
  const rows = Array.from({length: count}, (_, i) => [prefix + i.toString().padStart(5,"0"), finish(i)]);
  const payload = JSON.stringify(rows);
  await db.batch([
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,case_id,
      test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable)
    SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),'blue','github','depeng1994/lite-actions','lite-actions',
      json_extract(value,'$[0]'),1,'case_000_test','test','test','npu_smoke','pool',1,8,'[]','PASS',1 FROM json_each(?)`).bind(payload),
    db.prepare(`INSERT INTO task_details_hot(task_uid,finished_at,detail_json)
      SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_object('running_at',json_extract(value,'$[1]')-17)
      FROM json_each(?)`).bind(payload),
    db.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,logical_model,submitted_at,is_active,
      has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,pools_json,max_cards,result_state,
      first_started_at,finished_at,card_seconds_finished,has_unmeasured_allocated,updated_at)
    SELECT 'github','depeng1994/lite-actions',json_extract(value,'$[0]'),1,'lite-actions',json_extract(value,'$[1]')-20,0,
      0,0,0,1,'["pool"]',8,'PASS',json_extract(value,'$[1]')-17,json_extract(value,'$[1]'),136,0,?
      FROM json_each(?)`).bind(now, payload),
  ]);
}
test("archives and verifies cold gzip, purges old days and preserves keyset order", async () => {
  await seed(103, "t_history_", i => i < 43 ? now - 31 * day - i : now - i);
  await seed(5, "t_expired_", i => now - 366 * day - i);
  await archive(context);
  expect(await db.prepare("SELECT count(*) AS n FROM task_index WHERE storage_tier='cold'").first("n")).toBe(43);
  expect(await db.prepare("SELECT count(*) AS n FROM task_details_hot").first("n")).toBe(60);
  expect(await db.prepare("SELECT count(*) AS n FROM task_index WHERE task_uid LIKE 't_expired_%'").first("n")).toBe(0);

  const uids: string[] = [];
  let cursor = "";
  for (const size of [50,50,3]) {
    const page = await (await request("/public/workflows?model=lite-actions&history_limit=50" + cursor)).json<WorkflowList>();
    const rows = page.history.flatMap(run => run.cases);
    expect(rows).toHaveLength(size);
    uids.push(...rows.map(task => task.task_uid));
    cursor = page.next_cursor ? "&cursor=" + page.next_cursor : "";
  }
  expect(new Set(uids).size).toBe(103);
  const detail = await request("/public/tasks/t_history_00000");
  expect(await detail.json()).toMatchObject({running_at: now - 31 * day - 17});
  expect(await databaseSize(db)).toBeGreaterThan(0);
});

test("rolls oldest summaries and their details, keeps protected data and marks history end", async () => {
  const cookie = sessionCookie(await signIn());
  await request("/admin/users", {cookie, data: {username: "capacity"}});
  await seed(600, "t_purge_", i => now - 1000 - i);
  await db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
    VALUES('protected','yellow','SWAP_WORKFLOW','{}','PENDING','test',?)`).bind(now).run();
  const first = await db.prepare("SELECT task_uid FROM task_index ORDER BY finished_at,task_uid LIMIT 1").first("task_uid");
  let probes = 0;
  const size = async () => (++probes === 1 ? 810 : 690);
  await capacity({...context, env: {...bindings, D1_LIMIT_BYTES: "1000"}}, size);
  expect(probes).toBe(2);
  expect(await db.prepare("SELECT task_uid FROM task_index WHERE task_uid=?").bind(first).first("task_uid")).toBeNull();
  expect(await db.prepare("SELECT status FROM commands WHERE cmd_id='protected'").first("status")).toBe("PENDING");
  expect(await db.prepare("SELECT username FROM users WHERE username='capacity'").first("username")).toBe("capacity");
  expect(await db.prepare("SELECT count(*) AS n FROM audit_logs WHERE action='CAPACITY_PURGE'").first("n")).toBe(1);
  const endCursor = btoa(JSON.stringify([1, "github", "depeng1994/lite-actions", "~", 1]))
    .replaceAll("+","-").replaceAll("/","_").replace(/=+$/,"");
  const page = await (await request("/admin/workflows?model=lite-actions&history_limit=50&cursor=" + endCursor, {cookie})).json<WorkflowList>();
  expect(page.truncated_by_capacity).toBe(true);
});

test("quota rejection is read-safe, prevents managed writes and rolls over UTC", async () => {
  const cookie = sessionCookie(await signIn());
  await putMeta(db, "quota_exhausted_day", String(Math.floor(now / day)), now).run();
  const refused = await request("/admin/users", {cookie, data: {username: "rejected"}});
  expect(refused.status).toBe(503);
  expect(await refused.json()).toMatchObject({code: "WRITE_QUOTA_EXHAUSTED"});
  expect(Number(refused.headers.get("Retry-After"))).toBeGreaterThan(0);
  expect((await request("/public/overview")).status).toBe(200);
  expect(await db.prepare("SELECT username FROM users WHERE username='rejected'").first("username")).toBeNull();
  await putMeta(db, "quota_exhausted_day", String(Math.floor(now / day) - 1), now).run();
  expect((await request("/admin/users", {cookie, data: {username: "recovered"}})).status).toBe(201);
});

test("local D1 size_after crosses real storage watermarks", async () => {
  await seed(440, "t_sized_", i => now - i);
  await db.prepare(`UPDATE task_details_hot SET detail_json=json_object('padding',hex(randomblob(1800)))
    WHERE task_uid LIKE 't_sized_%'`).run();
  const before = await databaseSize(db);
  const limit = Math.ceil(before / .82);
  await capacity({...context, env: {...bindings, D1_LIMIT_BYTES: String(limit)}});
  const after = await databaseSize(db);
  expect(after).toBeLessThan(.7 * limit);
  expect(await db.prepare("SELECT count(*) AS n FROM task_index WHERE task_uid LIKE 't_sized_%'").first("n")).toBeGreaterThan(0);
});
