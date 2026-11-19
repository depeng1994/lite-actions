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
    ts: now - 20, tm: {QUEUED: now - 20}, reliable: 1,
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
  expect(await db.prepare("SELECT duration_sec FROM duration_samples WHERE task_uid=?")
    .bind(replacement.ids.task_uid).first("duration_sec")).toBe(12);
});

test("command lookup never acknowledges and receipts are worker-scoped and idempotent", async () => {
  await db.prepare(`
    INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
    VALUES ('cmd_test','yellow','MOVE_UP','{}','PENDING','test',?)
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
