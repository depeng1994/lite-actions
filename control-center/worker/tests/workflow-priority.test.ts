import {beforeAll, expect, test} from "vitest";
import {refreshWorkflowRuns} from "../src/services/ingest";
import {db, migrate, request, sessionCookie, signIn} from "./db";

const now = Math.floor(Date.now() / 1000);
let cookie: string;

beforeAll(async () => {
  await migrate();
  cookie = sessionCookie(await signIn());
  await db.batch([
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_a_1','workflow_a01','key:a1','blue','github','depeng1994/lite-actions','lite-actions','run-a',1,
        'case_000_a1','test','test','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 300),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_a_2','workflow_a02','key:a2','blue','github','depeng1994/lite-actions','lite-actions','run-a',1,
        'case_001_a2','test','test','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 300),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_b_1','workflow_b01','key:b1','blue','github','depeng1994/lite-actions','lite-actions','run-b',1,
        'case_000_b1','test','test','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 200),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_b_2','workflow_b02','key:b2','blue','github','depeng1994/lite-actions','lite-actions','run-b',1,
        'case_001_b2','test','test','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 200),
    // Cross-pool workflow on yellow (does not share blue/pool queue with run-a/run-b).
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_c_1','workflow_c01','key:c1','yellow','gitcode','cann/torchtitan-npu','torchtitan-npu','run-c',1,
        'case_000_c1','test','test','npu_smoke','pool_yellow',1,8,'QUEUED',1,?,?)`).bind(now, now - 100),
    // In-flight workflow (sequence=0) that ALSO has a queued child case: must NOT participate in waiting swap.
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_0_run','workflow_001','key:0r','blue','github','depeng1994/lite-actions','lite-actions','run-0',1,
        'case_000_0r','test','test','npu_smoke','pool',1,8,'RUNNING',1,?,?)`).bind(now, now - 400),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at,submitted_at)
      VALUES('workflow_0_q','workflow_002','key:0q','blue','github','depeng1994/lite-actions','lite-actions','run-0',1,
        'case_001_0q','test','test','npu_smoke','pool',1,8,'QUEUED',1,?,?)`).bind(now, now - 400),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_a_1',0,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_a_2',1,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_b_1',2,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_b_2',3,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('yellow','pool_yellow','npu_smoke','workflow_c_1',0,'{"options":[[0]]}',1,?)`).bind(now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool','npu_smoke','workflow_0_q',4,'{"options":[[0]]}',1,?)`).bind(now),
  ]);
  await db.batch(refreshWorkflowRuns(db, [
    {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-0", attempt: 1},
    {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-a", attempt: 1},
    {provider: "github", repo: "depeng1994/lite-actions", run_id: "run-b", attempt: 1},
    {provider: "gitcode", repo: "cann/torchtitan-npu", run_id: "run-c", attempt: 1},
  ], now));
});

test("rejects in-flight sequence=0 workflows and swaps multi-case slots without duplicate queue_pos", async () => {
  // In-flight run-0 must be rejected with 409 even though it has a queued child.
  const inflight = await request("/admin/workflows/move-up", {cookie, data: {
    provider: "github", repo: "depeng1994/lite-actions", run_id: "run-0", attempt: 1,
    queue_version: 1, idempotency_key: "reject-inflight",
  }});
  expect(inflight.status).toBe(409);
  expect(await inflight.json()).toMatchObject({code: "WORKFLOW_NOT_WAITING"});

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
  expect(await db.prepare("SELECT cmd_type FROM commands WHERE op_id=?").bind(result.op_id).first("cmd_type")).toBe("SWAP_WORKFLOW");

  // Multi-case slot swap preserves intra-workflow order with distinct queue_pos.
  const positions = await db.prepare(`SELECT task_uid,queue_pos FROM queue_orders
    WHERE worker_id='blue' AND pool_name='pool' ORDER BY queue_pos`).all<{task_uid: string; queue_pos: number}>();
  expect(positions.results).toEqual([
    {task_uid: "workflow_b_1", queue_pos: 0},
    {task_uid: "workflow_b_2", queue_pos: 1},
    {task_uid: "workflow_a_1", queue_pos: 2},
    {task_uid: "workflow_a_2", queue_pos: 3},
    {task_uid: "workflow_0_q", queue_pos: 4},
  ]);

  // /workflows must display the persisted swapped sequence (0, 1=run-b, 2=run-a, 3=run-c), NOT submitted_at order.
  const board = await (await request("/admin/workflows?history_limit=0", {cookie})).json<{
    queue_version: number; active: Array<{run_id: string; sequence: number; can_move_up: boolean}>;
  }>();
  expect(board.queue_version).toBe(2);
  expect(board.active.map(row => [row.run_id, row.sequence, row.can_move_up])).toEqual([
    ["run-0", 0, false],
    ["run-b", 1, false],
    ["run-a", 2, true],
    ["run-c", 3, true],
  ]);
});

