import {beforeAll, expect, test} from "vitest";
import {taskIdentity} from "../src/utils/taskIdentity";
import {hmac} from "../src/utils/crypto";
import type {WorkerId} from "../src/types/api";
import {bindings, db, migrate, request} from "./db";

const identity = {provider: "github" as const, repo: "depeng1994/lite-actions", run_id: "100", attempt: 1};
const blue = "test-blue-worker-key";
const yellow = "test-yellow-worker-key";
const config = {...bindings};
const now = Math.floor(Date.now() / 1000);
const instance = "abcdef01";

beforeAll(async () => {
  await migrate();
  config.WORKER_BLUE_KEY_HASH = await hmac(bindings.KEY_PEPPER!, blue);
  config.WORKER_YELLOW_KEY_HASH = await hmac(bindings.KEY_PEPPER!, yellow);
});

function worker(zone: WorkerId, path: string, data?: unknown) {
  return request("/worker/" + path, {data, headers: {Authorization: "Bearer " + (zone === "blue" ? blue : yellow)}}, config);
}
function heartbeat(zone: WorkerId) {
  return worker(zone, "heartbeat", {w: zone, i: instance, rev: 1, sha: "12345678", ok: 1, act: 1, q: 1});
}
async function descriptor(caseId: string) {
  const ids = await taskIdentity(identity, caseId, null);
  return {ids, task: {
    tid: ids.short_tid, p: "github", r: identity.repo, run: identity.run_id, att: 1,
    cid: caseId, lbl: "CI", k: "npu_smoke", pool: "pool_blue", req: [1, 8], ph: "QUEUED",
    ts: now - 20, sub: now - 30, wn: "Dispatcher Smoke", rn: 17, dt: "Lite Actions self smoke: cpu-only", tm: {QUEUED: now - 20}, reliable: 1,
  }};
}

