import {beforeAll, expect, test} from "vitest";
import type {Timeline} from "../src/types/api";
import {hmac} from "../src/utils/crypto";
import {taskIdentity} from "../src/utils/taskIdentity";
import {bindings, db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);
const host_id = "198.51.100.30:22";
const devices = [0,1,2,3,4,5,6,7];

beforeAll(async () => {
  await migrate();
  const config = {pool_blue: {owner: "blue", enabled: true, type: "ascend-a3", nodes: [{
    host_id, allocation_groups: [{devices, verified: true}],
  }]}};
  await db.batch([
    db.prepare("INSERT INTO pools_configs VALUES(1,'hash',?,'{}','test',?,NULL)").bind(JSON.stringify(config), now),
    db.prepare(`INSERT INTO workers(worker_id,instance_id,applied_rev,code_sha,health_ok,active_count,queued_count,
      last_heartbeat_at,last_idle_probe_at,last_egress_used,updated_at)
      VALUES('blue','01234567',1,'sha',1,0,1,?,?,0,?)`).bind(now, now, now),
    db.prepare(`INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,occupancy_state,probe_at,
      visibility_state,consecutive_failures,last_online_at,last_probe_round,updated_at)
      VALUES('blue','pool_blue','198.51.100.30:22','[0,1,2,3,4,5,6,7]','idle',?,'online',0,?,?,?)`).bind(now, now, 1, now),
    ...[1,3,5,7,9].map((duration, index) => db.prepare(`INSERT INTO task_index(
      task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,case_id,test_id,label,kind,
      target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec,started_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      `sample-${index}`, now - index, "blue", "github", "org/schedule", "schedule", `sample-run-${index}`, 1,
      `case_00${index}_sample`, "npu_smoke", "Sample", "npu_smoke", "pool_blue", 1, 8,
      "[]", "PASS", 1, duration, null,
    )),
    ...[1,3,5,7,9].map((duration, index) => db.prepare(`INSERT INTO duration_samples(
      logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid)
      VALUES('schedule','npu_smoke','pool_blue',1,8,?,?,?)`).bind(duration, now - index, `sample-${index}`)),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,queued_at,updated_at)
      VALUES('schedule-task','schedule-task','schedule-key','blue','github','org/schedule','schedule','schedule-run',1,
      'case_000_scheduled','npu_smoke','Scheduled','npu_smoke','pool_blue',1,8,'QUEUED',1,?,?)`).bind(now, now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool_blue','npu_smoke','schedule-task',0,'{"rev":1,"options":[[0]]}',1,?)`).bind(now),
  ]);
});

test("timeline API projects the median duration from reliable samples", async () => {
  const timeline = await (await request("/public/timeline")).json<{
    projections: Array<{task_uid: string; projection_status: string; est_start?: number; est_end?: number}>;
  }>();
  const projection = timeline.projections.find(({task_uid}) => task_uid === "schedule-task");
  expect(projection).toMatchObject({task_uid: "schedule-task", projection_status: "SCHEDULED", uncertain_release: false});
  const estStart = projection?.est_start;
  const estEnd = projection?.est_end;
  expect(estStart).toBeGreaterThanOrEqual(now);
  expect(estStart).toBeLessThanOrEqual(now + 1);
  expect(estEnd).toBeDefined();
  if (estStart === undefined || estEnd === undefined) throw new Error("missing schedule projection timestamps");
  expect(estEnd).toBe(estStart + 5);
});

test("completed leases respect the 72-hour cutoff and leave current projections intact", async () => {
  const token = "schedule-blue-fixture-key";
  const config = {...bindings, WORKER_BLUE_KEY_HASH: await hmac(bindings.KEY_PEPPER!, token)};
  const base = {w: "blue", i: "01234567"};
  const post = (path: string, data: unknown) => request("/worker/" + path, {
    data, now, headers: {Authorization: `Bearer ${token}`},
  }, config);
  expect((await post("heartbeat", {...base, rev: 1, ok: 1, act: 0, q: 1})).status).toBe(200);
  const cutoff = now - 72 * 3600;
  let sequence = 0;
  const expected: string[] = [];
  for (const [index, finished, hasAllocation] of [[0, cutoff - 1, true], [1, cutoff, true], [2, now - 10, true], [3, now - 1, false]] as const) {
    const identity = {provider: "github" as const, repo: "org/history", run_id: String(index + 100), attempt: 1};
    const caseId = "case_000_history";
    const ids = await taskIdentity(identity, caseId, null);
    const tid = ids.short_tid;
    const start = finished - 120;
    const events = [
      {t: "TU", tid, p: identity.provider, r: identity.repo, run: identity.run_id, att: 1, cid: caseId,
        lbl: "Closed lease", k: "npu_smoke", pool: "pool_blue", req: [1, 8], ph: "RUNNING",
        ts: start, tm: {RUNNING: start}, reliable: 1},
      ...(hasAllocation ? [{t: "TL", tid, ts: start, a: [{h: host_id, d: devices}]}] : []),
      {t: "TF", tid, ts: finished, st: "PASS", ec: 0, cv: 1},
    ];
    expect(await (await post("events", {...base, ev: events.map(event => ({...event, s: ++sequence}))})).json())
      .toMatchObject({ok: true, need_snapshot: false});
    if (hasAllocation && finished >= cutoff) expected.unshift(ids.task_uid);
  }

  for (const [observation, state, projectionStatus] of [
    ["idle", "CONFIRMED_IDLE", "SCHEDULED"],
    ["busy", "UNKNOWN_BUSY", "WAITING_EXTERNAL"],
  ] as const) {
    await db.prepare("UPDATE device_group_states SET occupancy_state=? WHERE host_id=?").bind(observation, host_id).run();
    // Each fixture observation needs a fresh public-cache key.
    const live = await (await request(`/public/timeline?fixture=${observation}`, {now})).json<Timeline>();
    const withHistory = await (await request(`/public/timeline?fixture=${observation}&history_hours=72`, {now})).json<Timeline>();
    const group = (response: Timeline) => response.workers[0].pools[0].nodes[0].device_groups[0];
    expect(group(live).state).toBe(state);
    expect(group(withHistory).state).toBe(state);
    expect(group(live).solid_blocks).toEqual([]);
    expect(group(withHistory).solid_blocks).toMatchObject(expected.map(task_uid => ({task_uid, total_cards: 8, group_count: 1})));
    expect(withHistory.history_tasks?.map(task => task.task_uid)).toEqual(expected);
    const projection = (response: Timeline) => response.projections.find(item => item.task_uid === "schedule-task");
    expect(projection(live)?.projection_status).toBe(projectionStatus);
    expect(projection(withHistory)?.projection_status).toBe(projectionStatus);
    if (projectionStatus === "SCHEDULED") {
      expect(projection(withHistory)).toMatchObject({uncertain_release: false, est_start: withHistory.now});
    }
  }
  const compact = await (await request("/public/timeline?history_hours=72&compact_history=1", {now})).json<Timeline>();
  expect(compact.history_tasks).toBeUndefined();
  expect(compact.history_task_uids).toEqual(expected);
  expect(expected.map(uid => compact.tasks_by_uid[uid].task_uid)).toEqual(expected);
});