test("cross-pool swap persists workflow rank and concurrent 409 leaves zero dirty writes", async () => {
  // Swap run-c (yellow) ahead of run-a (blue) across unshared pools.
  const cross = await request("/admin/workflows/move-up", {cookie, data: {
    provider: "gitcode", repo: "cann/torchtitan-npu", run_id: "run-c", attempt: 1,
    queue_version: 2, idempotency_key: "move-cross-pool",
  }});
  expect(cross.status).toBe(202);
  const board = await (await request("/admin/workflows?history_limit=0", {cookie})).json<{
    active: Array<{run_id: string; sequence: number}>;
  }>();
  expect(board.active.map(row => [row.run_id, row.sequence])).toEqual([
    ["run-0", 0], ["run-b", 1], ["run-c", 2], ["run-a", 3],
  ]);

  // Stale / concurrent version conflict must produce ZERO commands, ops, or audits.
  const commandsBefore = await db.prepare("SELECT COUNT(*) AS n FROM commands").first<number>("n");
  const auditsBefore = await db.prepare("SELECT COUNT(*) AS n FROM audit_logs").first<number>("n");
  const opsBefore = await db.prepare("SELECT COUNT(*) AS n FROM workflow_priority_ops").first<number>("n");

  const [r1, r2] = await Promise.all([
    request("/admin/workflows/move-up", {cookie, data: {
      provider: "github", repo: "depeng1994/lite-actions", run_id: "run-a", attempt: 1,
      queue_version: 3, idempotency_key: "race-1",
    }}),
    request("/admin/workflows/move-up", {cookie, data: {
      provider: "gitcode", repo: "cann/torchtitan-npu", run_id: "run-c", attempt: 1,
      queue_version: 3, idempotency_key: "race-2",
    }}),
  ]);
  const statuses = [r1.status, r2.status].sort();
  expect(statuses).toEqual([202, 409]);
  // Exactly ONE winner added 1 op and 1 audit; the 409 loser added ZERO rows.
  expect(await db.prepare("SELECT COUNT(*) AS n FROM workflow_priority_ops").first<number>("n")).toBe((opsBefore ?? 0) + 1);
  expect(await db.prepare("SELECT COUNT(*) AS n FROM audit_logs").first<number>("n")).toBe((auditsBefore ?? 0) + 1);
  // Winner involved blue+yellow so <=2 commands; explicitly verify no command was written for the loser op.
  const stale = await request("/admin/workflows/move-up", {cookie, data: {
    provider: "github", repo: "depeng1994/lite-actions", run_id: "run-a", attempt: 1,
    queue_version: 3, idempotency_key: "stale-after-race",
  }});
  expect(stale.status).toBe(409);
  const commandsAfterRace = await db.prepare("SELECT COUNT(*) AS n FROM commands").first<number>("n");
  expect(commandsAfterRace).toBeGreaterThan(commandsBefore ?? 0);
  const staleAgain = await request("/admin/workflows/move-up", {cookie, data: {
    provider: "github", repo: "depeng1994/lite-actions", run_id: "run-a", attempt: 1,
    queue_version: 2, idempotency_key: "stale-again",
  }});
  expect(staleAgain.status).toBe(409);
  expect(await db.prepare("SELECT COUNT(*) AS n FROM commands").first<number>("n")).toBe(commandsAfterRace);
});

test("queue head returns NOOP and workflow projection keeps monotonic queue version after rows are consumed", async () => {
  const version = await db.prepare("SELECT queue_version FROM workflow_queue_meta WHERE id=1").first<number>("queue_version");
  const head = await (await request("/admin/workflows?history_limit=0", {cookie})).json<{
    active: Array<{provider: string; repo: string; run_id: string; attempt: number; sequence: number}>;
  }>();
  const seq1 = head.active.find(row => row.sequence === 1)!;
  const response = await request("/admin/workflows/move-up", {cookie, data: {
    provider: seq1.provider, repo: seq1.repo, run_id: seq1.run_id, attempt: seq1.attempt,
    queue_version: version, idempotency_key: "head",
  }});
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({status: "NOOP", reason: "ALREADY_AT_HEAD"});

  await db.prepare("DELETE FROM queue_orders").run();
  const projection = await request("/admin/workflows?history_limit=0", {cookie});
  expect(projection.status).toBe(200);
  expect((await projection.json<{queue_version: number}>()).queue_version).toBe(version);
});