test("binds zones, ingests transitions exactly once, and reconciles only a complete snapshot", async () => {
  expect(await (await heartbeat("blue")).json()).toMatchObject({ok: true, pending_commands: 0, need_snapshot: true});
  expect((await heartbeat("yellow")).status).toBe(200);
  const base = {w: "blue", i: instance};
  const {ids, task} = await descriptor("case_000_alpha");
  expect((await worker("yellow", "events", {...base, ev: []})).status).toBe(403);
  const batch = {...base, ev: [
    {s: 1, t: "TU", ...task},
    {s: 2, t: "TP", tid: task.tid, ts: now - 10, ph: "RUNNING"},
  ]};
  expect(await (await worker("blue", "events", batch)).json()).toEqual({ok: true, need_snapshot: false});
  expect((await worker("blue", "events", batch)).status).toBe(200);
  expect(await db.prepare("SELECT count(*) AS n FROM worker_event_dedup").first("n")).toBe(2);
  expect(await db.prepare("SELECT submitted_at,workflow_name,run_number,display_title FROM tasks_active WHERE task_uid=?")
    .bind(ids.task_uid).first()).toMatchObject({submitted_at: now - 30, workflow_name: "Dispatcher Smoke", run_number: 17, display_title: "Lite Actions self smoke: cpu-only"});
  expect(await db.prepare("SELECT is_active,total_case_count,result_state,submitted_at,workflow_name,run_number,display_title FROM workflow_runs WHERE run_id=?")
    .bind(identity.run_id).first()).toMatchObject({is_active: 1, total_case_count: 1, result_state: "QUEUED",
      submitted_at: now - 30, workflow_name: "Dispatcher Smoke", run_number: 17, display_title: "Lite Actions self smoke: cpu-only"});
  const gap = await worker("blue", "events", {...base, ev: [{s: 4, t: "TF", tid: task.tid, ts: now, st: "PASS", cv: 1}]});
  expect(await gap.json()).toEqual({ok: true, need_snapshot: true, expected_seq: 3});
  const replacement = await descriptor("case_001_beta");
  const snapshots = {...base, sid: "0123456789", seq: 4, n: 3};
  const group = {pool: "pool_blue", h: "198.51.100.2:22", d: [0, 1], st: "idle", ts: now};
  expect(await (await worker("blue", "snapshots", {...snapshots, p: 3, kind: "device", items: [group]})).json()).toMatchObject({complete: false});
  expect(await (await worker("blue", "snapshots", {...snapshots, p: 1, kind: "task", items: [replacement.task]})).json()).toMatchObject({complete: false});
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(ids.task_uid).first("phase")).toBe("RUNNING");
  const middle = {...snapshots, p: 2, kind: "queue", items: [{pool: "pool_blue", k: "npu_smoke",
    v: 1, rev: 1, items: [{tid: replacement.task.tid, pos: 0, ix: [[0]]}]}]};
  const complete = await worker("blue", "snapshots", middle);
  expect(await complete.json()).toEqual({ok: true, complete: true});
  expect(await (await worker("blue", "snapshots", middle)).json()).toEqual({ok: true, complete: true});
  expect(await db.prepare("SELECT is_active,has_waiting_resource_case FROM workflow_runs WHERE run_id=?")
    .bind(identity.run_id).first()).toMatchObject({is_active: 1, has_waiting_resource_case: 1});
  expect(await db.prepare("SELECT status FROM task_index WHERE task_uid=?").bind(ids.task_uid).first("status")).toBe("INFRA_ERROR");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(replacement.ids.task_uid).first("phase")).toBe("QUEUED");
  expect(await db.prepare("SELECT count(*) AS n FROM queue_orders").first("n")).toBe(1);
  expect(await db.prepare("SELECT occupancy_state FROM device_group_states").first("occupancy_state")).toBe("idle");
  const terminal = {...base, ev: [
    {s: 5, t: "TP", tid: replacement.task.tid, ts: now - 12, ph: "RUNNING"},
    {s: 6, t: "TF", tid: replacement.task.tid, ts: now, st: "PASS", ec: 0, cv: 1},
  ]};
  expect(await (await worker("blue", "events", terminal)).json()).toEqual({ok: true, need_snapshot: false});
  expect((await worker("blue", "events", terminal)).status).toBe(200);
  expect(await db.prepare("SELECT started_at FROM task_index WHERE task_uid=?")
    .bind(replacement.ids.task_uid).first("started_at")).toBe(now - 12);
  expect(await db.prepare("SELECT duration_sec FROM duration_samples WHERE task_uid=?")
    .bind(replacement.ids.task_uid).first("duration_sec")).toBe(12);
  expect(await db.prepare("SELECT is_active,total_case_count,result_state,first_started_at FROM workflow_runs WHERE run_id=?")
    .bind(identity.run_id).first()).toMatchObject({is_active: 0, total_case_count: 2, result_state: "FAIL"});
  expect(await db.prepare("SELECT first_started_at FROM workflow_runs WHERE run_id=?")
    .bind(identity.run_id).first("first_started_at")).toBe(now - 12);
});

test("command lookup never acknowledges and receipts are worker-scoped and idempotent", async () => {
  await db.prepare(`
    INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
    VALUES ('cmd_test','yellow','SWAP_WORKFLOW','{}','PENDING','test',?)
  `).bind(now).run();
  expect(await (await heartbeat("yellow")).json()).toMatchObject({pending_commands: 1});
  expect(await (await worker("blue", "commands")).json()).toEqual({commands: []});
  expect(await (await worker("yellow", "commands")).json()).toMatchObject({commands: [{cmd_id: "cmd_test"}]});
  expect(await (await heartbeat("yellow")).json()).toMatchObject({pending_commands: 1});
  const receipt = {cmd_id: "cmd_test", status: "ACKED"};
  expect((await worker("blue", "command-receipts", receipt)).status).toBe(404);
  expect((await worker("yellow", "command-receipts", receipt)).status).toBe(200);
  expect((await worker("yellow", "command-receipts", receipt)).status).toBe(200);
  expect((await worker("yellow", "command-receipts", {...receipt, status: "APPLIED", pos: 1})).status).toBe(200);
  expect(await (await heartbeat("yellow")).json()).toMatchObject({pending_commands: 0});
});

