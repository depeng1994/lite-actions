import {beforeAll, expect, test} from "vitest";
import {db, migrate, request, sessionCookie, signIn} from "./db";

const now = Math.floor(Date.now() / 1000);
let cookie: string;

beforeAll(async () => {
  await migrate();
  cookie = sessionCookie(await signIn());
  await db.batch([
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
      VALUES('workflow_a_case','workflow_a_ca','key:a','blue','github','depeng1994/lite-actions','lite-actions','run-a',1,
        'case_000_a','test','test','npu_smoke','pool',1,8,'QUEUED',1,?)`).bind(now),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
      VALUES('workflow_b_case','workflow_b_ca','key:b','blue','github','depeng1994/lite-actions','lite-actions','run-b',1,
        'case_000_b','test','test','npu_smoke','pool',1,8,'QUEUED',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_a_case',0,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_b_case',1,'{"options":[[0]]}',1,?)`).bind(now),
  ]);
});

test("swaps queued workflows with queue-version checks and idempotency", async () => {
  const input = {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-b", attempt: 1,
    queue_version: 1, idempotency_key: "move-run-b"};
  const first = await request("/admin/workflows/move-up", {cookie, data: input});
  expect(first.status).toBe(202);
  const result = await first.json<{op_id: string; status: string; queue_version_before: number; queue_version_after: number;
    from_seq: number; to_seq: number; swapped_workflow: {run_id: string}}>();
  expect(result).toMatchObject({status: "PENDING", queue_version_before: 1, queue_version_after: 2, from_seq: 2, to_seq: 1,
    swapped_workflow: {run_id: "run-a"}});
  const repeat = await (await request("/admin/workflows/move-up", {cookie, data: input})).json<{op_id: string}>();
  expect(repeat.op_id).toBe(result.op_id);
  const commandRows = await db.prepare("SELECT cmd_id,op_id,cmd_type,payload_json,status FROM commands").all();
  console.log("workflow commands", commandRows.results, result.op_id);
  expect(await db.prepare("SELECT cmd_type FROM commands WHERE op_id=?").bind(result.op_id).first("cmd_type")).toBe("SWAP_WORKFLOW");
  expect(await db.prepare("SELECT queue_pos FROM queue_orders WHERE task_uid='workflow_b_case'").first("queue_pos")).toBe(0);
  expect(await db.prepare("SELECT queue_version FROM workflow_queue_meta WHERE id=1").first("queue_version")).toBe(2);
  expect((await request("/admin/workflows/move-up", {cookie, data: {...input, queue_version: 1, idempotency_key: "stale"}})).status).toBe(409);
});

test("queue head returns NOOP and operation receipt exposes worker commands", async () => {
  const response = await request("/admin/workflows/move-up", {cookie, data: {
    provider: "github", repo: "depeng1994/lite-actions", run_id: "run-b", attempt: 1,
    queue_version: 2, idempotency_key: "head",
  }});
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({status: "NOOP", reason: "ALREADY_AT_HEAD"});
});

test("workflow projection keeps the monotonic queue version after rows are consumed", async () => {
  await db.prepare("DELETE FROM queue_orders WHERE task_uid IN ('workflow_a_case','workflow_b_case')").run();
  const response = await request("/admin/workflows?limit=50", {cookie});
  expect(response.status).toBe(200);
  const payload = await response.json<{queue_version: number}>();
  expect(payload.queue_version).toBe(2);
});
