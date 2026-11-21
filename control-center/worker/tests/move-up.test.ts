import {beforeAll, expect, test} from "vitest";
import {db, migrate, request, sessionCookie, signIn} from "./db";

beforeAll(migrate);

test("move-up queues ordered commands scoped to the task worker", async () => {
  await db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
    attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
    VALUES('fixture','tid','gitcode:key','yellow','gitcode','cann/torchtitan-npu','torchtitan-npu','1',1,
      'case_000_test','test','test','npu_smoke','pool',1,8,'QUEUED',1,1)`).run();
  const cookie = sessionCookie(await signIn());
  const path = "/admin/tasks/fixture/move-up";
  const results = await Promise.all(Array.from({length: 3}, () => request(path, {data: {}, cookie})));
  expect(results.map(res => res.status)).toEqual([202,202,202]);
  const payloads = await Promise.all(results.map(res => res.json<{cmd_id: string}>()));
  const records = await db.prepare("SELECT cmd_id,worker_id,payload_json FROM commands ORDER BY ordinal").all<{
    cmd_id: string; worker_id: string; payload_json: string;
  }>();
  expect(new Set(records.results.map(row => row.cmd_id))).toEqual(new Set(payloads.map(item => item.cmd_id)));
  expect(records.results.every(row => row.worker_id === "yellow" && JSON.parse(row.payload_json).task_key === "gitcode:key")).toBe(true);
  await db.prepare("INSERT INTO queue_orders VALUES('yellow','pool','npu_smoke','fixture',0,'{}',1,1)").run();
  expect(await (await request(path, {data: {}, cookie})).json()).toEqual({status: "NOOP", reason: "ALREADY_AT_HEAD"});
  await db.prepare("UPDATE tasks_active SET phase='RESERVED' WHERE task_uid='fixture'").run();
  expect((await request(path, {data: {}, cookie})).status).toBe(409);
});