test("multipart queue options and leases preserve all parts through completion", async () => {
  const i = "abcd1234";
  const base = {w: "blue", i};
  await worker("blue","heartbeat",{...base,rev:1,sha:"",ok:1,act:1,q:1});
  const {ids, task} = await descriptor("case_002_gamma");
  const queue = {t:"QP",ts:now,pool:"pool_blue",k:"npu_smoke",v:3,rev:1};
  const first = {h:"198.51.100.2:22",d:[0,1]};
  const second = {h:"198.51.100.3:22",d:[2,3]};
  expect((await worker("blue","events",{...base,ev:[
    {s:1,t:"TU",...task},
    {...queue,s:2,items:[{tid:task.tid,pos:0,ix:[[0],[1]],off:0}]},
    {...queue,s:3,last:1,items:[{tid:task.tid,pos:0,ix:[[2,3]],off:2}]},
  ]})).status).toBe(200);
  const options = await db.prepare("SELECT candidate_group_idxs_json FROM queue_orders WHERE task_uid=?")
    .bind(ids.task_uid).first<string>("candidate_group_idxs_json");
  expect(JSON.parse(options!).options).toEqual([[0],[1],[2,3]]);
  expect((await worker("blue","events",{...base,ev:[
    {s:4,t:"TL",ts:now,tid:task.tid,ap:1,a:[first]},
    {s:5,t:"TL",ts:now,tid:task.tid,ap:2,a:[second]},
    {s:6,t:"TF",ts:now,tid:task.tid,st:"CANCELLED",ec:130,cv:0,qu:1},
  ]})).status).toBe(200);
  const lease = await db.prepare("SELECT actual_allocation_json FROM task_index WHERE task_uid=?")
    .bind(ids.task_uid).first<string>("actual_allocation_json");
  expect(JSON.parse(lease!)).toEqual([first,second].map(node => ({host_id:node.h,devices:node.d})));
  expect(await db.prepare("SELECT count(*) AS n FROM device_group_states WHERE quarantine_task_uid=? AND quarantined=1")
    .bind(ids.task_uid).first("n")).toBe(2);
});

test("workflow priority receipts preserve pool outcomes and close the operation", async () => {
  await db.batch([
    db.prepare(`INSERT INTO workflow_priority_ops(op_id,idempotency_key,queue_version_before,queue_version_after,
      target_wf_json,swapped_wf_json,from_seq,to_seq,status,requested_by,created_at)
      VALUES('op_receipt',NULL,1,2,'{}','{}',2,1,'PENDING','test',?)`).bind(now),
    db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,op_id,payload_json,status,created_by,created_at)
      VALUES('cmd_receipt','yellow','SWAP_WORKFLOW','op_receipt','{}','PENDING','test',?)`).bind(now),
  ]);
  await heartbeat("yellow");
  expect((await worker("yellow", "command-receipts", {cmd_id: "cmd_receipt", status: "ACKED"})).status).toBe(200);
  const applied = await worker("yellow", "command-receipts", {
    cmd_id: "cmd_receipt", status: "APPLIED",
    pools: [{p: "pool_blue", k: "npu_smoke", st: "APPLIED", c: 2}],
  });
  expect(applied.status).toBe(200);
  expect(await db.prepare("SELECT status,result_json FROM commands WHERE cmd_id='cmd_receipt'").first()).toMatchObject({status: "APPLIED"});
  expect(JSON.parse(String(await db.prepare("SELECT result_json FROM commands WHERE cmd_id='cmd_receipt'").first<string>("result_json"))).pools)
    .toEqual([{p: "pool_blue", k: "npu_smoke", st: "APPLIED", c: 2}]);
  expect(await db.prepare("SELECT status FROM workflow_priority_ops WHERE op_id='op_receipt'").first("status")).toBe("APPLIED");
});
